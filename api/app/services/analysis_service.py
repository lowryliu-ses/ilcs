"""检测任务、回传事件与结果审核。

三条互不替代的线：
- 采集：pending → collecting → collected，看的是「要求指标是否都有合法记录」。
- 质量：unassessed / valid / suspect / invalid，由有权限的判定动作给出。
- 审核：pending / approved / rejected，由授权复核人绑定确切结果版本给出。
采集完成不自动 valid，也不自动 approved；批次跑完同样不会。
"""
from __future__ import annotations

import hashlib
import json

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..core.clock import now
from ..core.context import SERVICE, AccessContext
from ..core.errors import (
    NotFound, PermissionDenied, StateConflict, ValidationFailed,
)
from ..domain.access import same_person, service_may_submit_task
from ..domain import dataquality
from ..domain.metrics import check_value, collected
from ..models import (
    AnalysisTask, IngestEvent, MetricDefinition, ResultReview, ResultValue, Sample, User,
)
from ..repositories.batches import AnalysisTaskRepository, SampleRepository
from ..repositories.files import FileRepository
from ..repositories.governance import AccessLogRepository, UserRepository
from ..repositories.metrics import (
    DataRuleRepository, IngestEventRepository, MetricRepository, ResultReviewRepository, ResultValueRepository,
)
from ..repositories.samples import PhysicalSampleRepository
from .audit_service import AuditService
from .identity_service import IdentityService, admin_self_approval

TASK_STATES = ("pending", "collecting", "collected", "cancelled")
TASK_STATE_LABEL = {
    "pending": "待采集", "collecting": "采集中", "collected": "已采集", "cancelled": "已取消",
}
QUALITIES = ("unassessed", "valid", "suspect", "invalid")
QUALITY_LABEL = {
    "unassessed": "未判定", "valid": "有效", "suspect": "可疑", "invalid": "无效",
}
REVIEW_LABEL = {"pending": "待复核", "approved": "已通过", "rejected": "已退回"}


