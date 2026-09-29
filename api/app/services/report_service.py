"""正式统计与报告。

正式报告只纳入审核通过且质量有效的结果；无效或可疑的结果可以作为已审核的排除说明
出现，但不进入正式结论统计。发布时固化全部结果版本、算法版本、模板版本、文件摘要、
签名与时间——之后源结果被修订也不会改写已发布的报告。
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy.orm import Session

from ..core.clock import now
from ..core.context import AccessContext
from ..core.db import dec
from ..core.errors import NotFound, PermissionDenied, StateConflict, ValidationFailed
from ..domain import report_templates, statistics
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
from ..repositories.resources import station_model
from ..repositories.samples import PhysicalSampleRepository
from ..repositories.workflow import StepRunRepository
from .audit_service import AuditService
from .file_service import FileService, FileStore
from .identity_service import IdentityService, admin_self_approval
from .inventory_service import InventoryService

ALGORITHM_VERSION = "stats-1.0"
TEMPLATE_VERSION = report_templates.template_version(report_templates.DEFAULT)
OPERATION_LOG_LIMIT = 300
STATE_LABEL = {
    "draft": "草稿", "review": "评审中", "approved": "已批准", "published": "已发布",
    "superseded": "已被替代",
}
# 报告是给人看的文件：状态一律用中文，不把内部枚举原样印上去
ASSIGNMENT_STATE_LABEL = {
    "pending": "待执行", "running": "执行中", "done": "已完成", "failed": "失败",
}
BATCH_STATE_LABEL = {
    "planned": "计划", "scheduled": "已排程", "running": "运行中", "paused": "已保持",
    "fault": "故障", "aborting": "终止中", "aborted": "已终止", "done": "已完成",
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
                        batch_id=batch_id,
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
        return self._view([batch], metric_ids, official)

    def task_batches(self, task_id: str) -> tuple:
        """父任务（一个方案分多批执行）下已经建了批次的叶子：[(子任务, 批次)]，按份额顺序。"""
        from .task_service import TaskService

        task = self.tasks.get(task_id)
        if task is None:
            raise NotFound("实验任务不存在")
        if not self.tasks.children(task.id):
            raise StateConflict("任务没有拆分：按批次看结果", code="task_not_split")
        leaves = [
            (leaf, batch) for leaf, batch in TaskService(self.db, self.ctx).leaf_batches([task.id])
            if batch is not None
        ]
        leaves.sort(key=lambda pair: (pair[0].purpose == "retest", int((pair[0].portion or {}).get("offset") or 0),
                                      pair[1].created_at))
        return task, leaves

    def task_analysis_view(
        self, task_id: str, metric_ids: list[str] | None = None, official: bool = True,
    ) -> dict:
        """父任务的合并结果：各叶子批次的观测合在一起，给出合并统计、分批明细与批次差异。"""
        task, leaves = self.task_batches(task_id)
        if not leaves:
            raise StateConflict("子任务还没有批次", code="no_batches")
        return self._view([batch for _, batch in leaves], metric_ids, official, task=task, leaves=leaves)

    def _view(self, batches: list, metric_ids: list[str] | None, official: bool, *, task=None, leaves=None) -> dict:
        batch = batches[0]
        multi = task is not None
        # 合并时终止的批次只列在分批明细里：它的样本按失败计，数据不进合并统计
        pooled = [item for item in batches if not (multi and item.state == "aborted")] or batches
        rows = [row for item in pooled for row in self.observations(item.id)]
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
            extra: dict = {}
            if multi:
                # 合并前先看可比性：各批回传的单位或检测方法版本不一致就不合并
                units = sorted({row.unit for row in dataset.included if row.unit})
                methods = sorted({row.method_version for row in dataset.included if row.method_version})
                comparable, reason = True, ""
                if len(units) > 1:
                    comparable, reason = False, f"各批单位不一致：{'、'.join(units)}"
                elif len(methods) > 1:
                    comparable, reason = False, f"各批检测方法版本不一致：{'、'.join(methods)}"
                extra = {
                    "comparable": comparable, "comparable_reason": reason,
                    "by_batch": statistics.batch_breakdown(dataset),
                    "batch_effect": statistics.batch_effect(dataset) if comparable else None,
                }
            blocks.append(
                {
                    "metric_id": metric_id,
                    "metric_name": dataset.metric_name,
                    "unit": dataset.unit,
                    "groups": groups,
                    "effects": statistics.dataset_effects(dataset, factors, plan_type),
                    "summary": statistics.dataset_summary(dataset, groups, repeats),
                    **extra,
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
        scope = {}
        if multi:
            from .task_service import TaskService

            tasks = TaskService(self.db, self.ctx)
            scope = {
                "task_id": task.id, "task_title": task.title, "batch_ids": [item.id for item in pooled],
                "batches": [
                    {
                        "batch_id": item.id, "task_id": leaf.id, "title": leaf.title, "state": item.state,
                        "state_label": BATCH_STATE_LABEL.get(item.state, item.state),
                        "purpose": leaf.purpose or "",
                        "portion_label": tasks.part_label(
                            leaf.portion or {}, len(self.assignments.for_batch(item.id)), plan_type == "matrix",
                        ) if leaf.portion else "",
                        "samples": len(self.assignments.for_batch(item.id)),
                        "recipe_version": item.recipe_snapshot.get("version", ""),
                    }
                    for leaf, item in leaves
                ],
                "progress": tasks.progress(task),
            }
        return {
            **scope,
            "batch_id": "" if multi else batch.id,
            "plan_id": batch.plan_id,
            "plan_name": (batch.plan_snapshot or {}).get("name"),
            "plan_type": plan_type,
            "recipe_id": batch.recipe_id,
            "recipe_name": batch.recipe_snapshot.get("name"),
            "state": batch.state if not multi else (
                "done" if all(item.state in {"done", "aborted"} for item in batches) else "running"
            ),
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

    def build_content(self, batch_id: str, conclusion: str = "", template_key: str | None = None) -> dict:
        """组装报告内容。取数只有这一套；模板只决定渲染哪些章节、按什么顺序。"""
        batch = self.batches.get(batch_id)
        if not batch:
            raise NotFound("批次不存在")
        view = self.analysis_view(batch_id, official=True)
        task = self.tasks.by_batch(batch_id)
        parts = self._batch_parts(batch)
        results, exclusions, stats = self._result_sections(view)
        sop = batch.sop_snapshot or {}
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
            "samples": parts["samples"],
            "resources": {
                "owner": owner.display_name if owner else "",
                "assignee": assignee.display_name if assignee else batch.operator,
                "reviewer": reviewer.display_name if reviewer else "",
                "stations": parts["stations"],
                "materials": parts["materials"],
            },
            "execution": parts["execution"],
            "exceptions": parts["exceptions"],
            "instruments": parts["instruments"],
            "operation_log": parts["operation_log"],
            "raw_files": parts["raw_files"],
            "data_flags": parts["data_flags"],
            "results": results,
            "exclusions": exclusions,
            "statistics": stats,
            "conclusion": conclusion,
            "plan_type": view["plan_type"],
            "batch_id": batch_id,
            "template": report_templates.template(template_key),
        }

    def build_task_content(self, task_id: str, conclusion: str = "", template_key: str | None = None) -> dict:
        """父任务（一个方案分多批执行）的一份合并报告：合并统计、分批明细与批次差异、短缺与放弃记录。

        各章节按批次汇总（执行、异常、操作记录前面标批次号），结果与统计用全部批次的正式观测一起算。
        """
        from .task_service import TaskService

        task, leaves = self.task_batches(task_id)
        if not leaves:
            raise StateConflict("子任务还没有批次，无法出报告", code="no_batches")
        batches = [batch for _, batch in leaves]
        view = self.task_analysis_view(task_id, official=True)
        results, exclusions, stats = self._result_sections(view)
        first = batches[0]
        plan = self.plans.get(task.plan_id)
        merged: dict[str, list] = {key: [] for key in (
            "samples", "materials", "execution", "exceptions", "operation_log", "raw_files", "data_flags",
        )}
        instruments: dict[str, dict] = {}
        stations: set[str] = set()
        for batch in batches:
            parts = self._batch_parts(batch, label=batch.id)
            for key in merged:
                merged[key].extend(parts[key])
            stations.update(parts["stations"])
            for row in parts["instruments"]:
                current = instruments.setdefault(row["station_id"], {**row, "steps": [], "methods": []})
                current["steps"] = current["steps"] + row["steps"]
                current["methods"] = sorted(set(current["methods"]) | set(row["methods"]))
        recipes = sorted({f"{batch.recipe_id} v{batch.recipe_snapshot.get('version', '')}" for batch in batches})
        sops = sorted({
            f"{(batch.sop_snapshot or {}).get('code', '')} {(batch.sop_snapshot or {}).get('version', '')}".strip()
            for batch in batches if batch.sop_snapshot
        })
        owner = self.users.get(task.owner_user_id) if task.owner_user_id else None
        reviewer = self.users.get(task.reviewer_user_id) if task.reviewer_user_id else None
        assignees = []
        for leaf, _ in leaves:
            person = self.users.get(leaf.assignee_user_id) if leaf.assignee_user_id else None
            if person is not None and person.display_name not in assignees:
                assignees.append(person.display_name)
        progress = TaskService(self.db, self.ctx).progress(task)
        return {
            "title": f"{(first.plan_snapshot or {}).get('name') or task.plan_id} 实验报告（{len(batches)} 批合并）",
            "header": {"generated_at": now().isoformat(timespec="minutes")},
            "plan_section": {
                "plan": task.plan_id,
                "plan_version": "、".join(sorted({str(batch.plan_version) for batch in batches})),
                "task": task.id,
                "goal": (first.plan_snapshot or {}).get("goal", "") or (plan.goal if plan else ""),
            },
            "method_section": {
                "recipe": "、".join(sorted({batch.recipe_id for batch in batches})),
                "recipe_version": "、".join(recipes),
                "sop": "、".join(sops),
                "sop_version": "",
                "sop_checksum": "、".join(sorted({
                    (batch.sop_snapshot or {}).get("file_checksum", "") for batch in batches if batch.sop_snapshot
                } - {""})),
                "risk": first.recipe_snapshot.get("risk", ""),
            },
            "samples": merged["samples"],
            "resources": {
                "owner": owner.display_name if owner else "",
                "assignee": "、".join(assignees) or first.operator,
                "reviewer": reviewer.display_name if reviewer else "",
                "stations": sorted(stations),
                "materials": merged["materials"],
            },
            "execution": merged["execution"],
            "exceptions": merged["exceptions"],
            "instruments": [instruments[key] for key in sorted(instruments)],
            "operation_log": sorted(merged["operation_log"], key=lambda row: row["time"])[-OPERATION_LOG_LIMIT:],
            "raw_files": merged["raw_files"],
            "data_flags": merged["data_flags"],
            "results": results,
            "exclusions": exclusions,
            "statistics": stats,
            "batches": {
                "rows": view.get("batches") or [],
                "progress": {key: progress[key] for key in (
                    "target", "valid", "failed", "running", "pending", "descoped", "retest", "accepted", "shortfall",
                )},
                "decisions": list(task.shortfall_decisions or []),
                "metrics": [
                    {
                        "metric_name": block["metric_name"], "unit": block["unit"],
                        "comparable": block.get("comparable", True),
                        "comparable_reason": block.get("comparable_reason", ""),
                        "by_batch": [
                            {**row, "mean": self._num(row["mean"]), "sd": self._num(row["sd"]),
                             "cv_pct": self._num(row["cv_pct"])}
                            for row in block.get("by_batch") or []
                        ],
                        "batch_effect": self._effect_out(block.get("batch_effect")),
                    }
                    for block in view["metrics"]
                ],
            },
            "conclusion": conclusion,
            "plan_type": view["plan_type"],
            "task_id": task.id,
            "batch_id": "",
            # 报告引用的批次：终止的批次只在「分批情况」里列出，不进结果、不参与发布前校验与固化
            "batch_ids": view.get("batch_ids") or [batch.id for batch in batches],
            "template": report_templates.template(template_key),
        }

    def _effect_out(self, effect: dict | None) -> dict | None:
        if not effect:
            return None
        p_value = effect.get("p")
        return {**effect, "f": self._num(effect.get("f")), "p": "" if p_value is None else f"{p_value:.3g}"}

    @staticmethod
    def content_batch_ids(content: dict) -> list[str]:
        """报告引用的批次：多批合并报告是 batch_ids，单批报告是 batch_id。"""
        return list(content.get("batch_ids") or ([content["batch_id"]] if content.get("batch_id") else []))

    def _rebuild(self, content: dict, conclusion: str, template_key: str | None) -> dict:
        """按报告原来的范围重新取数：多批合并报告按父任务，单批报告按批次。"""
        if content.get("batch_ids") and content.get("task_id"):
            return self.build_task_content(content["task_id"], conclusion, template_key)
        return self.build_content(content.get("batch_id", ""), conclusion, template_key)

    def _require_task_reportable(self, task_id: str) -> None:
        """父任务出合并报告前：批次都跑完了、短缺已处置、各批可以合并。"""
        from .task_service import TaskService

        task, leaves = self.task_batches(task_id)
        tasks = TaskService(self.db, self.ctx)
        blocked: list[str] = []
        running = [batch.id for _, batch in leaves if batch.state not in {"done", "aborted"}]
        if running:
            blocked.append(f"批次 {'、'.join(running)} 还没运行结束")
        waiting = [
            leaf.id for leaf, batch in tasks.leaf_batches([task.id]) if batch is None and leaf.state != "cancelled"
        ]
        if waiting:
            blocked.append(f"子任务 {'、'.join(waiting)} 还没有批次")
        progress = tasks.progress(task)
        if progress["shortfall"] > 0:
            blocked.append(f"计划 {progress['target']} 个样本，还短缺 {progress['shortfall']} 个：先补测或签名放弃")
        if not [batch for _, batch in leaves if batch.state == "done"]:
            blocked.append("没有运行结束的批次")
        if not blocked:
            view = self.task_analysis_view(task_id, official=True)
            blocked += [
                f"{block['metric_name']}：{block['comparable_reason']}，不能合并出一份报告；请分批出报告"
                for block in view["metrics"] if not block.get("comparable", True)
            ]
        if blocked:
            raise StateConflict(
                "父任务还不能出合并报告", {"blocked": [{"key": "task", "label": text} for text in blocked]},
                code="task_not_reportable",
            )

    def _result_sections(self, view: dict) -> tuple[list, list, list]:
        """结果表、排除说明与统计三节：单批与多批合并同一套。"""
        results, exclusions, stats = [], [], []
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
            results.append({"metric_name": block["metric_name"], "unit": block["unit"], "rows": rows})
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
        return results, exclusions, stats

    def _batch_parts(self, batch, label: str = "") -> dict:
        """一个批次的样本、物料、执行、异常、仪器、原始文件、数据标记与操作记录。

        `label` 给了（多批合并报告）就在执行、异常、操作记录、物料与标记前面标上批次号，读的人分得清是哪一批。
        """
        tag = f"{label} · " if label else ""
        samples = []
        for assignment in self.assignments.for_batch(batch.id):
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
                    **({"batch_id": batch.id} if label else {}),
                }
            )

        materials = []
        for reservation in self.reservations.for_batch(batch.id):
            lot = self.lots.get(reservation.lot_id)
            state = self.inventory.state_of(reservation)
            materials.append(
                {
                    "lot_id": reservation.lot_id,
                    "material": (lot.material if lot else "") + (f"（{label}）" if label else ""),
                    "qty": f"{state.authorized:f}",
                    "consumed": f"{state.consumed:f}",
                    "loss": f"{state.loss:f}",
                    "unit": reservation.unit,
                }
            )

        runs = self.runs.for_batch(batch.id)
        feed = self._binding_notes(batch.id, runs)
        execution = [
            {
                "step_index": run.step_index,
                "kind_label": KIND_NAMES.get(run.kind, run.kind),
                "step_name": tag + (run.step_snapshot or {}).get("name", ""),
                "state_label": STEP_STATE_LABEL.get(run.state, run.state),
                "note": "；".join(part for part in (run.reason or "", feed.get(run.id, "")) if part),
            }
            for run in runs
        ]
        exceptions = [f"{tag}{run.reason}" for run in runs if run.state in {"failed", "unknown"} and run.reason]
        if batch.failure_reason:
            exceptions.append(f"{tag}{batch.failure_reason}")
        instruments = self._instruments(batch, runs)
        if label:
            for row in instruments:
                row["steps"] = [f"{label} {name}" for name in row["steps"]]
        raw_files, data_flags = self._raw_files_and_flags(batch.id, runs)
        if label:
            for flag in data_flags:
                flag["target"] = f"{label} {flag['target']}"
        operation_log = self._operation_log(batch, runs)
        if label:
            for row in operation_log:
                row["action"] = f"{label} {row['action']}"
        return {
            "samples": samples, "materials": materials, "execution": execution, "exceptions": exceptions,
            "instruments": instruments, "raw_files": raw_files, "data_flags": data_flags,
            "operation_log": operation_log, "stations": sorted({run.station_id for run in runs if run.station_id}),
        }

    def _binding_notes(self, batch_id: str, runs) -> dict[str, str]:
        """取自上游结果的参数（前馈）在执行记录里写明来源：参数 ← 来源步骤.字段 × 系数，样本数与计算值范围。

        来源记录后来被重做取代（返工、回环、从指定节点重做）时照样写出，并注明已被取代：
        已执行的设备动作用的是当时的值，不会因为来源重做而改写。每个样本的明细在批次页的指令记录里。
        """
        from ..domain.bindings import decimal_text
        from ..repositories.execution import CommandRepository

        run_states = {run.id: run.state for run in runs}
        notes: dict[str, list[str]] = {}
        for command in CommandRepository(self.db, self.ctx).for_batch(batch_id):
            records = [row for row in (command.bindings or []) if isinstance(row, dict)]
            if not records or not command.step_run_id or command.state != "done":
                continue
            by_param: dict[str, list[dict]] = {}
            for row in records:
                by_param.setdefault(str(row.get("param") or ""), []).append(row)
            for rows in by_param.values():
                first = rows[0]
                values = sorted(Decimal(str(row.get("value") or "0")) for row in rows)
                low, high = decimal_text(values[0]), decimal_text(values[-1])
                span = low if low == high else f"{low}–{high}"
                coefficient = (
                    f" × 因子「{str(first.get('coefficient_source'))[7:]}」" if str(first.get("coefficient_source") or "").startswith("factor:")
                    else f" × {first.get('coefficient')} {first.get('coefficient_unit')}" if first.get("coefficient")
                    else ""
                )
                replaced = any(
                    run_states.get(row.get("source_ref")) == "superseded"
                    or self._checkpoint_superseded(row.get("source_ref"), run_states)
                    for row in rows
                )
                notes.setdefault(command.step_run_id, []).append(
                    f"{first.get('label') or first.get('param')} ← {first.get('source_name')}.{first.get('field')}"
                    f"（{first.get('unit')}）{coefficient}：{len(rows) if first.get('sample_id') else '整批'}"
                    f"{' 个样本' if first.get('sample_id') else ''}，{span} {first.get('target_unit')}"
                    + ("；所依据的来源记录后来已被重做取代" if replaced else "")
                )
        return {run_id: "；".join(parts) for run_id, parts in notes.items()}

    def _checkpoint_superseded(self, checkpoint_id, run_states: dict[str, str]) -> bool:
        from ..models import Checkpoint

        if not checkpoint_id:
            return False
        checkpoint = self.db.get(Checkpoint, checkpoint_id)
        return bool(checkpoint and run_states.get(checkpoint.step_run_id) == "superseded")

    # ---------- 报告补充章节 ----------

    def _instruments(self, batch, runs) -> list[dict]:
        """执行用到的工位：台账（型号、厂商、序列号、固件）、校准许可、驱动与设备自报、按哪版设备方法执行。"""
        from ..domain.resources import governing_calibration
        from ..models import Adapter, Station
        from .asset_service import AssetService

        assets = AssetService(self.db, self.ctx)
        methods_by_station: dict[str, set[str]] = {}
        steps_by_station: dict[str, list[str]] = {}
        for run in runs:
            if not run.station_id:
                continue
            snapshot = run.step_snapshot or {}
            method = snapshot.get("method") or {}
            if method.get("code"):
                methods_by_station.setdefault(run.station_id, set()).add(
                    f"{method['code']} v{method.get('version', '')}（程序 {method.get('program') or '—'}）"
                )
            steps_by_station.setdefault(run.station_id, []).append(snapshot.get("name") or f"第 {run.step_index + 1} 步")
        rows = []
        for station_id in sorted(steps_by_station):
            station = self.db.get(Station, station_id)
            adapter = self.db.get(Adapter, station_id)
            asset = assets.assets.get(station.asset_id) if station is not None and station.asset_id else None
            calibration = "—"
            if asset is not None:
                governing = governing_calibration(assets.spec_for(asset), "", batch.created_at or now())
                if governing is not None:
                    calibration = (
                        f"{'合格' if governing.result == 'pass' else '不合格'}，"
                        f"有效期至 {governing.expires_at:%Y-%m-%d}" if governing.expires_at else
                        f"{'合格' if governing.result == 'pass' else '不合格'}"
                    )
                elif not asset.calibration_applicable:
                    calibration = f"不适用：{asset.calibration_exempt_reason}"
            rows.append({
                "station_id": station_id,
                "name": station.name if station is not None else station_id,
                "model": station_model(station, asset) if station is not None else (asset.model if asset else ""),
                "asset_no": asset.asset_no if asset else "",
                "vendor": (asset.vendor if asset else "") or (adapter.vendor if adapter else ""),
                "serial": asset.serial if asset else "",
                "firmware": (adapter.firmware if adapter and adapter.firmware else "") or (asset.firmware if asset else ""),
                "driver": f"{adapter.protocol} {adapter.version}".strip() if adapter else "",
                "kind": ("真实设备" if adapter.kind == "real" else "模拟器") if adapter else "",
                "calibration": calibration,
                "methods": sorted(methods_by_station.get(station_id, set())),
                "steps": steps_by_station[station_id],
            })
        return rows

    def _raw_files_and_flags(self, batch_id: str, runs) -> tuple[list[dict], list[dict]]:
        """原始数据文件（带摘要，可据此核对原件未被替换）与自动打标（越界、逻辑冲突、设备输出不符）。"""
        files: dict[str, dict] = {}

        def attach(file_id: str, usage: str) -> None:
            if not file_id:
                return
            if file_id not in files:
                record = self.files.get(file_id)
                if record is None:
                    return
                files[file_id] = {
                    "id": record.id, "filename": record.filename, "media_type": record.media_type,
                    "size": int(record.byte_size or 0), "checksum": record.checksum, "usage": [],
                }
            if usage not in files[file_id]["usage"]:
                files[file_id]["usage"].append(usage)

        flags: list[dict] = []
        for record in self.files.for_ref("batch", batch_id):
            attach(record.id, "批次附件")
        # 检测任务的取数口径与统计（observations）一致：本批次的检测任务
        for task in self.analysis.for_batch(batch_id):
            for record in self.files.for_ref("analysis_task", task.id):
                attach(record.id, f"检测任务 {task.id[:8]} 附件")
            for value in self.values.for_task(task.id):
                if value.superseded_by_id:
                    continue
                definition = self.metrics.get(value.metric_definition_id)
                code = definition.code if definition else value.metric_definition_id
                attach(value.raw_file_id, f"{code} v{value.result_version} 原始数据")
                for flag in value.flags or []:
                    flags.append({
                        "scope": "结果", "target": f"{value.assignment_id or task.physical_sample_id} · {code} v{value.result_version}",
                        "code": flag.get("code", ""), "message": flag.get("message", ""),
                        "quality": value.quality, "review_state": value.review_state,
                    })
        for run in runs:
            for flag in run.flags or []:
                flags.append({
                    "scope": "设备回报",
                    "target": f"第 {run.step_index + 1} 步 {(run.step_snapshot or {}).get('name', '')}",
                    "code": flag.get("code", ""), "message": flag.get("message", ""),
                    "quality": "", "review_state": "",
                })
        return sorted(files.values(), key=lambda row: row["filename"]), flags

    def _operation_log(self, batch, runs) -> list[dict]:
        """操作记录：批次与各步骤执行上的审计事件，按时间排序（签名事件标明含义）。"""
        targets = [batch.id, *{run.id for run in runs}]
        events = [event for target in targets for event in self.audit.for_target(target)]
        events.sort(key=lambda event: event.time)
        return [
            {
                "time": event.time.isoformat(timespec="seconds"), "user": event.user, "action": event.action,
                "before": event.before, "after": event.after, "detail": (event.detail or "")[:200],
                "signed": bool(event.sign), "meaning": event.meaning,
            }
            for event in events[-OPERATION_LOG_LIMIT:]
        ]

    def create(self, payload: dict, user: User) -> dict:
        batch_id = payload.get("batch_id") or ""
        task_id = payload.get("task_id") or ""
        task = self.tasks.get(task_id) if task_id else None
        if task_id and task is None:
            raise NotFound("实验任务不存在")
        template = report_templates.template(payload.get("template"))
        if task is not None and not batch_id and self.tasks.children(task.id):
            # 一个方案分多批执行：在父任务上出一份合并报告，子任务不必各出一份
            self._require_task_reportable(task.id)
            content = self.build_task_content(task.id, payload.get("conclusion", ""), template["key"])
            report = Report(
                org_id=self.ctx.org_id, code=self.reports.next_code(),
                title=payload.get("title") or content["title"], task_id=task.id,
                plan_id=payload.get("plan_id", "") or task.plan_id, batch_id="", created_by=user.id,
            )
            return self._save_draft(report, content, template, user, f"父任务 {task.id}（{len(content['batch_ids'])} 批）")
        if task is not None and not batch_id:
            batch_id = task.batch_id
        if not batch_id:
            raise ValidationFailed("报告必须绑定一个执行批次，或一个已拆分的父任务")
        if task is None:
            task = self.tasks.by_batch(batch_id)
        content = self.build_content(batch_id, payload.get("conclusion", ""), template["key"])
        report = Report(
            org_id=self.ctx.org_id, code=self.reports.next_code(),
            title=payload.get("title") or content["title"],
            task_id=task.id if task else "", plan_id=payload.get("plan_id", "") or
            (self.batches.get(batch_id).plan_id if self.batches.get(batch_id) else ""),
            batch_id=batch_id, created_by=user.id,
        )
        return self._save_draft(report, content, template, user, f"批次 {batch_id}")

    def _save_draft(self, report: Report, content: dict, template: dict, user: User, scope: str) -> dict:
        self.reports.add(report)
        version = ReportVersion(
            org_id=self.ctx.org_id, report_id=report.id, version=1, state="draft",
            template_version=report_templates.template_version(template["key"]), algorithm_version=ALGORITHM_VERSION,
            author_id=user.id, content=content,
        )
        self.versions.add(version)
        self.audit.record(
            user, "生成报告草稿", version.id, before="—", after="草稿",
            detail=f"{report.code}；{scope}；模板 {template['name']} {version.template_version}",
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
            refreshed = self._rebuild(
                content, content.get("conclusion", ""),
                payload.get("template") or (content.get("template") or {}).get("key"),
            )
            content = refreshed
            version.template_version = report_templates.template_version(content["template"]["key"])
        elif payload.get("template"):
            # 换模板不重新取数：只换章节选择
            content["template"] = report_templates.template(payload["template"])
            version.template_version = report_templates.template_version(content["template"]["key"])
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
        batch_ids = self.content_batch_ids(content)
        if not batch_ids:
            return ["报告没有绑定批次，无法校验结果审核状态"]
        blockers: list[str] = []
        for batch_id in batch_ids:
            for row in self.observations(batch_id):
                if row.superseded:
                    continue
                if row.review_state == "pending":
                    blockers.append(
                        f"任务 {row.analysis_task_id} 的指标 {row.metric_id} v{row.result_version} 待复核"
                        + (f"（批次 {batch_id}）" if len(batch_ids) > 1 else "")
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
        result_versions = [
            f"{row.analysis_task_id}:{row.metric_id}:v{row.result_version}"
            for batch_id in self.content_batch_ids(content)
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
        content = self._rebuild(
            published.content or {},
            (published.content or {}).get("conclusion", ""),
            ((published.content or {}).get("template") or {}).get("key"),
        )
        version = ReportVersion(
            org_id=self.ctx.org_id, report_id=published.report_id,
            version=published.version + 1, state="draft",
            template_version=report_templates.template_version(content["template"]["key"]),
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
