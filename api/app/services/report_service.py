"""正式统计与报告。

正式报告只纳入审核通过且质量有效的结果；无效或可疑的结果可以作为已审核的排除说明
出现，但不进入正式结论统计。发布时固化全部结果版本、算法版本、模板版本、文件摘要、
签名与时间——之后源结果被修订也不会改写已发布的报告。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..core.clock import now
from ..core.context import AccessContext
from ..core.db import dec
from ..core.errors import NotFound, PermissionDenied, StateConflict, ValidationFailed
from ..domain import statistics
from ..domain.access import same_person
from ..domain.statistics import EXCLUSION_REASONS, Observation, build_dataset
from ..domain.steps import KIND_NAMES
from ..models import Report, ReportVersion, User
from ..repositories.batches import AnalysisTaskRepository, BatchRepository, SampleRepository
from ..repositories.files import FileRepository
from ..repositories.governance import UserRepository
from ..repositories.materials import LotRepository, ReservationRepository
from ..repositories.metrics import MetricRepository, ResultValueRepository
from ..repositories.recipes import ExperimentTaskRepository, PlanRepository
from ..repositories.reports import ReportRepository, ReportVersionRepository
from ..repositories.samples import PhysicalSampleRepository
from ..repositories.workflow import StepRunRepository
from .audit_service import AuditService
from .file_service import FileService, FileStore
from .identity_service import IdentityService, admin_self_approval
from .inventory_service import InventoryService

ALGORITHM_VERSION = "stats-1.0"
TEMPLATE_VERSION = "fixed-1.0"
STATE_LABEL = {
    "draft": "草稿", "review": "评审中", "approved": "已批准", "published": "已发布",
    "superseded": "已被替代",
}
# 报告是给人看的文件：状态一律用中文，不把内部枚举原样印上去
ASSIGNMENT_STATE_LABEL = {
    "pending": "待执行", "running": "执行中", "done": "已完成", "failed": "失败",
}
STEP_STATE_LABEL = {
    "pending": "待执行", "ready": "待办", "running": "执行中", "waiting": "等待中",
    "completed": "已完成", "failed": "失败", "unknown": "结果未知", "cancelled": "已取消",
}


class ReportService:
    def __init__(self, db: Session, ctx: AccessContext):
        self.db = db
        self.ctx = ctx
        self.reports = ReportRepository(db, ctx)
        self.versions = ReportVersionRepository(db, ctx)
        self.tasks = ExperimentTaskRepository(db, ctx)
        self.plans = PlanRepository(db, ctx)
        self.batches = BatchRepository(db, ctx)
        self.assignments = SampleRepository(db, ctx)
        self.physical = PhysicalSampleRepository(db, ctx)
        self.analysis = AnalysisTaskRepository(db, ctx)
        self.values = ResultValueRepository(db, ctx)
        self.metrics = MetricRepository(db, ctx)
        self.runs = StepRunRepository(db, ctx)
        self.reservations = ReservationRepository(db, ctx)
        self.lots = LotRepository(db, ctx)
        self.users = UserRepository(db)
        self.files = FileRepository(db, ctx)
        self.inventory = InventoryService(db, ctx)
        self.audit = AuditService(db, ctx)
        self.identity = IdentityService(db, ctx)

    # ---------- 数据集 ----------

    def observations(self, batch_id: str) -> list[Observation]:
        """把结果明细拍成统计候选。显式绑定运行分配、轮次、指标与结果版本。"""
        assignments = {row.id: row for row in self.assignments.for_batch(batch_id)}
        tasks = self.analysis.for_batch(batch_id)
        rows: list[Observation] = []
        for task in tasks:
            for value in self.values.for_task(task.id):
                assignment = assignments.get(value.assignment_id)
                rows.append(
                    Observation(
                        assignment_id=value.assignment_id,
                        analysis_task_id=task.id,
                        round_no=task.round_no,
                        metric_id=value.metric_definition_id,
                        result_version=value.result_version,
                        value=value.value_num,
                        unit=value.unit,
                        quality=value.quality,
                        review_state=value.review_state,
                        superseded=bool(value.superseded_by_id),
                        not_measured_reason=value.not_measured_reason,
                        condition_group=assignment.condition_group if assignment else "",
                        condition_label=assignment.condition_label if assignment else "",
                        is_control=bool(assignment.is_control) if assignment else False,
                        levels=tuple(assignment.levels or []) if assignment else (),
                        repeat=assignment.repeat if assignment else 1,
                        method_version=task.method_version,
                    )
                )
        return rows

    def analysis_view(
        self, batch_id: str, metric_ids: list[str] | None = None, official: bool = True,
    ) -> dict:
        """结果分析。official=False 是探索性范围，界面与导出都要标出来。"""
        batch = self.batches.get(batch_id)
        if not batch:
            raise NotFound("批次不存在")
        rows = self.observations(batch_id)
        plan = self.plans.get(batch.plan_id)
        plan_type = plan.plan_type if plan else "matrix"
        factors = (batch.plan_snapshot or {}).get("factors") or []
        repeats = (batch.plan_snapshot or {}).get("repeats")
        available = sorted({row.metric_id for row in rows})
        chosen = [m for m in (metric_ids or available) if m in available]
        definitions = self.metrics.many(available)
        blocks = []
        for metric_id in chosen:
            definition = definitions.get(metric_id)
            if definition is not None and definition.value_type != "number":
                # 只有数值指标进入数值统计
                continue
            dataset = build_dataset(
                rows, metric_id,
                definition.name if definition else metric_id,
                definition.unit if definition else "",
                official=official,
            )
            groups = statistics.dataset_groups(dataset)
            blocks.append(
                {
                    "metric_id": metric_id,
                    "metric_name": dataset.metric_name,
                    "unit": dataset.unit,
                    "groups": groups,
                    "effects": statistics.dataset_effects(dataset, factors, plan_type),
                    "summary": statistics.dataset_summary(dataset, groups, repeats),
                    "excluded": [
                        {
                            "assignment_id": row.assignment_id,
                            "analysis_task_id": row.analysis_task_id,
                            "result_version": row.result_version,
                            "metric_name": dataset.metric_name,
                            "reason": reason,
                            "reason_label": EXCLUSION_REASONS.get(reason, reason),
                            "quality": row.quality,
                            "review_state": row.review_state,
                        }
                        for row, reason in dataset.excluded
                    ],
                }
            )
        text_metrics = [
            {
                "metric_id": metric_id,
                "metric_name": definitions[metric_id].name if metric_id in definitions else metric_id,
                "value_type": definitions[metric_id].value_type if metric_id in definitions else "text",
            }
            for metric_id in chosen
            if metric_id in definitions and definitions[metric_id].value_type != "number"
        ]
        return {
            "batch_id": batch.id,
            "plan_id": batch.plan_id,
            "plan_name": (batch.plan_snapshot or {}).get("name"),
            "plan_type": plan_type,
            "recipe_id": batch.recipe_id,
            "recipe_name": batch.recipe_snapshot.get("name"),
            "state": batch.state,
            "official": official,
            "scope_label": "正式范围（审核通过且质量有效）" if official else "探索性范围（含未审核、可疑、无效）",
            "available_metrics": [
                {
                    "id": metric_id,
                    "code": definitions[metric_id].code if metric_id in definitions else metric_id,
                    "name": definitions[metric_id].name if metric_id in definitions else metric_id,
                    "unit": definitions[metric_id].unit if metric_id in definitions else "",
                    "numeric": (
                        definitions[metric_id].value_type == "number"
                        if metric_id in definitions else True
                    ),
                }
                for metric_id in available
            ],
            "selected_metrics": chosen,
            "metrics": blocks,
            "non_numeric_metrics": text_metrics,
            "show_factor_effects": plan_type == "matrix" and bool(factors),
        }

    def compare(self, batch_ids: list[str], metric_id: str, official: bool = True) -> dict:
        """跨批次比较。指标语义、单位与方法版本不可比就拒绝比，不看条件组编号。"""
        datasets = []
        definition = self.metrics.get(metric_id)
        for batch_id in batch_ids:
            rows = self.observations(batch_id)
            datasets.append(
                (
                    batch_id,
                    build_dataset(
                        rows, metric_id,
                        definition.name if definition else metric_id,
                        definition.unit if definition else "",
                        official=official,
                    ),
                )
            )
        comparable, reason = statistics.unit_comparable([d for _, d in datasets])
        return {
            "metric_id": metric_id,
            "metric_name": definition.name if definition else metric_id,
            "unit": definition.unit if definition else "",
            "comparable": comparable,
            "reason": reason,
            "official": official,
            "batches": [
                {
                    "batch_id": batch_id,
                    "included": len(dataset.included),
                    "excluded": len(dataset.excluded),
                    "mean": statistics.mean(dataset.values),
                    "sd": statistics.stddev(dataset.values),
                    "cv_pct": statistics.cv_percent(dataset.values),
                    "method_versions": sorted(
                        {row.method_version for row in dataset.included if row.method_version}
                    ),
                }
                for batch_id, dataset in datasets
            ],
        }

    def export_rows(self, batch_id: str, official: bool = True) -> list[list]:
        view = self.analysis_view(batch_id, official=official)
        rows: list[list] = [
            [
                "batch_id", "plan_id", "scope", "metric_code", "metric_name", "unit",
                "condition_group", "condition", "control", "n_included", "n_excluded",
                "mean", "sd", "cv_pct", "data_source", "review_note",
            ]
        ]
        for block in view["metrics"]:
            for group in block["groups"]:
                rows.append(
                    [
                        view["batch_id"], view["plan_id"],
                        "official" if official else "exploratory",
                        block["metric_id"], block["metric_name"], block["unit"],
                        group["group"], group["label"], "Y" if group["is_control"] else "",
                        group["n_included"], group["n_excluded"],
                        self._num(group["mean"]), self._num(group["sd"]), self._num(group["cv_pct"]),
                        "ILCS 结果明细",
                        "仅审核通过且质量有效" if official else "含未审核 / 可疑 / 无效，不可用于正式报告",
                    ]
                )
            for excluded in block["excluded"]:
                rows.append(
                    [
                        view["batch_id"], view["plan_id"],
                        "excluded", block["metric_id"], block["metric_name"], block["unit"],
                        "", excluded["assignment_id"], "", 0, 1, "", "", "",
                        f"任务 {excluded['analysis_task_id']} v{excluded['result_version']}",
                        excluded["reason_label"],
                    ]
                )
        return rows

    @staticmethod
    def _num(value) -> str:
        return "" if value is None else f"{value:.3f}"

    # ---------- 报告 ----------

    def out(self, version: ReportVersion, detail: bool = False) -> dict:
        report = self.reports.get(version.report_id)
        author = self.users.get(version.author_id) if version.author_id else None
        approver = self.users.get(version.approver_id) if version.approver_id else None
        payload = {
            "id": version.id,
            "report_id": version.report_id,
            "code": report.code if report else "",
            "title": report.title if report else "",
            "task_id": report.task_id if report else "",
            "batch_id": report.batch_id if report else "",
            "version": version.version,
            "state": version.state,
            "state_label": STATE_LABEL.get(version.state, version.state),
            "template_version": version.template_version,
            "algorithm_version": version.algorithm_version,
            "author_id": version.author_id,
            "author_name": author.display_name if author else "",
            "approver_id": version.approver_id,
            "approver_name": approver.display_name if approver else "",
            "pdf_file_id": version.pdf_file_id,
            "supersedes_id": version.supersedes_id,
            "reject_reason": version.reject_reason,
            "submitted_at": version.submitted_at.isoformat(timespec="seconds") if version.submitted_at else None,
            "approved_at": version.approved_at.isoformat(timespec="seconds") if version.approved_at else None,
            "published_at": version.published_at.isoformat(timespec="seconds") if version.published_at else None,
            "created_at": version.created_at.isoformat(timespec="seconds"),
            "row_version": version.row_version,
            "readonly": version.state in {"published", "superseded"},
        }
        if detail:
            payload["content"] = version.content or {}
            payload["publish_snapshot"] = version.publish_snapshot or {}
            payload["audit"] = [
                {
                    "time": e.time.isoformat(timespec="seconds"), "user": e.user, "action": e.action,
                    "before": e.before, "after": e.after, "detail": e.detail, "sign": e.sign,
                }
                for e in self.audit.for_target(version.id)
            ]
        return payload

    def page(self, offset: int, limit: int, state: str | None = None):
        rows, total = self.versions.page(offset, limit, state)
        return [self.out(row) for row in rows], total

    def detail(self, version_id: str) -> dict:
        version = self.versions.get(version_id)
        if not version:
            raise NotFound("报告版本不存在")
        return self.out(version, detail=True)

    def versions_of(self, report_id: str) -> list[dict]:
        return [self.out(row) for row in self.versions.for_report(report_id)]

    def build_content(self, batch_id: str, conclusion: str = "") -> dict:
        """按固定模板组装报告内容。"""
        batch = self.batches.get(batch_id)
        if not batch:
            raise NotFound("批次不存在")
        view = self.analysis_view(batch_id, official=True)
        task = self.tasks.by_batch(batch_id)
        plan = self.plans.get(batch.plan_id)
        assignments = self.assignments.for_batch(batch_id)
        sop = batch.sop_snapshot or {}

        samples = []
        for assignment in assignments:
            physical = self.physical.get(assignment.physical_sample_id)
            samples.append(
                {
                    "id": assignment.id,
                    "barcode": physical.barcode if physical else "",
                    "source": physical.source if physical else "",
                    "sample_type": physical.sample_type if physical else "",
                    "location": (
                        physical.current_location or physical.location_note
                        if physical else ""
                    ),
                    "state": ASSIGNMENT_STATE_LABEL.get(assignment.state, assignment.state),
                }
            )

        materials = []
        for reservation in self.reservations.for_batch(batch_id):
            lot = self.lots.get(reservation.lot_id)
            state = self.inventory.state_of(reservation)
            materials.append(
                {
                    "lot_id": reservation.lot_id,
                    "material": lot.material if lot else "",
                    "qty": f"{state.authorized:f}",
                    "consumed": f"{state.consumed:f}",
                    "loss": f"{state.loss:f}",
                    "unit": reservation.unit,
                }
            )

        runs = self.runs.for_batch(batch_id)
        execution = [
            {
                "step_index": run.step_index,
                "kind_label": KIND_NAMES.get(run.kind, run.kind),
                "step_name": (run.step_snapshot or {}).get("name", ""),
                "state_label": STEP_STATE_LABEL.get(run.state, run.state),
                "note": run.reason or "",
            }
            for run in runs
        ]
        exceptions = [run.reason for run in runs if run.state in {"failed", "unknown"} and run.reason]
        if batch.failure_reason:
            exceptions.append(batch.failure_reason)

        results = []
        exclusions = []
        stats = []
        for block in view["metrics"]:
            rows = []
            for group in block["groups"]:
                for observation in group["observations"]:
                    rows.append(
                        {
                            "assignment_id": observation["assignment_id"],
                            "condition_label": group["label"],
                            "round_no": observation["round_no"],
                            "result_version": observation["result_version"],
                            "value": observation["value"],
                            "quality_label": "有效",
                            "review_label": "已通过",
                        }
                    )
            results.append(
                {"metric_name": block["metric_name"], "unit": block["unit"], "rows": rows}
            )
            exclusions.extend(block["excluded"])
            stats.append(
                {
                    "metric_name": block["metric_name"],
                    "included": block["summary"]["included"],
                    "excluded": block["summary"]["excluded"],
                    "mean": self._num(block["summary"]["mean"]),
                    "sd": self._num(block["summary"]["sd"]),
                    "cv_pct": self._num(block["summary"]["cv_pct"]),
                    "groups": block["groups"],
                    "effects": block["effects"] if view["show_factor_effects"] else [],
                }
            )

        owner = self.users.get(task.owner_user_id) if task and task.owner_user_id else None
        assignee = self.users.get(task.assignee_user_id) if task and task.assignee_user_id else None
        reviewer = self.users.get(task.reviewer_user_id) if task and task.reviewer_user_id else None
        return {
            "title": f"{(batch.plan_snapshot or {}).get('name') or batch.plan_id} 实验报告",
            "header": {"generated_at": now().isoformat(timespec="minutes")},
            "plan_section": {
                "plan": batch.plan_id,
                "plan_version": str(batch.plan_version),
                "task": task.id if task else "",
                "goal": (batch.plan_snapshot or {}).get("goal", ""),
            },
            "method_section": {
                "recipe": batch.recipe_id,
                "recipe_version": batch.recipe_snapshot.get("version", ""),
                "sop": sop.get("code", ""),
                "sop_version": sop.get("version", ""),
                "sop_checksum": sop.get("file_checksum", ""),
                "risk": batch.recipe_snapshot.get("risk", ""),
            },
            "samples": samples,
            "resources": {
                "owner": owner.display_name if owner else "",
                "assignee": assignee.display_name if assignee else batch.operator,
                "reviewer": reviewer.display_name if reviewer else "",
                "stations": sorted({run.station_id for run in runs if run.station_id}),
                "materials": materials,
            },
            "execution": execution,
            "exceptions": exceptions,
            "results": results,
            "exclusions": exclusions,
            "statistics": stats,
            "conclusion": conclusion,
            "plan_type": view["plan_type"],
            "batch_id": batch_id,
        }

    def create(self, payload: dict, user: User) -> dict:
        batch_id = payload.get("batch_id") or ""
        task_id = payload.get("task_id") or ""
        task = self.tasks.get(task_id) if task_id else None
        if task is not None and not batch_id:
            batch_id = task.batch_id
        if not batch_id:
            raise ValidationFailed("报告必须绑定一个执行批次")
        if task is None:
            task = self.tasks.by_batch(batch_id)
        content = self.build_content(batch_id, payload.get("conclusion", ""))
        report = Report(
            org_id=self.ctx.org_id, code=self.reports.next_code(),
            title=payload.get("title") or content["title"],
            task_id=task.id if task else "", plan_id=payload.get("plan_id", "") or
            (self.batches.get(batch_id).plan_id if self.batches.get(batch_id) else ""),
            batch_id=batch_id, created_by=user.id,
        )
        self.reports.add(report)
        version = ReportVersion(
            org_id=self.ctx.org_id, report_id=report.id, version=1, state="draft",
            template_version=TEMPLATE_VERSION, algorithm_version=ALGORITHM_VERSION,
            author_id=user.id, content=content,
        )
        self.versions.add(version)
        self.audit.record(
            user, "生成报告草稿", version.id, before="—", after="草稿",
            detail=f"{report.code}；批次 {batch_id}；模板 {TEMPLATE_VERSION}",
            object_version=version.row_version,
        )
        self.db.commit()
        return self.out(version, detail=True)

    def update(self, version_id: str, payload: dict, user: User) -> dict:
        version = self.versions.get(version_id)
        if not version:
            raise NotFound("报告版本不存在")
        if version.state not in {"draft"}:
            raise StateConflict(
                f"{STATE_LABEL.get(version.state, version.state)}的报告不可编辑；"
                f"退回后会形成可追溯的修改记录",
                code="report_not_editable",
            )
        self.versions.check_version(version, payload.get("row_version"), "报告版本")
        content = dict(version.content or {})
        if "conclusion" in payload:
            content["conclusion"] = payload["conclusion"]
        if payload.get("refresh"):
            refreshed = self.build_content(
                content.get("batch_id", ""), content.get("conclusion", "")
            )
            content = refreshed
        version.content = content
        self.versions.bump(version)
        self.audit.record(
            user, "编辑报告草稿", version.id, object_version=version.row_version,
            detail="重新取数" if payload.get("refresh") else "更新结论",
        )
        self.db.commit()
        return self.out(version, detail=True)

    def submit(self, version_id: str, user: User) -> dict:
        version = self.versions.get(version_id)
        if not version:
            raise NotFound("报告版本不存在")
        if version.state != "draft":
            raise StateConflict("只有草稿可以提交审核")
        blockers = self.publish_blockers(version)
        if blockers:
            raise StateConflict(
                "报告引用的结果还没有全部通过审核",
                {"blocked": [{"key": "result", "label": row} for row in blockers]},
                code="unreviewed_results",
            )
        version.state = "review"
        version.submitted_at = now()
        self.versions.bump(version)
        self.audit.record(
            user, "提交报告审核", version.id, before="草稿", after="评审中",
            object_version=version.row_version,
        )
        self.db.commit()
        return self.out(version)

    def approve(self, version_id: str, payload: dict, user: User) -> dict:
        """批准或退回。作者不能批准自己的报告。"""
        version = self.versions.get(version_id)
        if not version:
            raise NotFound("报告版本不存在")
        if version.state != "review":
            raise StateConflict("只有评审中的报告可以批准或退回")
        conclusion = payload.get("conclusion")
        if conclusion not in {"approved", "rejected"}:
            raise ValidationFailed("结论只能是 approved 或 rejected")
        if same_person(version.author_id, user.id) and not admin_self_approval(
            self.db, self.ctx, user, version.id, "批准本人编写的报告",
        ):
            raise PermissionDenied(
                "不能批准本人编写的报告（职责分离）", code="self_approval_denied"
            )
        reason = (payload.get("reason") or "").strip()
        if conclusion == "rejected":
            if not reason:
                raise ValidationFailed("退回必须写明理由")
            version.state = "draft"
            version.reject_reason = reason
            self.versions.bump(version)
            self.audit.record(
                user, "退回报告", version.id, before="评审中", after="草稿", detail=reason,
                object_version=version.row_version,
            )
            self.db.commit()
            return self.out(version)
        signature = self.identity.consume_signature(
            payload.get("signature_id"), user, "批准报告",
            object_ref=version.id, object_version=version.row_version,
        )
        version.state = "approved"
        version.approver_id = user.id
        version.approved_at = now()
        version.signature_id = signature.id
        self.versions.bump(version)
        self.audit.record(
            user, "批准报告", version.id, sign=True, meaning=signature.meaning,
            signature_id=signature.id, before="评审中", after="已批准",
            object_version=version.row_version, detail=reason,
        )
        self.db.commit()
        return self.out(version)

    def publish_blockers(self, version: ReportVersion) -> list[str]:
        """发布前必须校验引用结果全部审核通过。"""
        content = version.content or {}
        batch_id = content.get("batch_id") or ""
        if not batch_id:
            return ["报告没有绑定批次，无法校验结果审核状态"]
        blockers: list[str] = []
        for row in self.observations(batch_id):
            if row.superseded:
                continue
            if row.review_state == "pending":
                blockers.append(
                    f"任务 {row.analysis_task_id} 的指标 {row.metric_id} v{row.result_version} 待复核"
                )
        return sorted(set(blockers))[:10]

    def publish(self, version_id: str, payload: dict, user: User) -> dict:
        version = self.versions.get(version_id)
        if not version:
            raise NotFound("报告版本不存在")
        if version.state != "approved":
            raise StateConflict("只有已批准的报告可以发布")
        blockers = self.publish_blockers(version)
        if blockers:
            raise StateConflict(
                "存在未审核的引用结果，禁止发布正式报告",
                {"blocked": [{"key": "result", "label": row} for row in blockers]},
                code="unreviewed_results",
            )
        signature = self.identity.consume_signature(
            payload.get("signature_id"), user, "发布报告",
            object_ref=version.id, object_version=version.row_version,
        )
        report = self.reports.get(version.report_id)
        content = dict(version.content or {})
        batch_id = content.get("batch_id", "")
        result_versions = [
            f"{row.analysis_task_id}:{row.metric_id}:v{row.result_version}"
            for row in self.observations(batch_id)
            if not row.superseded
        ]
        content["header"] = {
            "code": report.code if report else "",
            "version": version.version,
            "organization": self.ctx.org_id,
            "published_at": now().isoformat(timespec="minutes"),
        }
        content["approval"] = {
            "author": (
                self.users.get(version.author_id).display_name
                if self.users.get(version.author_id) else ""
            ),
            "approver": user.display_name,
            "signature_meaning": signature.meaning,
            "published_at": now().isoformat(timespec="minutes"),
            "template_version": version.template_version,
            "algorithm_version": version.algorithm_version,
            "result_versions": f"{len(result_versions)} 条已固化",
        }
        version.content = content

        pdf_bytes = self._render_pdf(content)
        file_service = FileService(self.db, self.ctx, FileStore())
        import io

        pdf_record = file_service.upload(
            f"{report.code if report else 'report'}-v{version.version}.pdf",
            "application/pdf", io.BytesIO(pdf_bytes), user,
            ref_type="report_version", ref_id=version.id, note="发布时固化的报告 PDF",
        )
        # 上一版发布报告标为已替代，原 PDF 与引用不变
        previous = [
            row for row in self.versions.for_report(version.report_id)
            if row.id != version.id and row.state == "published"
        ]
        for row in previous:
            row.state = "superseded"
            row.row_version = int(row.row_version or 0) + 1
            version.supersedes_id = row.id

        version.state = "published"
        version.published_at = now()
        version.pdf_file_id = pdf_record["id"]
        version.signature_id = signature.id
        version.publish_snapshot = {
            "result_versions": result_versions,
            "algorithm_version": version.algorithm_version,
            "template_version": version.template_version,
            "pdf_file_id": pdf_record["id"],
            "pdf_checksum": pdf_record["checksum"],
            "signature_id": signature.id,
            "published_at": version.published_at.isoformat(timespec="seconds"),
            "supersedes": version.supersedes_id,
        }
        self.versions.bump(version)
        self.audit.record(
            user, "发布报告", version.id, sign=True, meaning=signature.meaning,
            signature_id=signature.id, before="已批准", after="已发布",
            object_version=version.row_version,
            detail=(
                f"固化 {len(result_versions)} 条结果版本；算法 {version.algorithm_version}；"
                f"模板 {version.template_version}；PDF 摘要 {pdf_record['checksum'][:16]}…"
                + (f"；替代 {version.supersedes_id}" if version.supersedes_id else "")
            ),
        )
        self.db.commit()
        return self.out(version, detail=True)

    def revise(self, version_id: str, user: User) -> dict:
        """源结果被修订后发布新报告。原 PDF 与引用不变，新报告标明替代关系。"""
        published = self.versions.get(version_id)
        if not published:
            raise NotFound("报告版本不存在")
        if published.state != "published":
            raise StateConflict("只有已发布的报告才需要通过新版本替代")
        content = self.build_content(
            (published.content or {}).get("batch_id", ""),
            (published.content or {}).get("conclusion", ""),
        )
        version = ReportVersion(
            org_id=self.ctx.org_id, report_id=published.report_id,
            version=published.version + 1, state="draft", template_version=TEMPLATE_VERSION,
            algorithm_version=ALGORITHM_VERSION, author_id=user.id, content=content,
            supersedes_id=published.id,
        )
        self.versions.add(version)
        self.audit.record(
            user, "创建报告新版本", version.id, before="—", after="草稿",
            detail=f"基于 v{published.version} 重新取数；原 PDF 与引用不变",
            object_version=version.row_version,
        )
        self.db.commit()
        return self.out(version, detail=True)

    @staticmethod
    def _render_pdf(content: dict) -> bytes:
        from .report_pdf import render

        return render(content)

    def pdf_file(self, version_id: str, user: User):
        version = self.versions.get(version_id)
        if not version:
            raise NotFound("报告版本不存在")
        if not version.pdf_file_id:
            raise StateConflict("该版本还没有发布 PDF", code="pdf_not_ready")
        return FileService(self.db, self.ctx).open_for_download(version.pdf_file_id, user)
