"""任务中心。

任务状态是派生的，不是能 PATCH 的字段：执行阶段从 Batch / StepRun 来，
数据阶段从检测任务与结果审核来，报告阶段从报告版本来。手工只能改「谁做、什么时候做」。

任务可以拆成子任务（父任务是容器、不绑定批次，状态由子任务汇总），也可以声明上游任务：
上游的批次运行结束之前，下游的批次不能下发（开跑检查挡住），排程也不会把它排到上游结束之前。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..core.clock import as_utc, now
from ..core.context import AccessContext
from ..core.errors import NotFound, PermissionDenied, StateConflict, ValidationFailed
from ..domain import tasks as task_rules
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

    def derive_state(self, task: ExperimentTask, _depth: int = 0) -> str:
        """只有「待分配 / 待接单 / 已接单 / 已取消」是任务自己的状态，其余从对象派生。父任务由子任务汇总。"""
        if task.state == "cancelled":
            return "cancelled"
        children = self.tasks.children(task.id) if _depth < 8 else []
        if children:
            return task_rules.aggregate([self.derive_state(child, _depth + 1) for child in children])
        batch = self.batches.get(task.batch_id) if task.batch_id else None
        if batch is None:
            if not task.assignee_user_id:
                return "unassigned"
            return "accepted" if task.accepted_at else "pending_accept"
        if batch.state not in {"done", "aborted"}:
            return "running"
        if batch.state == "aborted":
            return "cancelled"
        # 批次 done 只表示运行结束，任务继续往数据与报告阶段走。展示状态只看复核流程走没走完；
        # 能不能作为下游开跑的依据另看 data_problems（审核通过不等于数据有效）
        review = self._data_review(batch)
        if review["tasks"] and not self._review_complete(review):
            return "data_review"
        report = self.reports.query().filter_by(task_id=task.id).first()
        if report is not None:
            published = self.report_versions.published(report.id)
            if published is not None:
                return "done"
        return "reporting"

    def _data_review(self, batch) -> dict[str, list]:
        """批次的数据阶段：有效检测任务，以及缺指标、待复核、被退回、判为无效的结果与没有检测任务的样本。

        有效检测任务：没取消、没被重测任务取代（重测链只看最新一轮）。没有有效检测任务就没有数据阶段。
        每个在用样本（没剔除、没拆分；拆分后看子样本）都要有有效检测任务。
        """
        from ..domain.metrics import collected

        tasks = [row for row in self.analysis.for_batch(batch.id) if row.state != "cancelled"]
        retested = {row.retest_of for row in tasks if row.retest_of}
        live = [row for row in tasks if row.id not in retested]
        review: dict[str, list] = {
            "tasks": live, "missing": [], "pending": [], "rejected": [], "invalid": [], "uncovered": [],
        }
        if not live:
            return review
        # 每个指标的当前版本 = 最高版本且未被取代（与 current_for_task 同一规则），一次查完这个批次的
        current_of: dict[str, dict] = {}
        for value in self.values.for_tasks([row.id for row in live]):
            if value.superseded_by_id:
                continue
            bucket = current_of.setdefault(value.analysis_task_id, {})
            existing = bucket.get(value.metric_definition_id)
            if existing is None or value.result_version > existing.result_version:
                bucket[value.metric_definition_id] = value
        for analysis in live:
            current = current_of.get(analysis.id, {})
            done, _ = collected(list(analysis.required_metrics or []), current)
            if not done:
                review["missing"].append(analysis)
            for value in current.values():
                if value.review_state == "pending":
                    review["pending"].append(value)
                elif value.review_state == "rejected":
                    review["rejected"].append(value)
                elif value.quality == "invalid":
                    review["invalid"].append(value)
        covered = {row.sample_id for row in live if row.sample_id}
        review["uncovered"] = [
            sample for sample in self.samples.for_batch(batch.id)
            if sample.state not in {"failed", "split"} and sample.id not in covered
        ]
        return review

    @staticmethod
    def _review_complete(review: dict[str, list]) -> bool:
        """复核流程走完：指标采齐、没有待复核或被退回的结果、每个在用样本都有检测任务。"""
        return not (review["missing"] or review["pending"] or review["rejected"] or review["uncovered"])

    def data_problems(self, task: ExperimentTask, _depth: int = 0) -> list[str]:
        """「数据复核通过」还差什么；空表示可以作为下游开跑的依据。

        独立计算，不从展示状态反推：复核流程走完之外，判为无效的数据也不能放行（审核通过只说明审过了）。
        要继续就重测，或把这条依赖改成别的放行条件。父任务逐个检查它未取消的子任务。
        """
        children = self.tasks.children(task.id) if _depth < 8 else []
        if children:
            problems: list[str] = []
            for child in children:
                if self.derive_state(child) == "cancelled":
                    continue
                problems += [f"子任务 {child.id} {text}" for text in self.data_problems(child, _depth + 1)]
            return problems
        batch = self.batches.get(task.batch_id) if task.batch_id else None
        if batch is None:
            return ["还没有批次"]
        if batch.state != "done":
            return ["批次还没运行结束"]
        review = self._data_review(batch)
        if not review["tasks"]:
            return []
        problems = []
        if review["uncovered"]:
            names = "、".join(sample.id for sample in review["uncovered"][:3])
            problems.append(f"{len(review['uncovered'])} 个在用样本没有检测任务（{names}）")
        if review["missing"]:
            problems.append(f"{len(review['missing'])} 个检测任务缺指标、未采齐")
        if review["pending"]:
            problems.append(f"{len(review['pending'])} 条结果待复核")
        if review["rejected"]:
            problems.append(f"{len(review['rejected'])} 条结果被退回，待重新录入或重测")
        if review["invalid"]:
            problems.append(f"{len(review['invalid'])} 条结果判为无效，重测或另行处置前不能作为下游依据")
        return problems

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
            "parent_id": task.parent_id or "",
            "depends_on": list(task.depends_on or []),
            # 从父任务继承来的上游：拆分出来的子任务同样要等它们
            "inherited_depends_on": [
                ref for ref in self.effective_dependencies(task) if ref not in (task.depends_on or [])
            ],
            "dependency_gate": task.dependency_gate or task_rules.DEFAULT_GATE,
            "dependency_gate_label": task_rules.gate_label(task.dependency_gate),
            "latest_plan_version": (latest.version if (latest := self.plan_versions.latest_approved(task.plan_id)) else None),
            "children": [
                {"id": child.id, "title": child.title, "batch_id": child.batch_id,
                 "state": (child_state := self.derive_state(child)),
                 "state_label": STATE_LABEL.get(child_state, child_state)}
                for child in self.tasks.children(task.id)
            ],
            "blocked_by": self.dependency_blockers(task),
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

    # ---------- 任务树与依赖 ----------

    def effective_edges(self, task: ExperimentTask) -> list[tuple[str, str]]:
        """这个任务真正要等的上游与各自的放行条件：自己声明的，加上每一层父任务声明的。

        继承来的依赖沿用声明它的那个任务的放行条件。拆分只是把一个任务切成几份执行，
        不能借此绕过父任务的外部依赖。
        """
        edges = [(ref, task.dependency_gate or task_rules.DEFAULT_GATE) for ref in task.depends_on or []]
        for ancestor in self.ancestors(task):
            edges.extend((ref, ancestor.dependency_gate or task_rules.DEFAULT_GATE) for ref in ancestor.depends_on or [])
        return list(dict.fromkeys(edges))

    def effective_dependencies(self, task: ExperimentTask) -> list[str]:
        """这个任务真正要等的上游（含继承）。环检测、排程下限、开跑检查、改依赖时的在途限制都按这一份算。"""
        return list(dict.fromkeys(ref for ref, _ in self.effective_edges(task)))

    def dependency_blockers(self, task: ExperimentTask) -> list[dict]:
        """还没满足的上游：没到放行条件（运行结束 / 数据复核通过 / 报告发布；父任务要全部子任务满足）或已取消。"""
        rows: list[dict] = []
        for upstream_id, gate in self.effective_edges(task):
            upstream = self.tasks.get(upstream_id)
            if upstream is None:
                rows.append({"task_id": upstream_id, "label": f"上游任务 {upstream_id} 不存在或不在本组织"})
                continue
            state = self.derive_state(upstream)
            if state == "cancelled":
                rows.append({"task_id": upstream_id, "label": f"上游任务 {upstream_id} 已取消：请移除这条依赖或改依赖别的任务"})
            elif gate == "data_validated" and (problems := self.data_problems(upstream)):
                # 数据放行逐项核对事实，不拿展示状态当证据
                rows.append({
                    "task_id": upstream_id, "gate": gate,
                    "label": (
                        f"上游任务 {upstream_id}「{upstream.title}」{STATE_LABEL.get(state, state)}，"
                        f"数据复核通过后本任务才能开跑：{'；'.join(problems[:3])}"
                    ),
                })
            elif not task_rules.gate_satisfied(gate, state):
                rows.append({
                    "task_id": upstream_id, "gate": gate,
                    "label": (
                        f"上游任务 {upstream_id}「{upstream.title}」{STATE_LABEL.get(state, state)}，"
                        f"{task_rules.gate_label(gate)}后本任务才能开跑"
                    ),
                })
        return rows

    def upstream_batches(self, task: ExperimentTask) -> list:
        """上游任务对应的批次（含从父任务继承的上游，父任务展开到叶子）。排程据此算下游最早开工时刻。"""
        return self.leaf_batches(self.effective_dependencies(task))

    def ancestors(self, task: ExperimentTask) -> list[ExperimentTask]:
        chain: list[ExperimentTask] = []
        current = task
        while current.parent_id and len(chain) < 16:
            parent = self.tasks.get(current.parent_id)
            if parent is None:
                break
            chain.append(parent)
            current = parent
        return chain

    def leaf_batches(self, task_ids: list[str]) -> list:
        """这些任务展开到叶子后的（任务, 批次）；叶子还没建批次时批次为 None。"""
        found = []
        pending = list(task_ids)
        seen: set[str] = set()
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            seen.add(current)
            row = self.tasks.get(current)
            if row is None:
                continue
            children = self.tasks.children(row.id)
            if children:
                pending.extend(child.id for child in children)
            elif row.batch_id:
                batch = self.batches.get(row.batch_id)
                if batch is not None:
                    found.append((row, batch))
            else:
                found.append((row, None))
        return found

    def set_dependencies(self, task_id: str, depends_on: list[str], user: User, gate: str | None = None) -> dict:
        task = self.tasks.get(task_id)
        if not task:
            raise NotFound("实验任务不存在")
        if gate is not None and gate not in task_rules.GATES:
            raise ValidationFailed(f"放行条件只能是 {'、'.join(task_rules.GATES)}")
        wanted = list(dict.fromkeys(ref.strip() for ref in depends_on or [] if ref and ref.strip()))
        for ref in wanted:
            if self.tasks.get(ref) is None:
                raise NotFound(f"上游任务 {ref} 不存在或不在本组织")
        issues = task_rules.dependency_issues(task.id, wanted, self.tasks.edges())
        issues += [
            f"{ref} 是本任务的子任务，父任务不能依赖自己的子任务" for ref in wanted
            if self._is_descendant(ref, task.id)
        ]
        issues += [text for text in self._inherited_cycles(task.id, wanted) if text not in issues]
        if issues:
            raise StateConflict("依赖设置不成立", {"blocked": [{"key": "dependency", "label": text} for text in issues]},
                                code="task_dependency_invalid")
        if set(wanted) - set(task.depends_on or []):
            # 子任务继承父任务的依赖：给父任务加上游，等于给它每个后代的批次加上游
            dispatched = [
                (leaf, batch) for leaf, batch in self.leaf_batches([task.id])
                if batch is not None and batch.state not in {"planned", "scheduled"}
            ]
            if dispatched:
                leaf, batch = dispatched[0]
                raise StateConflict(
                    "批次已下发，不能再给它加上游依赖" if leaf.id == task.id
                    else f"子任务 {leaf.id} 的批次 {batch.id} 已下发，不能再给父任务加上游依赖（子任务继承父任务的依赖）",
                    code="batch_in_flight",
                )
        before = list(task.depends_on or [])
        before_gate = task.dependency_gate or task_rules.DEFAULT_GATE
        stricter = gate is not None and (
            len(task_rules.GATES[gate][1]) < len(task_rules.GATES[before_gate][1])
        )
        if stricter and not set(wanted) - set(before):
            # 放行条件收紧同样等于给已下发的批次追加前置
            dispatched = [
                (leaf, batch) for leaf, batch in self.leaf_batches([task.id])
                if batch is not None and batch.state not in {"planned", "scheduled"}
            ]
            if dispatched:
                raise StateConflict(
                    f"批次 {dispatched[0][1].id} 已下发，不能再收紧上游的放行条件", code="batch_in_flight",
                )
        task.depends_on = wanted
        if gate is not None:
            task.dependency_gate = gate
        task.updated_at = now()
        self.tasks.bump(task)
        self.audit.record(
            user, "设置任务依赖", task.id, before="、".join(before) or "无", after="、".join(wanted) or "无",
            detail=f"上游任务{task_rules.gate_label(task.dependency_gate)}后，本任务的批次才能下发"
                   + (f"（放行条件 {task_rules.gate_label(before_gate)} → {task_rules.gate_label(gate)}）"
                      if gate is not None and gate != before_gate else ""),
            object_version=task.row_version,
        )
        self.db.commit()
        return self.out(task, detail=True)

    def _inherited_cycles(self, task_id: str, wanted: list[str]) -> list[str]:
        """算上继承的依赖会不会成环。

        本任务和它的全部后代都要等 wanted；从 wanted 出发沿「要等谁」往上走——每个任务要等它的有效
        上游，父任务还要等它的全部子任务——走回本任务或它的任一后代就是环。
        """
        targets = {task_id} | self._descendant_ids(task_id)
        issues: list[str] = []
        for start in wanted:
            stack, seen = [start], set()
            while stack:
                current = stack.pop()
                if current in targets:
                    issues.append(f"依赖 {start} 会形成环：{start} 直接、间接或通过父任务依赖本任务或它的子任务")
                    break
                if current in seen:
                    continue
                seen.add(current)
                row = self.tasks.get(current)
                if row is None:
                    continue
                stack.extend(self.effective_dependencies(row))
                stack.extend(child.id for child in self.tasks.children(row.id))
        return issues

    def _descendant_ids(self, task_id: str) -> set[str]:
        found: set[str] = set()
        pending = [task_id]
        while pending and len(found) < 1000:
            for child in self.tasks.children(pending.pop()):
                if child.id not in found:
                    found.add(child.id)
                    pending.append(child.id)
        return found

    def _is_descendant(self, candidate: str, ancestor: str) -> bool:
        row = self.tasks.get(candidate)
        hops = 0
        while row is not None and row.parent_id and hops < 16:
            if row.parent_id == ancestor:
                return True
            row = self.tasks.get(row.parent_id)
            hops += 1
        return False

    def migrate_version(self, task_id: str, reason: str, user: User) -> dict:
        """把任务显式迁移到方案当前的批准版本。

        任务建立时锁定批准版本，之后的方案修订不会静默改变它；要按新版本执行只能走这里，留审计。
        已经建了批次的任务不能迁移：批次快照已按旧版本冻结，换版本要先终止旧批次、另建任务。
        父任务连同它还没建批次的同方案后代一起迁移；已建批次的后代保持原版本（kept），挂在下面的
        别的方案的任务保持它自己方案的版本（skipped），结果里都列出来。
        """
        task = self.tasks.get(task_id)
        if not task:
            raise NotFound("实验任务不存在")
        reason = (reason or "").strip()
        if not reason:
            raise ValidationFailed("迁移方案版本必须写明原因")
        if task.state == "cancelled":
            raise StateConflict("已取消的任务不能迁移方案版本", code="task_cancelled")
        if task.batch_id:
            raise StateConflict(
                f"任务已绑定批次 {task.batch_id}，批次快照按 v{task.plan_version} 冻结，不能迁移；"
                f"要按新版本执行请终止旧批次、另建任务",
                code="task_already_has_batch",
            )
        version = self.plan_versions.latest_approved(task.plan_id)
        if version is None:
            raise StateConflict("方案没有批准版本", code="plan_not_approved")
        if version.id == task.plan_version_id:
            raise StateConflict(f"任务已经是最新批准版本 v{version.version}", code="task_version_current")
        family = [task, *(row for row in (self.tasks.get(ref) for ref in sorted(self._descendant_ids(task.id))) if row)]
        moved, kept, skipped = [], [], []
        for row in family:
            if row.state == "cancelled":
                continue
            if row.plan_id != task.plan_id:
                # 别的方案的任务：它的版本只能来自它自己的方案，写入这个方案的版本会让它再也建不了批次
                skipped.append({"task_id": row.id, "plan_id": row.plan_id, "plan_version": row.plan_version})
                continue
            if row.batch_id:
                kept.append({"task_id": row.id, "batch_id": row.batch_id, "plan_version": row.plan_version})
                continue
            before = row.plan_version
            row.plan_version, row.plan_version_id = version.version, version.id
            row.updated_at = now()
            self.tasks.bump(row)
            moved.append({"task_id": row.id, "from": before, "to": version.version})
        self.audit.record(
            user, "迁移任务方案版本", task.id, before=f"v{moved[0]['from']}" if moved else "—", after=f"v{version.version}",
            detail=f"{reason}；迁移 {len(moved)} 个任务" + (
                f"；{len(kept)} 个已建批次的子任务保持原版本（{'、'.join(row['task_id'] for row in kept)}）" if kept else ""
            ) + (
                f"；{len(skipped)} 个别的方案的子任务不迁移（{'、'.join(row['task_id'] for row in skipped)}）"
                if skipped else ""
            ),
            object_version=task.row_version,
        )
        self.db.commit()
        return {**self.out(task, detail=True), "migrated": moved, "kept": kept, "skipped": skipped}

    def _locked_content(self, task: ExperimentTask, plan):
        """任务锁定的方案版本内容，与建批次同一个解析口径。早先没记版本的任务按方案本身。"""
        if not task.plan_version_id:
            return plan
        version = self.plan_versions.get(task.plan_version_id)
        if version is None or version.plan_id != plan.id or version.state != "approved":
            raise StateConflict(
                f"任务锁定的方案版本 v{task.plan_version} 已不是批准状态，不能按它拆分",
                code="task_plan_version_invalid",
            )
        from .batch_service import BatchService

        return BatchService._pinned_plan(plan, version)

    def decompose(self, task_id: str, payload: dict, user: User) -> dict:
        """把任务拆成子任务：按样本分份（每份不超过 chunk_size，默认按方法的样品位），或拆成 N 份。

        子任务继承方案、优先级、期望完成时间、负责人与复核人；`sequential` 时后一份依赖前一份
        （同一台设备要按顺序做的场景），否则并行。父任务不绑定批次，状态由子任务汇总。
        """
        task = self.tasks.get(task_id)
        if not task:
            raise NotFound("实验任务不存在")
        if task.state == "cancelled":
            raise StateConflict("已取消的任务不能拆分")
        if task.batch_id:
            raise StateConflict(
                f"任务已绑定批次 {task.batch_id}：绑定了批次的任务不能再拆，请新建任务拆分", code="task_already_has_batch",
            )
        if self.tasks.children(task.id):
            raise StateConflict("任务已经拆分过", code="task_already_split")
        plan = self.plans.get(task.plan_id)
        if plan is None:
            raise NotFound("实验方案不存在")
        # 样本清单、流程与方案类型都取任务锁定的版本：方案之后的修订（哪怕还是草稿）不能借拆分混进来
        content = self._locked_content(task, plan)
        samples = list(task.sample_ids or []) or (
            list(content.sample_ids or []) if content.plan_type != "matrix" else []
        )
        parts = payload.get("parts")
        size = payload.get("chunk_size")
        if samples:
            if not size:
                if parts:
                    size = -(-len(samples) // int(parts))
                else:
                    from ..repositories.recipes import RecipeRepository

                    recipe = RecipeRepository(self.db, self.ctx).get(content.recipe_id)
                    size = max(1, int(recipe.plate or 1)) if recipe else len(samples)
            groups = task_rules.chunks(samples, int(size))
        else:
            count = int(parts or 0)
            if count < 2:
                raise ValidationFailed("没有样本清单时必须指定拆成几份（parts ≥ 2），每份按方案整体执行一次")
            groups = [[] for _ in range(count)]
        if len(groups) < 2:
            raise ValidationFailed(f"按每份 {size} 个样本只能分成 {len(groups)} 份，不需要拆分")
        if len(groups) > 50:
            raise ValidationFailed("一次最多拆成 50 个子任务")
        sequential = bool(payload.get("sequential"))
        created: list[ExperimentTask] = []
        for number, group in enumerate(groups, start=1):
            child = ExperimentTask(
                id=self.tasks.next_id(), org_id=self.ctx.org_id, plan_id=task.plan_id,
                plan_version=task.plan_version, plan_version_id=task.plan_version_id,
                title=f"{task.title} · {number}/{len(groups)}", owner_user_id=task.owner_user_id,
                reviewer_user_id=task.reviewer_user_id, sample_ids=group, due_at=task.due_at,
                priority=task.priority, note=f"由 {task.id} 拆分", created_by=user.id, state="unassigned",
                parent_id=task.id, depends_on=[created[-1].id] if sequential and created else [],
            )
            self.tasks.add(child)
            self.db.flush()
            created.append(child)
        task.updated_at = now()
        self.tasks.bump(task)
        self.audit.record(
            user, "拆分实验任务", task.id, after=f"{len(created)} 个子任务",
            detail=(
                f"{'按样本每份 ' + str(size) + ' 个' if samples else '整体执行 ' + str(len(groups)) + ' 次'}；"
                f"{'顺序依赖' if sequential else '并行'}；子任务 {'、'.join(row.id for row in created)}"
            ),
            object_version=task.row_version,
        )
        self.db.commit()
        return self.out(task, detail=True)

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
            # 库里存无时区 UTC；带偏移的截止时间留在对象上，返回时和 now() 比「是否逾期」会直接报错
            due_at=as_utc(payload.get("due_at")), priority=payload.get("priority", 2),
            note=payload.get("note", ""), created_by=user.id, state="unassigned",
            parent_id=payload.get("parent_id") or "",
        )
        if task.parent_id:
            parent = self.tasks.get(task.parent_id)
            if parent is None:
                raise NotFound("父任务不存在或不在本组织")
            if parent.batch_id:
                raise StateConflict(f"父任务已绑定批次 {parent.batch_id}，不能再挂子任务", code="task_already_has_batch")
        wanted = list(payload.get("depends_on") or [])
        for ref in wanted:
            if self.tasks.get(ref) is None:
                raise NotFound(f"上游任务 {ref} 不存在或不在本组织")
        task.depends_on = wanted
        gate = payload.get("dependency_gate") or task_rules.DEFAULT_GATE
        if gate not in task_rules.GATES:
            raise ValidationFailed(f"放行条件只能是 {'、'.join(task_rules.GATES)}")
        task.dependency_gate = gate
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
        self._require_sop_ack_in_flight(task, assignee_id, "转派该任务")
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
        if task.batch_id:
            # 已排程的批次：人工步骤的预占跟着换到新执行人
            from ..models import Batch
            from .staffing_service import StaffingService

            batch = self.db.get(Batch, task.batch_id)
            if batch is not None and batch.state in {"scheduled", "running", "held"}:
                self.db.flush()
                StaffingService(self.db, self.ctx).book_batch(batch)
        self.audit.record(
            user, "转派实验任务" if reassign else "分配实验任务", task.id,
            before=self._name(previous) or "待分配", after=assignee.display_name,
            detail=reason or "首次分配", object_version=task.row_version,
        )
        self.db.commit()
        return self.out(task)

    def _require_sop_ack_in_flight(self, task, user_id: str, action: str) -> None:
        """批次已经开跑时换执行人：新执行人要确认过批次采用的 SOP（按固化版本）。

        还没开跑的批次由开跑检查核对；开跑之后下一个关口是恢复或提交人工记录，改派时就说清楚更好。
        """
        if not task.batch_id:
            return
        from ..models import Batch
        from .sop_service import SopService

        batch = self.db.get(Batch, task.batch_id)
        if batch is None or batch.state not in {"running", "paused", "fault"}:
            return
        blockers = SopService(self.db, self.ctx).batch_ack_blockers(batch, user_id) or []
        if blockers:
            raise StateConflict(
                f"{action}前需要阅读确认：{blockers[0]}；执行人可在批次页确认本批次采用的 SOP",
                {"blocked": [{"key": "sop_ack", "label": text} for text in blockers]}, code="sop_ack_required",
            )

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
        self._require_sop_ack_in_flight(task, user.id, "接单")
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
        children = self.tasks.children(task.id)
        in_flight = [
            (child, child_batch) for child in children
            if (child_batch := self.batches.get(child.batch_id) if child.batch_id else None) is not None
            and child_batch.state not in {"planned", "scheduled", "done", "aborted"}
        ]
        if in_flight:
            raise StateConflict(
                "子任务有在途批次，请先终止这些批次再取消父任务",
                {"blocked": [{"key": "batch", "label": f"子任务 {child.id} 的批次 {row.id} 当前 {row.state}"}
                             for child, row in in_flight]},
                code="batch_in_flight",
            )
        for child in children:
            if child.state != "cancelled":
                child.state = "cancelled"
                child.cancel_reason = f"随父任务 {task.id} 取消：{reason}"
                self.tasks.bump(child)
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
        """批次创建与任务绑定原子完成。叶子任务只对应一个批次；父任务不绑定批次。"""
        if self.tasks.children(task.id):
            raise StateConflict(
                "父任务不直接执行：请在它的子任务上建批次",
                {"blocked": [{"key": "task", "label": "任务已拆成子任务，状态由子任务汇总"}]},
                code="task_has_children",
            )
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
