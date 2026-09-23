"""任务中心。

任务状态是派生的，不是能 PATCH 的字段：执行阶段从 Batch / StepRun 来，
数据阶段从检测任务与结果审核来，报告阶段从报告版本来。手工只能改「谁做、什么时候做」。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..core.clock import now
from ..core.context import AccessContext
from ..core.errors import NotFound, PermissionDenied, StateConflict, ValidationFailed
from ..models import ExperimentTask, TaskAssignment, User
from ..repositories.batches import AnalysisTaskRepository, BatchRepository, SampleRepository
from ..repositories.governance import UserRepository
from ..repositories.metrics import ResultValueRepository
from ..repositories.recipes import (
    ExperimentTaskRepository, PlanRepository, PlanVersionRepository, TaskAssignmentRepository,
)
from ..repositories.reports import ReportRepository, ReportVersionRepository
from ..repositories.workflow import StepRunRepository
from .audit_service import AuditService
from .people_service import PeopleService

STATES = (
    "unassigned", "pending_accept", "accepted", "running", "data_review", "reporting",
    "done", "cancelled",
)
STATE_LABEL = {
    "unassigned": "待分配", "pending_accept": "待接单", "accepted": "已接单",
    "running": "执行中", "data_review": "待数据复核", "reporting": "待报告",
    "done": "完成", "cancelled": "已取消",
}
ACTION_LABEL = {"assign": "分配", "reassign": "转派", "accept": "接单", "cancel": "取消"}


class TaskService:
    def __init__(self, db: Session, ctx: AccessContext):
        self.db = db
        self.ctx = ctx
        self.tasks = ExperimentTaskRepository(db, ctx)
        self.assignments = TaskAssignmentRepository(db)
        self.plans = PlanRepository(db, ctx)
        self.plan_versions = PlanVersionRepository(db, ctx)
        self.batches = BatchRepository(db, ctx)
        self.runs = StepRunRepository(db, ctx)
        self.analysis = AnalysisTaskRepository(db, ctx)
        self.samples = SampleRepository(db, ctx)
        self.values = ResultValueRepository(db, ctx)
        self.reports = ReportRepository(db, ctx)
        self.report_versions = ReportVersionRepository(db, ctx)
        self.users = UserRepository(db)
        self.people = PeopleService(db, ctx)
        self.audit = AuditService(db, ctx)

    # ---------- 状态派生 ----------

    def derive_state(self, task: ExperimentTask) -> str:
        """只有「待分配 / 待接单 / 已接单 / 已取消」是任务自己的状态，其余从对象派生。"""
        if task.state == "cancelled":
            return "cancelled"
        batch = self.batches.get(task.batch_id) if task.batch_id else None
        if batch is None:
            if not task.assignee_user_id:
                return "unassigned"
            return "accepted" if task.accepted_at else "pending_accept"
        if batch.state not in {"done", "aborted"}:
            return "running"
        if batch.state == "aborted":
            return "cancelled"
        # 批次 done 只表示运行结束，任务继续往数据与报告阶段走
        analysis_tasks = self.analysis.for_batch(batch.id)
        if analysis_tasks:
            task_ids = [row.id for row in analysis_tasks]
            values = self.values.for_tasks(task_ids)
            live = [row for row in values if not row.superseded_by_id]
            if not live or any(row.review_state == "pending" for row in live):
                return "data_review"
        report = self.reports.query().filter_by(task_id=task.id).first()
        if report is not None:
            published = self.report_versions.published(report.id)
            if published is not None:
                return "done"
        return "reporting"

    # ---------- 读 ----------

    def out(self, task: ExperimentTask, detail: bool = False) -> dict:
        plan = self.plans.get(task.plan_id)
        owner = self.users.get(task.owner_user_id) if task.owner_user_id else None
        assignee = self.users.get(task.assignee_user_id) if task.assignee_user_id else None
        reviewer = self.users.get(task.reviewer_user_id) if task.reviewer_user_id else None
        state = self.derive_state(task)
        batch = self.batches.get(task.batch_id) if task.batch_id else None
        payload = {
            "id": task.id,
            "title": task.title or (plan.name if plan else task.id),
            "plan_id": task.plan_id,
            "plan_name": plan.name if plan else "",
            "plan_type": plan.plan_type if plan else "",
            "plan_version": task.plan_version,
            "owner_user_id": task.owner_user_id,
            "owner_name": owner.display_name if owner else "",
            "assignee_user_id": task.assignee_user_id,
            "assignee_name": assignee.display_name if assignee else "",
            "reviewer_user_id": task.reviewer_user_id,
            "reviewer_name": reviewer.display_name if reviewer else "",
            "batch_id": task.batch_id,
            "batch_state": batch.state if batch else "",
            "sample_ids": task.sample_ids or [],
            "due_at": task.due_at.isoformat(timespec="minutes") if task.due_at else None,
            "overdue": bool(
                task.due_at and task.due_at < now() and state not in {"done", "cancelled"}
            ),
            "priority": task.priority,
            "state": state,
            "state_label": STATE_LABEL.get(state, state),
            "stored_state": task.state,
            "accepted_at": task.accepted_at.isoformat(timespec="seconds") if task.accepted_at else None,
            "cancel_reason": task.cancel_reason,
            "note": task.note,
            "created_at": task.created_at.isoformat(timespec="seconds"),
            "row_version": task.row_version,
        }
        if detail:
            payload["history"] = [
                {
                    "action": row.action,
                    "action_label": ACTION_LABEL.get(row.action, row.action),
                    "from_user_id": row.from_user_id,
                    "from_name": self._name(row.from_user_id),
                    "to_user_id": row.to_user_id,
                    "to_name": self._name(row.to_user_id),
                    "reason": row.reason,
                    "actor_name": self._name(row.actor_id),
                    "created_at": row.created_at.isoformat(timespec="seconds"),
                }
                for row in self.assignments.for_task(task.id)
            ]
            payload["step_runs"] = [
                {
                    "id": row.id, "step_index": row.step_index, "kind": row.kind,
                    "state": row.state, "attempt": row.attempt,
                }
                for row in (self.runs.for_batch(task.batch_id) if task.batch_id else [])
            ]
            payload["analysis_tasks"] = [
                {"id": row.id, "state": row.state, "round_no": row.round_no}
                for row in (self.analysis.for_batch(task.batch_id) if task.batch_id else [])
            ]
            report = self.reports.query().filter_by(task_id=task.id).first()
            payload["report_id"] = report.id if report else ""
            payload["audit"] = [
                {
                    "time": e.time.isoformat(timespec="seconds"), "user": e.user,
                    "action": e.action, "before": e.before, "after": e.after, "detail": e.detail,
                }
                for e in self.audit.for_target(task.id)
            ]
        return payload

    def _name(self, user_id: str) -> str:
        user = self.users.get(user_id) if user_id else None
        return user.display_name if user else ""

    def page(self, offset: int, limit: int, state: str | None = None, assignee: str = "",
             plan_id: str = ""):
        rows, total = self.tasks.page(offset, limit, None, assignee, plan_id)
        items = [self.out(row) for row in rows]
        if state:
            # 状态是派生的，过滤只能在派生之后做
            items = [row for row in items if row["state"] == state]
        return items, total if not state else len(items)

    def detail(self, task_id: str) -> dict:
        task = self.tasks.get(task_id)
        if not task:
            raise NotFound("实验任务不存在")
        return self.out(task, detail=True)

    def my_tasks(self, user_id: str) -> list[dict]:
        return [self.out(row) for row in self.tasks.open_for_assignee(user_id)]

    def pending_accept(self) -> list[dict]:
        return [self.out(row) for row in self.tasks.pending_accept()]

    # ---------- 写 ----------

    def create(self, payload: dict, user: User) -> dict:
        plan = self.plans.get(payload["plan_id"])
        if not plan:
            raise NotFound("实验方案不存在")
        version = self.plan_versions.latest_approved(plan.id)
        if version is None:
            raise StateConflict(
                "只能基于已批准的方案版本建立实验任务",
                {"blocked": [{"key": "plan", "label": "方案还没有批准版本，请先提交评审并批准"}]},
                code="plan_not_approved",
            )
        task = ExperimentTask(
            id=self.tasks.next_id(), org_id=self.ctx.org_id, plan_id=plan.id,
            plan_version=version.version, plan_version_id=version.id,
            title=payload.get("title") or plan.name,
            owner_user_id=payload.get("owner_user_id") or user.id,
            reviewer_user_id=payload.get("reviewer_user_id", ""),
            sample_ids=payload.get("sample_ids") or [],
            due_at=payload.get("due_at"), priority=payload.get("priority", 2),
            note=payload.get("note", ""), created_by=user.id, state="unassigned",
        )
        self.tasks.add(task)
        self.audit.record(
            user, "建立实验任务", task.id, before="—", after="待分配",
            detail=f"方案 {plan.id} v{version.version}；{len(task.sample_ids)} 个样本",
            object_version=task.row_version,
        )
        self.db.commit()
        return self.out(task)

    def assign(self, task_id: str, payload: dict, user: User) -> dict:
        task = self.tasks.get(task_id)
        if not task:
            raise NotFound("实验任务不存在")
        self.tasks.check_version(task, payload.get("row_version"), "实验任务")
        if task.state == "cancelled":
            raise StateConflict("已取消的任务不能分配")
        assignee_id = payload.get("assignee_user_id")
        if not assignee_id:
            raise ValidationFailed("必须指定执行人")
        assignee = self.users.get(assignee_id)
        if not assignee:
            raise NotFound("执行人账号不存在")
        reassign = bool(task.assignee_user_id and task.assignee_user_id != assignee_id)
        reason = (payload.get("reason") or "").strip()
        if reassign and not reason:
            raise ValidationFailed("转派必须写明原因", code="reassign_reason_required")
        # 按预计执行时段校验资质：已排程的批次用排程窗口与冻结的步骤快照，否则用任务到期日；
        # 实际下发与恢复时还会再校验一次
        steps, start, end = self._execution_window(task)
        self.people.require_for_steps(assignee_id, steps, start, action="分配该任务", until=end)
        previous = task.assignee_user_id
        task.assignee_user_id = assignee_id
        task.accepted_at = None
        task.state = "pending_accept"
        task.updated_at = now()
        self.tasks.bump(task)
        self.assignments.add(
            TaskAssignment(
                task_id=task.id, from_user_id=previous, to_user_id=assignee_id,
                action="reassign" if reassign else "assign", reason=reason, actor_id=user.id,
            )
        )
        self.audit.record(
            user, "转派实验任务" if reassign else "分配实验任务", task.id,
            before=self._name(previous) or "待分配", after=assignee.display_name,
            detail=reason or "首次分配", object_version=task.row_version,
        )
        self.db.commit()
        return self.out(task)

    def accept(self, task_id: str, user: User) -> dict:
        task = self.tasks.get(task_id)
        if not task:
            raise NotFound("实验任务不存在")
        if task.assignee_user_id != user.id:
            raise PermissionDenied("只能接自己被分配的任务")
        if task.accepted_at is not None:
            raise StateConflict("任务已接单")
        steps = self._steps_for(task)
        self.people.require_for_steps(user.id, steps, now(), action="接单")
        task.accepted_at = now()
        task.state = "accepted"
        self.tasks.bump(task)
        self.assignments.add(
            TaskAssignment(
                task_id=task.id, from_user_id="", to_user_id=user.id, action="accept",
                actor_id=user.id,
            )
        )
        self.audit.record(
            user, "接单", task.id, before="待接单", after="已接单",
            object_version=task.row_version,
        )
        self.db.commit()
        return self.out(task)

    def cancel(self, task_id: str, reason: str, user: User) -> dict:
        task = self.tasks.get(task_id)
        if not task:
            raise NotFound("实验任务不存在")
        if not reason.strip():
            raise ValidationFailed("取消任务必须写明原因")
        batch = self.batches.get(task.batch_id) if task.batch_id else None
        if batch is not None and batch.state not in {"planned", "scheduled", "done", "aborted"}:
            raise StateConflict(
                "任务已有在途批次，请先终止批次再取消任务",
                {"blocked": [{"key": "batch", "label": f"批次 {batch.id} 当前 {batch.state}"}]},
                code="batch_in_flight",
            )
        before = self.derive_state(task)
        task.state = "cancelled"
        task.cancel_reason = reason
        self.tasks.bump(task)
        self.assignments.add(
            TaskAssignment(
                task_id=task.id, from_user_id=task.assignee_user_id, to_user_id="",
                action="cancel", reason=reason, actor_id=user.id,
            )
        )
        self.audit.record(
            user, "取消实验任务", task.id, before=STATE_LABEL.get(before, before), after="已取消",
            detail=reason, object_version=task.row_version,
        )
        self.db.commit()
        return self.out(task)

    def bind_batch(self, task: ExperimentTask, batch_id: str) -> None:
        """批次创建与任务绑定原子完成。首期一个任务只对应一个批次。"""
        if task.batch_id and task.batch_id != batch_id:
            raise StateConflict(
                f"该任务已绑定批次 {task.batch_id}，不能再建第二个",
                {"blocked": [{"key": "task", "label": "首期一个任务对应一个执行批次"}]},
                code="task_already_has_batch",
            )
        task.batch_id = batch_id
        task.updated_at = now()
        self.tasks.bump(task)

    def _execution_window(self, task: ExperimentTask):
        from ..domain.steps import normalize
        from ..models import Allocation, Batch

        batch = self.db.get(Batch, task.batch_id) if task.batch_id else None
        if batch is not None and batch.org_id == self.ctx.org_id:
            steps = normalize(batch.recipe_snapshot.get("steps") or [])
            windows = self.db.query(Allocation).filter(Allocation.batch_id == batch.id).all()
            if windows:
                return (
                    steps,
                    max(now(), min(a.starts_at for a in windows)),
                    max(a.ends_at for a in windows),
                )
            return steps, task.due_at or now(), None
        return self._steps_for(task), task.due_at or now(), None

    def _steps_for(self, task: ExperimentTask) -> list[dict]:
        from ..domain.steps import normalize
        from ..repositories.recipes import RecipeRepository

        plan = self.plans.get(task.plan_id)
        if plan is None:
            return []
        recipe = RecipeRepository(self.db, self.ctx).get(plan.recipe_id)
        return normalize(recipe.steps if recipe else [])

    def ensure_task_for_batch(self, batch_id: str, plan_id: str, user: User) -> ExperimentTask:
        """旧批次入口的兜底：没有任务就补一个，避免两条数据链。"""
        existing = self.tasks.by_batch(batch_id)
        if existing is not None:
            return existing
        plan = self.plans.get(plan_id)
        version = self.plan_versions.latest_approved(plan_id) if plan else None
        task = ExperimentTask(
            id=self.tasks.next_id(), org_id=self.ctx.org_id, plan_id=plan_id,
            plan_version=version.version if version else (plan.version if plan else 1),
            plan_version_id=version.id if version else "",
            title=plan.name if plan else batch_id,
            owner_user_id=user.id, assignee_user_id=user.id, accepted_at=now(),
            batch_id=batch_id, state="accepted", created_by=user.id,
            note="由批次入口自动补建，保持任务与批次一一对应",
        )
        self.tasks.add(task)
        return task