def digest(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()


class AnalysisService:
    def __init__(self, db: Session, ctx: AccessContext):
        self.db = db
        self.ctx = ctx
        self.tasks = AnalysisTaskRepository(db, ctx)
        self.assignments = SampleRepository(db, ctx)
        self.physical = PhysicalSampleRepository(db, ctx)
        self.metrics = MetricRepository(db, ctx)
        self.events = IngestEventRepository(db, ctx)
        self.values = ResultValueRepository(db, ctx)
        self.reviews = ResultReviewRepository(db, ctx)
        self.files = FileRepository(db, ctx)
        self.users = UserRepository(db)
        self.access_log = AccessLogRepository(db)
        self.audit = AuditService(db, ctx)
        self.identity = IdentityService(db, ctx)

    # ---------- 检测任务 ----------

    def create_task(self, payload: dict, user: User | None = None) -> AnalysisTask:
        """建任务时冻结要求指标。之后改指标定义不影响这个任务的要求集合。"""
        assignment_id = payload.get("sample_id") or ""
        assignment = self.assignments.get(assignment_id) if assignment_id else None
        physical_id = payload.get("physical_sample_id") or (
            assignment.physical_sample_id if assignment else ""
        )
        if not physical_id and assignment is None:
            raise ValidationFailed("检测任务必须绑定一个样本")
        if physical_id and not self.physical.get(physical_id):
            raise NotFound("物理样本不存在或不在当前组织范围内")
        required = list(payload.get("required_metrics") or [])
        if not required:
            raise ValidationFailed("检测任务必须至少要求一个指标", code="metrics_required")
        known = self.metrics.many(required)
        unknown = [metric_id for metric_id in required if metric_id not in known]
        if unknown:
            raise NotFound(f"指标定义不存在：{'、'.join(unknown)}")
        retired = [m.id for m in known.values() if m.state != "active"]
        if retired:
            raise StateConflict(f"指标定义已停用，不能用于新任务：{'、'.join(retired)}")
        round_no = payload.get("round_no") or (self.tasks.max_round(physical_id) + 1)
        task = AnalysisTask(
            org_id=self.ctx.org_id,
            sample_id=assignment_id or None,
            physical_sample_id=physical_id,
            method=payload.get("method", ""),
            method_version=payload.get("method_version", ""),
            required_metrics=required,
            round_no=round_no,
            state="pending",
            external_ref=payload.get("external_ref", ""),
            retest_of=payload.get("retest_of", ""),
            created_by=user.id if user else self.ctx.subject_id,
        )
        self.tasks.add(task)
        return task

    def create_task_api(self, payload: dict, user: User) -> dict:
        task = self.create_task(payload, user)
        self.audit.record(
            user, "建立检测任务", task.id, before="—", after="待采集",
            detail=(
                f"样本 {task.physical_sample_id}；第 {task.round_no} 轮；"
                f"要求指标 {len(task.required_metrics)} 项已冻结"
                + (f"；复测自 {task.retest_of}" if task.retest_of else "")
            ),
        )
        self.db.commit()
        return self.task_out(task)

    def retest(self, task_id: str, payload: dict, user: User) -> dict:
        """重测创建新检测任务与新轮次，不覆盖原任务与原结果。"""
        source = self.tasks.get(task_id)
        if not source:
            raise NotFound("检测任务不存在")
        task = self.create_task(
            {
                "sample_id": source.sample_id,
                "physical_sample_id": source.physical_sample_id,
                "method": payload.get("method", source.method),
                "method_version": payload.get("method_version", source.method_version),
                "required_metrics": payload.get("required_metrics") or source.required_metrics,
                "retest_of": source.id,
            },
            user,
        )
        self.audit.record(
            user, "创建重测任务", task.id, before="—", after="待采集",
            detail=f"重测自 {source.id}（第 {source.round_no} 轮）；理由 {payload.get('reason', '—')}",
        )
        self.db.commit()
        return self.task_out(task)

    def cancel_task(self, task_id: str, reason: str, user: User) -> dict:
        task = self.tasks.get(task_id)
        if not task:
            raise NotFound("检测任务不存在")
        if task.state == "collected":
            raise StateConflict("已采集完成的任务不能取消；数据处置请走复核与更正")
        if not reason.strip():
            raise ValidationFailed("取消检测任务必须填写理由")
        before = task.state
        task.state = "cancelled"
        self.tasks.bump(task)
        self.audit.record(
            user, "取消检测任务", task.id, before=TASK_STATE_LABEL.get(before, before),
            after="已取消", detail=reason,
        )
        self.db.commit()
        return self.task_out(task)

    # ---------- 回传 ----------

    def ingest(self, payload: dict, source_label: str | None = None) -> dict:
        """结果回传。

        整次原子：任务归属、样本、指标是否在要求集合中、类型与单位任一不匹配，
        整个事件拒绝，不留部分结果。
        """
        event_id = (payload.get("event_id") or "").strip()
        if not event_id:
            self._reject("event_id_required", "回传缺少事件编号")
            raise ValidationFailed(
                "回传必须带事件编号 event_id；不存在匿名或缺编号的正式回传旁路",
                code="event_id_required",
            )
        task_id = (payload.get("task_id") or "").strip()
        task = self.tasks.get(task_id) if task_id else None
        if task is None:
            self._reject("task_not_found", f"检测任务 {task_id} 不在来源授权范围内或不存在")
            raise NotFound(f"检测任务 {task_id} 不存在或不在当前范围内")
        if self.ctx.subject_kind == SERVICE and not service_may_submit_task(
            self.ctx.scopes, task.id
        ):
            self._reject("task_not_authorized", f"服务身份未被授权提交任务 {task.id}")
            raise PermissionDenied(
                f"该服务身份未被授权提交检测任务 {task.id} 的结果", code="task_not_authorized"
            )
        if task.state == "cancelled":
            raise StateConflict("检测任务已取消，不接受回传")

        source = source_label or self.ctx.subject_label or self.ctx.subject_id
        existing = self.events.find(source, task.id, event_id)
        if existing is not None:
            # 顺序或并发重传相同内容：回放原事件与原结果，只有一份业务写入
            if existing.digest and existing.digest != digest(payload):
                raise StateConflict(
                    "同一事件编号提交了不同内容，已拒绝；修订请显式引用原版本",
                    {"event_id": event_id},
                    code="ingest_content_conflict",
                )
            return {**existing.response, "replayed": True}

        declared_sample = (payload.get("sample_id") or "").strip()
        if declared_sample and declared_sample not in {
            task.sample_id, task.physical_sample_id
        }:
            self._reject("sample_mismatch", f"事件声明样本 {declared_sample} 与任务不符")
            raise StateConflict(
                f"事件声明的样本 {declared_sample} 与任务绑定的样本不一致，整次拒绝",
                code="sample_mismatch",
            )
        # 仪器序列号只能作业务数据校验，不能代替来源认证
        serial = (payload.get("instrument_serial") or "").strip()
        if serial and self.ctx.subject_kind == SERVICE:
            allowed = set((self.ctx.scopes or {}).get("instrument_serials") or [])
            if allowed and serial not in allowed:
                self._reject("serial_mismatch", f"仪器序列号 {serial} 不在授权列表内")
                raise PermissionDenied(
                    f"仪器序列号 {serial} 与授权设备不一致", code="serial_mismatch"
                )

        # 测出这些值的工位：声明了就必须是本组织的工位，服务身份还要在它的工位授权范围内
        station_id = (payload.get("station_id") or "").strip()
        if station_id:
            from ..repositories.resources import StationRepository

            if StationRepository(self.db, self.ctx).get(station_id) is None:
                self._reject("station_not_found", f"工位 {station_id} 不存在")
                raise NotFound(f"工位 {station_id} 不存在或不在当前组织范围内")
            allowed_stations = (self.ctx.scopes or {}).get("stations") if self.ctx.subject_kind == SERVICE else None
            if isinstance(allowed_stations, list) and allowed_stations and station_id not in allowed_stations:
                self._reject("station_not_authorized", f"服务身份未被授权代表工位 {station_id}")
                raise PermissionDenied(f"该服务身份未被授权代表工位 {station_id}", code="station_not_authorized")

        metrics = payload.get("metrics") or []
        if not metrics:
            raise ValidationFailed("回传至少要有一个指标", code="metrics_required")
        required = set(task.required_metrics or [])
        definitions = self.metrics.many([row.get("metric_version_id") for row in metrics])
        raw_file_id = (payload.get("raw_file_id") or "").strip()
        raw_file = self.files.available(raw_file_id) if raw_file_id else None
        if raw_file_id and raw_file is None:
            self._reject("file_not_found", f"原始文件 {raw_file_id} 不在范围内")
            raise NotFound("原始文件不存在、不可用或不在当前组织范围内")

        problems: list[dict] = []
        current = self.values.current_for_task(task.id)
        prepared: list[dict] = []
        seen: set[str] = set()
        for index, row in enumerate(metrics, start=1):
            metric_id = (row.get("metric_version_id") or "").strip()
            definition = definitions.get(metric_id)
            if definition is None:
                problems.append({"key": f"metric{index}", "label": f"指标定义 {metric_id} 不存在"})
                continue
            if metric_id not in required:
                problems.append(
                    {"key": f"metric{index}",
                     "label": f"指标 {definition.code} 不在该任务冻结的要求集合内"}
                )
                continue
            if metric_id in seen:
                problems.append(
                    {"key": f"metric{index}", "label": f"同一事件里指标 {definition.code} 重复"}
                )
                continue
            seen.add(metric_id)
            reason = (row.get("not_measured_reason") or "").strip()
            if reason:
                prepared.append({"definition": definition, "value": None, "reason": reason,
                                 "unit": row.get("unit") or definition.unit})
                continue
            if "value" not in row or row["value"] is None:
                problems.append(
                    {"key": f"metric{index}",
                     "label": f"指标 {definition.code} 没有值：缺值不当成 0，"
                              f"确实测不到请写 not_measured_reason"}
                )
                continue
            errors = check_value(
                definition.value_type, definition.unit, definition.rules or {},
                row["value"], row.get("unit") or definition.unit,
            )
            problems.extend(
                {"key": f"metric{index}", "label": f"{definition.code}：{error}"} for error in errors
            )
            if errors:
                continue
            existing_value = current.get(metric_id)
            if existing_value is not None and not existing_value.not_measured_reason:
                problems.append(
                    {"key": f"metric{index}",
                     "label": f"指标 {definition.code} 已有当前值（版本 "
                              f"{existing_value.result_version}）；正常采集重复冲突，"
                              f"修订请用 /result-values/{{id}}/revisions 并说明原因"}
                )
                continue
            prepared.append(
                {"definition": definition, "value": row["value"], "reason": "",
                 "unit": row.get("unit") or definition.unit,
                 # 越界不拒收：入库打标，质量置可疑，交审核下结论
                 "flags": dataquality.range_flags(
                     definition.value_type, definition.rules or {}, row["value"], definition.code,
                 )}
            )

        if not problems:
            problems.extend(self._logic_check(current, prepared))

        if problems:
            self._reject("ingest_rejected", "；".join(p["label"] for p in problems)[:480])
            self.db.commit()
            raise StateConflict(
                "回传被整次拒绝，未写入任何结果",
                {"blocked": problems},
                code="ingest_rejected",
            )

        event = IngestEvent(
            org_id=self.ctx.org_id, source=source,
            service_identity_id=self.ctx.subject_id if self.ctx.subject_kind == SERVICE else "",
            analysis_task_id=task.id, event_id=event_id, digest=digest(payload),
            payload=payload, state="accepted",
        )
        self.db.add(event)
        try:
            self.db.flush()
        except IntegrityError:
            self.db.rollback()
            existing = self.events.find(source, task.id, event_id)
            if existing is None:
                raise
            return {**existing.response, "replayed": True}

        written: list[ResultValue] = []
        for row in prepared:
            definition: MetricDefinition = row["definition"]
            version = self.values.max_version(task.id, definition.id) + 1
            value = ResultValue(
                org_id=self.ctx.org_id, analysis_task_id=task.id,
                physical_sample_id=task.physical_sample_id, assignment_id=task.sample_id or "",
                metric_definition_id=definition.id, ingest_event_id=event.id,
                value_num=float(row["value"]) if (
                    definition.value_type == "number" and row["value"] is not None
                ) else None,
                value_text=str(row["value"]) if (
                    definition.value_type != "number" and row["value"] is not None
                ) else "",
                unit=row["unit"], collected_at=payload.get("collected_at") or now(),
                raw_file_id=raw_file_id, parser_version=payload.get("parser_version", ""),
                result_version=version, not_measured_reason=row["reason"],
                quality="suspect" if row.get("flags") else "unassessed", review_state="pending",
                provenance="device" if self.ctx.subject_kind == SERVICE else "manual",
                entered_by="" if self.ctx.subject_kind == SERVICE else self.ctx.subject_id,
                flags=list(row.get("flags") or []), station_id=station_id,
                instrument=(payload.get("instrument_serial") or "").strip(),
            )
            self.db.add(value)
            written.append(value)
        self.db.flush()
        if raw_file is not None:
            raw_file.ref_type = "analysis_task"
            raw_file.ref_id = task.id

        state_before = task.state
        self._refresh_task_state(task)
        response = {
            "event": {"id": event.id, "event_id": event.event_id, "source": source},
            "task_id": task.id,
            "task_state": task.state,
            "results": [
                {
                    "id": value.id, "metric_definition_id": value.metric_definition_id,
                    "result_version": value.result_version,
                    "quality": value.quality, "review_state": value.review_state,
                    "not_measured_reason": value.not_measured_reason,
                    "flags": value.flags or [],
                }
                for value in written
            ],
            "replayed": False,
            # 采集完成不等于审核通过，响应里说清楚
            "note": "已采集入账；质量与审核状态仍待复核，未进入正式统计",
        }
        event.response = response
        self.audit.record(
            None, "检测结果回传", task.id, before=TASK_STATE_LABEL.get(state_before, state_before),
            after=TASK_STATE_LABEL.get(task.state, task.state),
            detail=(
                f"事件 {source}/{event_id}；{len(written)} 项指标；"
                f"原始文件 {raw_file_id or '无'}；解析版本 {payload.get('parser_version') or '—'}"
                + (f"；工位 {station_id}" if station_id else "")
                + (
                    f"；自动打标 {sum(1 for value in written if value.flags)} 项（越界 / 逻辑冲突，已置可疑）"
                    if any(value.flags for value in written) else ""
                )
            ),
        )
        self.db.commit()
        return response

    def _logic_check(self, current: dict[str, ResultValue], prepared: list[dict]) -> list[dict]:
        """前后逻辑校验：用本任务已有的当前值加上本次提交的值，按组织的逻辑规则比对。

        只判至少涉及一个本次新值的规则（只涉及旧值的冲突早先已经判过）。级别 reject 的冲突整次拒收，
        级别 flag 的给本次涉及的新值打标。返回拒收问题；打标直接写进 prepared。
        """
        rules = DataRuleRepository(self.db, self.ctx).enabled_specs()
        if not rules:
            return []
        values: dict[str, float] = {}
        for value in current.values():
            definition = self.metrics.get(value.metric_definition_id)
            if definition is not None and value.value_num is not None:
                values[definition.code] = float(value.value_num)
        fresh: dict[str, dict] = {}
        for row in prepared:
            definition = row["definition"]
            if definition.value_type == "number" and row["value"] is not None:
                values[definition.code] = float(row["value"])
                fresh[definition.code] = row
        problems: list[dict] = []
        for rule, message in dataquality.violations(rules, values):
            involved = [code for code in (rule.left, rule.right) if code and code in fresh]
            if not involved:
                continue
            if rule.severity == "reject":
                problems.append({"key": f"rule:{rule.id}", "label": f"逻辑冲突（拒收）：{message}"})
                continue
            for code in involved:
                fresh[code]["flags"] = [
                    *(fresh[code].get("flags") or []), dataquality.flag("logic", message, rule_id=rule.id),
                ]
        return problems

    def _reject(self, code: str, reason: str) -> None:
        """拒绝事件另写访问日志，不制造成功业务审计。"""
        self.access_log.record(
            org_id=self.ctx.org_id, subject=self.ctx.subject_id,
            subject_kind=self.ctx.subject_kind, method="POST", path="/api/integrations/results",
            code=code, reason=reason, request_id=self.ctx.request_id,
        )

    def _refresh_task_state(self, task: AnalysisTask) -> None:
        """采集状态只由「要求指标是否都有合法记录」决定。"""
        current = self.values.current_for_task(task.id)
        done, missing = collected(list(task.required_metrics or []), current)
        if task.state == "cancelled":
            return
        task.state = "collected" if done else ("collecting" if current else "pending")
        self.tasks.bump(task)

    # ---------- 人工录入 ----------

    def enter_manual(self, task_id: str, payload: dict, user: User) -> dict:
        """研究员或操作员人工录入。记录录入人，设备数据不伪造人工作者。"""
        task = self.tasks.get(task_id)
        if not task:
            raise NotFound("检测任务不存在")
        event_id = (payload.get("event_id") or "").strip()
        if not event_id:
            raise ValidationFailed("人工录入也要带稳定事件编号", code="event_id_required")
        result = self.ingest(
            {
                "event_id": event_id,
                "task_id": task.id,
                "sample_id": payload.get("sample_id", ""),
                "collected_at": payload.get("collected_at"),
                "parser_version": payload.get("parser_version", ""),
                "raw_file_id": payload.get("raw_file_id", ""),
                "station_id": payload.get("station_id", ""),
                "instrument_serial": payload.get("instrument_serial", ""),
                "metrics": payload.get("metrics") or [],
            },
            source_label=f"manual:{user.id}",
        )
        for row in result.get("results", []):
            value = self.values.get(row["id"])
            if value is not None:
                value.provenance = "manual"
                value.entered_by = user.id
        self.db.commit()
        return result

    # ---------- 修订与复核 ----------

    def revise(self, value_id: str, payload: dict, user: User) -> dict:
        """更正产生新版本，显式引用原版本并说明原因；旧记录不被覆盖。"""
        source = self.values.get(value_id)
        if not source:
            raise NotFound("结果记录不存在")
        if source.superseded_by_id:
            raise StateConflict("该版本已被取代，请针对最新版本更正")
        reason = (payload.get("reason") or "").strip()
        if not reason:
            raise ValidationFailed("更正必须说明原因", code="revision_reason_required")
        definition = self.metrics.get(source.metric_definition_id)
        if definition is None:
            raise NotFound("指标定义不存在")
        errors = check_value(
            definition.value_type, definition.unit, definition.rules or {},
            payload.get("value"), payload.get("unit") or definition.unit,
        )
        if errors and not (payload.get("not_measured_reason") or "").strip():
            raise ValidationFailed("；".join(errors))
        # 更正后的值同样打标：越界、与本任务其他指标逻辑冲突
        measured = not (payload.get("not_measured_reason") or "").strip()
        pending = [{
            "definition": definition, "value": payload.get("value") if measured else None,
            "flags": dataquality.range_flags(definition.value_type, definition.rules or {}, payload.get("value"), definition.code)
            if measured else [],
        }]
        others = {key: row for key, row in self.values.current_for_task(source.analysis_task_id).items() if key != definition.id}
        refused = self._logic_check(others, pending)
        if refused:
            raise StateConflict("更正后的值与本任务其他指标逻辑冲突，已拒绝", {"blocked": refused}, code="logic_rejected")
        version = self.values.max_version(source.analysis_task_id, definition.id) + 1
        revision = ResultValue(
            org_id=self.ctx.org_id, analysis_task_id=source.analysis_task_id,
            physical_sample_id=source.physical_sample_id, assignment_id=source.assignment_id,
            metric_definition_id=definition.id, ingest_event_id="",
            value_num=float(payload["value"]) if (
                definition.value_type == "number" and payload.get("value") is not None
            ) else None,
            value_text=str(payload.get("value")) if (
                definition.value_type != "number" and payload.get("value") is not None
            ) else "",
            unit=payload.get("unit") or definition.unit,
            collected_at=payload.get("collected_at") or source.collected_at,
            raw_file_id=payload.get("raw_file_id", "") or source.raw_file_id,
            parser_version=payload.get("parser_version", "") or source.parser_version,
            result_version=version, revises_id=source.id,
            not_measured_reason=(payload.get("not_measured_reason") or ""),
            quality="suspect" if pending[0]["flags"] else "unassessed", review_state="pending", provenance="correction",
            entered_by=user.id, flags=list(pending[0]["flags"]),
            station_id=source.station_id, instrument=source.instrument,
        )
        self.db.add(revision)
        self.db.flush()
        source.superseded_by_id = revision.id
        source.row_version = int(source.row_version or 0) + 1
        task = self.tasks.get(source.analysis_task_id)
        if task is not None:
            self._refresh_task_state(task)
        self.audit.record(
            user, "更正检测结果", revision.id,
            before=f"v{source.result_version}={self._display(source)}",
            after=f"v{version}={self._display(revision)}",
            detail=f"原记录 {source.id} 保留；原因：{reason}",
            object_version=version,
        )
        self.db.commit()
        return {**self.value_out(revision), "revises": self.value_out(source)}

    def review(self, value_id: str, payload: dict, user: User) -> dict:
        """复核。本人不能审核本人录入的记录；管理员也不例外。"""
        value = self.values.get(value_id)
        if not value:
            raise NotFound("结果记录不存在")
        if value.superseded_by_id:
            raise StateConflict("该版本已被取代，请复核最新版本")
        expected = payload.get("result_version")
        if expected is not None and int(expected) != int(value.result_version):
            raise StateConflict(
                f"提交的结果版本 {expected} 与当前 {value.result_version} 不一致，请刷新后重试",
                code="version_conflict",
            )
        conclusion = payload.get("conclusion")
        if conclusion not in {"approved", "rejected"}:
            raise ValidationFailed("审核结论只能是 approved 或 rejected")
        quality = payload.get("quality", "unassessed")
        if quality not in QUALITIES:
            raise ValidationFailed(f"质量判定只能是 {'、'.join(QUALITIES)}")
        if conclusion == "approved" and quality == "unassessed":
            raise ValidationFailed(
                "通过审核必须同时给出质量判定；审核完成不等于质量有效",
                code="quality_required",
            )
        reason = (payload.get("reason") or "").strip()
        if conclusion == "rejected" and not reason:
            raise ValidationFailed("退回必须写明理由")
        if quality in {"suspect", "invalid"} and not reason:
            raise ValidationFailed("判定为可疑或无效必须写明理由")
        if same_person(value.entered_by, user.id) and not admin_self_approval(
            self.db, self.ctx, user, value.id, "复核本人录入的结果",
        ):
            raise PermissionDenied(
                "不能审核本人录入的记录（职责分离）", code="self_review_denied"
            )
        signature = self.identity.consume_signature(
            payload.get("signature_id"), user, f"结果复核：{conclusion}",
            object_ref=value.id, object_version=value.result_version,
        )
        before = f"{value.review_state}/{value.quality}"
        value.review_state = conclusion
        value.quality = quality
        value.row_version = int(value.row_version or 0) + 1
        record = ResultReview(
            org_id=self.ctx.org_id, result_value_id=value.id,
            result_version=value.result_version, reviewer_id=user.id, conclusion=conclusion,
            quality=quality, reason=reason, signature_id=signature.id,
        )
        self.reviews.add(record)
        self.audit.record(
            user, "复核检测结果", value.id, sign=True, meaning=signature.meaning,
            before=before, after=f"{conclusion}/{quality}", signature_id=signature.id,
            object_version=value.result_version,
            detail=(
                f"指标 {value.metric_definition_id} v{value.result_version}；{reason or '无附加说明'}；"
                f"{'质量有效，可进入正式统计' if conclusion == 'approved' and quality == 'valid' else '不进入正式统计'}"
            ),
        )
        self.db.commit()
        return self.value_out(value)

    # ---------- 输出 ----------

    @staticmethod
    def _display(value: ResultValue) -> str:
        if value.not_measured_reason:
            return f"未测（{value.not_measured_reason}）"
        if value.value_num is not None:
            return f"{value.value_num:g}{value.unit}"
        return value.value_text or "—"

    def value_out(self, value: ResultValue) -> dict:
        definition = self.metrics.get(value.metric_definition_id)
        entered_by = self.users.get(value.entered_by) if value.entered_by else None
        reviews = self.reviews.for_value(value.id)
        return {
            "id": value.id,
            "analysis_task_id": value.analysis_task_id,
            "physical_sample_id": value.physical_sample_id,
            "assignment_id": value.assignment_id,
            "metric_definition_id": value.metric_definition_id,
            "metric_code": definition.code if definition else "",
            "metric_name": definition.name if definition else "",
            "value_type": definition.value_type if definition else "number",
            "value": value.value_num if value.value_num is not None else (value.value_text or None),
            "display": self._display(value),
            "unit": value.unit,
            "collected_at": value.collected_at.isoformat(timespec="seconds") if value.collected_at else None,
            "raw_file_id": value.raw_file_id,
            "source_ref": value.source_ref,
            "parser_version": value.parser_version,
            "result_version": value.result_version,
            "revises_id": value.revises_id,
            "superseded_by_id": value.superseded_by_id,
            "not_measured_reason": value.not_measured_reason,
            "quality": value.quality,
            "quality_label": QUALITY_LABEL.get(value.quality, value.quality),
            "review_state": value.review_state,
            "review_label": REVIEW_LABEL.get(value.review_state, value.review_state),
            "provenance": value.provenance,
            "entered_by": value.entered_by,
            "entered_by_name": entered_by.display_name if entered_by else (
                "系统来源" if value.provenance == "device" else ""
            ),
            "row_version": value.row_version,
            "flags": value.flags or [],
            "station_id": value.station_id,
            "instrument": value.instrument,
            # 正式统计要三条同时成立
            "official": value.review_state == "approved" and value.quality == "valid"
            and not value.superseded_by_id,
            "reviews": [
                {
                    "id": row.id, "reviewer_id": row.reviewer_id,
                    "reviewer_name": (
                        self.users.get(row.reviewer_id).display_name
                        if self.users.get(row.reviewer_id) else ""
                    ),
                    "conclusion": row.conclusion, "quality": row.quality, "reason": row.reason,
                    "result_version": row.result_version,
                    "decided_at": row.decided_at.isoformat(timespec="seconds"),
                }
                for row in reviews
            ],
        }

    def task_out(self, task: AnalysisTask, with_values: bool = False) -> dict:
        definitions = self.metrics.many(list(task.required_metrics or []))
        current = self.values.current_for_task(task.id)
        done, missing = collected(list(task.required_metrics or []), current)
        payload = {
            "id": task.id,
            "sample_id": task.sample_id or "",
            "physical_sample_id": task.physical_sample_id,
            "method": task.method,
            "method_version": task.method_version,
            "round_no": task.round_no,
            "state": task.state,
            "state_label": TASK_STATE_LABEL.get(task.state, task.state),
            "external_ref": task.external_ref,
            "retest_of": task.retest_of,
            "created_at": task.created_at.isoformat(timespec="seconds"),
            "row_version": task.row_version,
            "required_metrics": [
                {
                    "id": metric_id,
                    "code": definitions[metric_id].code if metric_id in definitions else metric_id,
                    "name": definitions[metric_id].name if metric_id in definitions else "",
                    "unit": definitions[metric_id].unit if metric_id in definitions else "",
                    "collected": metric_id in current,
                    "not_measured": bool(
                        metric_id in current and current[metric_id].not_measured_reason
                    ),
                }
                for metric_id in (task.required_metrics or [])
            ],
            "collected": done,
            "missing_metrics": missing,
            "review_pending": len(
                [row for row in current.values() if row.review_state == "pending"]
            ),
        }
        if with_values:
            payload["values"] = [self.value_out(row) for row in self.values.for_task(task.id)]
        return payload

    def page_tasks(self, offset: int, limit: int, state: str | None = None, sample_id: str = ""):
        rows, total = self.tasks.page(offset, limit, state, sample_id)
        return [self.task_out(row) for row in rows], total

    def task_detail(self, task_id: str) -> dict:
        task = self.tasks.get(task_id)
        if not task:
            raise NotFound("检测任务不存在")
        return self.task_out(task, with_values=True)

    def page_values(self, offset: int, limit: int, review_state: str = "", quality: str = ""):
        rows, total = self.values.page(offset, limit, review_state, quality)
        return [self.value_out(row) for row in rows], total

    def review_queue(self) -> list[dict]:
        return [self.value_out(row) for row in self.values.pending_review()]
