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
    "done", "cancelled", "shortfall",
)
STATE_LABEL = {
    "unassigned": "待分配", "pending_accept": "待接单", "accepted": "已接单",
    "running": "执行中", "data_review": "待数据复核", "reporting": "待报告",
    "done": "完成", "cancelled": "已取消",
    # 父任务：子任务都跑完了，但计划的样本有短缺（批次终止、样本不合格），要补测或签名放弃
    "shortfall": "待补测",
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
        # 一次输出（out）之内的派生结果缓存：父任务的状态、进度要按样本逐个批次数，列表里会被问好几遍。
        # 只在 out 期间有效——写操作之间不缓存，免得拿到改动之前的数
        self._memo: dict | None = None

    # ---------- 状态派生 ----------

    def derive_state(self, task: ExperimentTask, _depth: int = 0) -> str:
        """只有「待分配 / 待接单 / 已接单 / 已取消」是任务自己的状态，其余从对象派生。父任务由子任务汇总。"""
        if self._memo is not None and ("state", task.id) in self._memo:
            return self._memo[("state", task.id)]
        state = self._derive_state(task, _depth)
        if self._memo is not None:
            self._memo[("state", task.id)] = state
        return state

    def _derive_state(self, task: ExperimentTask, _depth: int = 0) -> str:
        if task.state == "cancelled":
            return "cancelled"
        children = self.tasks.children(task.id) if _depth < 8 else []
        if children:
            state = task_rules.aggregate([self.derive_state(child, _depth + 1) for child in children])
            # 子任务都跑完了不等于这件事做完了：批次终止、样本不合格留下的短缺要补测或签名放弃，
            # 否则父任务不能结束，也不能作为下游的「运行结束」依据
            if state in task_rules.RUN_FINISHED and self.progress(task, _depth)["shortfall"] > 0:
                return "shortfall"
            return state
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
        if self._report_covers(task, batch.id):
            return "done"
        return "reporting"

    def _report_covers(self, task: ExperimentTask, batch_id: str) -> bool:
        """这个叶子任务的结果有没有进一份已发布的报告：它自己的报告，或纳入了它批次的祖先任务报告。

        一个方案分多批执行时报告通常出在父任务上（合并统计、分批明细），子任务不必各出一份；
        父任务报告发布之后才补做的批次不在那份报告里，照样要等新版报告。
        """
        for owner in [task, *self.ancestors(task)]:
            report = self.reports.query().filter_by(task_id=owner.id).first()
            if report is None:
                continue
            published = self.report_versions.published(report.id)
            if published is None:
                continue
            content = published.content or {}
            covered = content.get("batch_ids") or [content.get("batch_id")]
            if owner is task or batch_id in covered:
                return True
        return False

    # ---------- 按样本算的进度 ----------

    def _groups_planned(self, task: ExperimentTask, content) -> dict[str, int]:
        """叶子任务按条件组计划做几个：补测给的条件组、矩阵每组若干次重复，非矩阵只有一组 C01。"""
        portion = task.portion or {}
        if portion.get("groups"):
            return {str(group): int(count or 0) for group, count in portion["groups"].items()}
        if content.plan_type == "matrix":
            from ..domain import matrix

            each = int(portion.get("repeats") or content.repeats or 1)
            return {
                condition.group: each
                for condition in matrix.conditions(content.factors or [], content.control, content.design_points or None)
            }
        return {"C01": self.planned_count(task, content)}

    def _top_level_samples(self, batch) -> tuple[list, set[str]]:
        """批次里建批次时生成的那些样本（拆分出来的子样本并回母样），以及其中算失败的。

        母样被拆分后看子样本：子样本全部不合格才算这个母样失败。
        """
        rows = self.samples.for_batch(batch.id)
        split = [row.id for row in rows if row.state == "split"]
        top = [row for row in rows if not any(row.id.startswith(parent + "-") for parent in split)]
        failed: set[str] = set()
        for row in top:
            if row.state == "failed":
                failed.add(row.id)
            elif row.state == "split":
                descendants = [other for other in rows if other.id.startswith(row.id + "-") and other.state != "split"]
                if not descendants or all(other.state == "failed" for other in descendants):
                    failed.add(row.id)
        return top, failed

    def _leaf_tally(self, task: ExperimentTask) -> dict[str, dict[str, int]]:
        plan = self.plans.get(task.plan_id)
        tally: dict[str, dict[str, int]] = {
            key: {} for key in ("planned", "valid", "failed", "running", "pending", "descoped")
        }
        if plan is None:
            return tally
        content = self._content_of(task, plan)
        tally["planned"] = self._groups_planned(task, content)
        batch = self.batches.get(task.batch_id) if task.batch_id else None
        if batch is None or batch.state in {"planned", "scheduled"}:
            # 还没下发：取消了就是不做了（写了原因），否则待做
            tally["descoped" if task.state == "cancelled" else "pending"] = dict(tally["planned"])
            return tally
        top, failed = self._top_level_samples(batch)
        for row in top:
            if batch.state == "aborted" or row.id in failed:
                bucket = "failed"
            elif batch.state == "done":
                bucket = "valid"
            else:
                bucket = "running"
            group = row.condition_group or "C01"
            tally[bucket][group] = tally[bucket].get(group, 0) + 1
        return tally

    def _tally(self, task: ExperimentTask, _depth: int = 0) -> tuple[dict[str, dict[str, int]], int]:
        """（按条件组的计数, 已签名放弃的个数）。补测子任务的计划量不计入 target，另记在 retest。"""
        tally: dict[str, dict[str, int]] = {
            key: {} for key in ("target", "valid", "failed", "running", "pending", "descoped", "retest")
        }

        def add(bucket: str, groups: dict[str, int]) -> None:
            for group, count in groups.items():
                tally[bucket][group] = tally[bucket].get(group, 0) + int(count or 0)

        accepted = sum(int(row.get("count") or 0) for row in task.shortfall_decisions or [])
        children = self.tasks.children(task.id) if _depth < 8 else []
        if not children:
            leaf = self._leaf_tally(task)
            for bucket in ("valid", "failed", "running", "pending"):
                add(bucket, leaf[bucket])
            if task.purpose == "retest":
                add("retest", leaf["planned"])
            else:
                add("descoped", leaf["descoped"])
                if not leaf["descoped"]:
                    add("target", leaf["planned"])
            return tally, accepted
        for child in children:
            sub, sub_accepted = self._tally(child, _depth + 1)
            accepted += sub_accepted
            for bucket, groups in sub.items():
                add(bucket, groups)
        return tally, accepted

    def progress(self, task: ExperimentTask, _depth: int = 0) -> dict:
        """按样本算的进度：计划、有效完成、失败、进行中、待开始、补测中、已签名放弃，以及短缺。

        短缺按条件组算：一个条件缺的样本不能拿另一个条件多做的补上。补测子任务、签名放弃都会让短缺变少；
        没下发就取消的子任务是不做了（记为「取消」），不算短缺。
        """
        if self._memo is not None and ("progress", task.id) in self._memo:
            return self._memo[("progress", task.id)]
        tally, accepted = self._tally(task, _depth)
        groups = sorted(set().union(*(set(values) for values in tally.values())))
        per_group = {
            group: {
                "target": tally["target"].get(group, 0), "valid": tally["valid"].get(group, 0),
                "open": tally["running"].get(group, 0) + tally["pending"].get(group, 0),
                "failed": tally["failed"].get(group, 0),
            }
            for group in groups
        }
        missing = sum(max(0, row["target"] - row["valid"] - row["open"]) for row in per_group.values())
        totals = {bucket: sum(values.values()) for bucket, values in tally.items()}
        result = {
            **totals, "accepted": accepted,
            "shortfall": task_rules.shortfall(missing, 0, 0, accepted),
            "groups": per_group,
        }
        if self._memo is not None:
            self._memo[("progress", task.id)] = result
        return result

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
        outer = self._memo is None
        if outer:
            self._memo = {}
        try:
            return self._out(task, detail)
        finally:
            if outer:
                self._memo = None

    def _out(self, task: ExperimentTask, detail: bool = False) -> dict:
        plan = self.plans.get(task.plan_id)
        owner = self.users.get(task.owner_user_id) if task.owner_user_id else None
        assignee = self.users.get(task.assignee_user_id) if task.assignee_user_id else None
        reviewer = self.users.get(task.reviewer_user_id) if task.reviewer_user_id else None
        state = self.derive_state(task)
        batch = self.batches.get(task.batch_id) if task.batch_id else None
        children = self.tasks.children(task.id)
        matrix_plan = bool(plan and plan.plan_type == "matrix")
        if children:
            planned = self.progress(task)["target"]
        else:
            planned = self.planned_count(task) if plan else 0
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
            "children": [self._child_out(child, matrix_plan) for child in children],
            # 一个方案分多批执行：这一份负责什么、父任务怎么拆的、是不是补测
            "portion": task.portion or {},
            "portion_label": (
                self.part_label(task.portion or {}, planned, matrix_plan) if task.parent_id and task.portion else ""
            ),
            "planned_count": planned,
            "split_mode": task.split_mode or "",
            "split_mode_label": task_rules.SPLIT_MODES.get(task.split_mode or "", ""),
            "purpose": task.purpose or "",
            "progress": self.progress(task) if children or detail else None,
            "shortfall_decisions": list(task.shortfall_decisions or []),
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
            if children:
                payload["sample_map"] = self.sample_map(task, matrix_plan)
            payload["audit"] = [
                {
                    "time": e.time.isoformat(timespec="seconds"), "user": e.user,
                    "action": e.action, "before": e.before, "after": e.after, "detail": e.detail,
                }
                for e in self.audit.for_target(task.id)
            ]
        return payload

    def _child_out(self, child: ExperimentTask, matrix_plan: bool) -> dict:
        state = self.derive_state(child)
        grandchildren = self.tasks.children(child.id)
        planned = self.progress(child)["target"] if grandchildren else self.planned_count(child)
        batch = self.batches.get(child.batch_id) if child.batch_id else None
        counts = self.progress(child)
        return {
            "id": child.id, "title": child.title, "batch_id": child.batch_id,
            "batch_state": batch.state if batch else "",
            "state": state, "state_label": STATE_LABEL.get(state, state),
            "purpose": child.purpose or "", "planned_count": planned if child.purpose != "retest" else counts["retest"],
            "portion": child.portion or {},
            "portion_label": self.part_label(child.portion or {}, planned, matrix_plan) if child.portion else "",
            "depends_on": list(child.depends_on or []),
            "dependency_gate_label": task_rules.gate_label(child.dependency_gate),
            "valid": counts["valid"], "failed": counts["failed"], "running": counts["running"],
            "pending": counts["pending"],
        }

    def sample_map(self, task: ExperimentTask, matrix_plan: bool) -> list[dict]:
        """样本 → 子任务 → 批次 → 孔位对照。建了批次的列实际样本，没建的列计划的样本清单或份额。"""
        rows = []
        for child in self.tasks.children(task.id):
            batch = self.batches.get(child.batch_id) if child.batch_id else None
            planned = self.planned_count(child) if not self.tasks.children(child.id) else self.progress(child)["target"]
            if batch is not None:
                top, failed = self._top_level_samples(batch)
                samples = [
                    {
                        "id": row.id, "physical_sample_id": row.physical_sample_id, "well": row.well,
                        "condition_group": row.condition_group, "repeat": row.repeat,
                        "state": "failed" if (row.id in failed or batch.state == "aborted") else row.state,
                    }
                    for row in top
                ]
            else:
                samples = [
                    {"id": "", "physical_sample_id": sample_id, "well": "", "condition_group": "", "repeat": 0,
                     "state": "planned"}
                    for sample_id in child.sample_ids or []
                ]
            rows.append({
                "task_id": child.id, "title": child.title, "purpose": child.purpose or "",
                "label": self.part_label(child.portion or {}, planned, matrix_plan) if child.portion else "",
                "planned_count": planned, "batch_id": child.batch_id, "batch_state": batch.state if batch else "",
                "samples": samples,
            })
        return rows

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
        plan = self.plans.get(task.plan_id)
        old_content = self._content_of(task, plan) if plan is not None else None
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
        reportioned = self._reportion(task, plan, old_content, version) if plan is not None and old_content else []
        self.audit.record(
            user, "迁移任务方案版本", task.id, before=f"v{moved[0]['from']}" if moved else "—", after=f"v{version.version}",
            detail=f"{reason}；迁移 {len(moved)} 个任务" + (
                f"；按新版本重新分配份额：{'、'.join(reportioned)}" if reportioned else ""
            ) + (
                f"；{len(kept)} 个已建批次的子任务保持原版本（{'、'.join(row['task_id'] for row in kept)}）" if kept else ""
            ) + (
                f"；{len(skipped)} 个别的方案的子任务不迁移（{'、'.join(row['task_id'] for row in skipped)}）"
                if skipped else ""
            ),
            object_version=task.row_version,
        )
        self.db.commit()
        return {**self.out(task, detail=True), "migrated": moved, "kept": kept, "skipped": skipped}

    def _reportion(self, task: ExperimentTask, plan, old_content, version) -> list[str]:
        """父任务整单迁移到新批准版本：还没建批次的子任务按新版本重新分配份额。

        按数量拆的：新版本总数减去已建批次的子任务占掉的，均分给还没建批次的；矩阵按重复拆的同理（条件变了
        不行——已建批次的子任务按旧条件在做，混在一起没法合并统计）；整体重复的每份按新版本整体执行。
        分不下（剩下的比子任务还少、或某份超过流程每批样品位）就拒绝，请取消后按新版本重新建任务。
        """
        from ..domain import matrix
        from .batch_service import BatchService

        shares = [
            child for child in self.tasks.children(task.id)
            if child.purpose != "retest" and child.state != "cancelled" and child.plan_id == task.plan_id
            and ((child.portion or {}).get("count") or (child.portion or {}).get("repeats"))
        ]
        if not shares:
            return []
        content = BatchService._pinned_plan(plan, version)
        recipe = self._recipe_of(content)
        plate = int(recipe.plate or 0) if recipe else 0
        matrix_plan = content.plan_type == "matrix"
        key = "repeats" if matrix_plan else "count"
        movable = [child for child in shares if not child.batch_id]
        fixed = [child for child in shares if child.batch_id]
        if not movable:
            return []

        def refuse(message: str) -> None:
            raise StateConflict(
                f"{message}：请取消这个父任务，按新版本重新建任务", code="task_split_mismatch",
            )

        conditions = self.condition_count(content)
        if matrix_plan:
            before = [tuple(row.levels) for row in matrix.conditions(
                old_content.factors or [], old_content.control, old_content.design_points or None)]
            after = [tuple(row.levels) for row in matrix.conditions(
                content.factors or [], content.control, content.design_points or None)]
            if fixed and before != after:
                refuse("新版本的条件与原版本不同，已建批次的子任务按原条件在做")
            unit_total, per_part = max(1, int(content.repeats or 1)), max(1, plate // max(1, conditions))
        else:
            unit_total, per_part = int(content.sample_count or 0), plate
            if unit_total <= 0:
                refuse("新版本没有样本数")
        if any((child.portion or {}).get("replica") for child in shares):
            if unit_total > per_part:
                refuse(f"新版本一份就超过流程每批样品位 {plate}，不能整体重复")
            for child in movable:
                replica = int((child.portion or {}).get("replica") or 1)
                child.portion = {**(child.portion or {}), key: unit_total, "offset": (replica - 1) * unit_total}
                self.tasks.bump(child)
            return [f"{child.id} 整体执行 {unit_total}" for child in movable]
        used = sum(int((child.portion or {}).get(key) or 0) for child in fixed)
        remaining = unit_total - used
        if remaining < len(movable):
            refuse(
                f"新版本共 {unit_total}{'次重复' if matrix_plan else '个样本'}，已建批次的子任务占了 {used}，"
                f"剩下的不够分给 {len(movable)} 个子任务"
            )
        sizes = task_rules.balanced(remaining, len(movable))
        if max(sizes) > per_part:
            refuse(f"剩下的 {remaining} 分给 {len(movable)} 个子任务后超过流程每批样品位 {plate}")
        start = max(
            (int((child.portion or {}).get("offset") or 0) + int((child.portion or {}).get(key) or 0) for child in fixed),
            default=0,
        )
        notes = []
        for child, size, offset in zip(movable, sizes, task_rules.offsets(sizes)):
            child.portion = {**(child.portion or {}), key: size, "offset": start + offset}
            self.tasks.bump(child)
            notes.append(f"{child.id} {size}{'次重复' if matrix_plan else '个'}")
        return notes

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

    # ---------- 一个方案分多批执行 ----------

    def _content_of(self, task: ExperimentTask, plan):
        """任务锁定版本的方案内容。只读场合（进度、展示）版本失效时退回方案本身；写操作走 `_locked_content`。"""
        try:
            return self._locked_content(task, plan)
        except StateConflict:
            return plan

    def _recipe_of(self, content):
        from ..repositories.recipes import RecipeRepository

        return RecipeRepository(self.db, self.ctx).get(content.recipe_id)

    @staticmethod
    def condition_count(content) -> int:
        if content.plan_type != "matrix":
            return 1
        from ..domain import matrix

        return len(matrix.conditions(content.factors or [], content.control, content.design_points or None))

    def planned_count(self, task: ExperimentTask, content=None) -> int:
        """叶子任务计划做几个样本：样本清单、本份份额，或方案整体。"""
        if task.sample_ids:
            return len(task.sample_ids)
        plan = self.plans.get(task.plan_id)
        if plan is None:
            return 0
        content = content if content is not None else self._content_of(task, plan)
        counted = task_rules.portion_count(task.portion, self.condition_count(content))
        if counted is not None:
            return counted
        from .plan_service import PlanService

        return PlanService(self.db, self.ctx).sample_total(content)

    def split_parts(self, content, payload: dict, samples: list[str]) -> dict:
        """按请求算出每一份，不写库。拆分预览、建任务时自动拆分与手动拆分共用这一套。

        有样本清单：按清单顺序切，每份一个子任务、带自己的样本。按数量的方案：每份记样本数与全局序号偏移，
        建批次时按它生成样本。矩阵方案：按重复拆，每份都包含全部条件（完整区组）。整体重复（replicate）：
        每份按方案整体执行一次。任何一份都不能超过流程每批样品位。
        """
        recipe = self._recipe_of(content)
        plate = int(recipe.plate or 0) if recipe else 0
        chunk = int(payload.get("chunk_size") or 0) or None
        parts = int(payload.get("parts") or 0) or None
        mode = payload.get("mode") or ("sequential" if payload.get("sequential") else task_rules.DEFAULT_SPLIT_MODE)
        if mode not in {"parallel", "pilot", "sequential"}:
            raise ValidationFailed("拆分方式只能是 parallel（并行）、pilot（首批验证后放行）、sequential（逐批顺序）")
        matrix_plan = content.plan_type == "matrix"
        conditions = self.condition_count(content)
        replicate = payload.get("replicate")
        try:
            if samples:
                if replicate:
                    raise ValueError("有样本清单时不能整体重复：同一个样本不能同时放进几个批次")
                replicate = False
                sizes = task_rules.split_sizes(len(samples), plate, per_batch=chunk, parts=parts)
                rows = [
                    {"sample_ids": samples[start:start + size], "portion": {"offset": start}, "size": size}
                    for size, start in zip(sizes, task_rules.offsets(sizes))
                ]
            else:
                if matrix_plan:
                    repeats = max(1, int(content.repeats or 1))
                    total = conditions * repeats
                else:
                    repeats, total = 1, int(content.sample_count or 0)
                    if total <= 0:
                        raise ValueError("方案没有样本清单也没有样本数，无法拆分")
                if replicate is None:
                    # 兼容旧用法：一批放得下的矩阵方案给了份数，就是把整个矩阵重复做几次
                    replicate = matrix_plan and bool(parts) and not chunk and total <= plate
                if replicate:
                    if not parts:
                        raise ValueError("整体重复执行要指定份数（至少 2 份）")
                    if total > plate:
                        raise ValueError(
                            f"方案 {total} 个样本超过流程每批样品位 {plate}，一批放不下，不能整体重复；请按份额分批"
                        )
                    key, unit = ("repeats", repeats) if matrix_plan else ("count", total)
                    rows = [
                        {"sample_ids": [], "portion": {key: unit, "offset": index * unit, "replica": index + 1},
                         "size": total}
                        for index in range(parts)
                    ]
                elif matrix_plan:
                    blocks = task_rules.matrix_blocks(conditions, repeats, plate, per_batch=chunk, parts=parts)
                    rows = [
                        {"sample_ids": [], "portion": {"repeats": block, "offset": start}, "size": block * conditions}
                        for block, start in zip(blocks, task_rules.offsets(blocks))
                    ]
                else:
                    sizes = task_rules.split_sizes(total, plate, per_batch=chunk, parts=parts)
                    rows = [
                        {"sample_ids": [], "portion": {"count": size, "offset": start}, "size": size}
                        for size, start in zip(sizes, task_rules.offsets(sizes))
                    ]
        except ValueError as error:
            raise ValidationFailed(str(error), code="split_invalid") from error
        return {
            "parts": rows, "mode": mode, "mode_label": task_rules.SPLIT_MODES[mode], "replicate": bool(replicate),
            "capacity": plate, "total": sum(row["size"] for row in rows),
            "conditions": conditions if matrix_plan else None, "plan_type": content.plan_type,
        }

    @staticmethod
    def part_label(portion: dict, size: int, matrix_plan: bool) -> str:
        """给人看的份额：第 8–14 号 / 第 5–7 次重复 / 第 2 次整体重复 / 补测。"""
        portion = portion or {}
        offset = int(portion.get("offset") or 0)
        if portion.get("groups"):
            return "补测 " + "、".join(f"{group}×{count}" for group, count in portion["groups"].items())
        if portion.get("replica"):
            return f"第 {portion['replica']} 次整体执行"
        if matrix_plan and portion.get("repeats"):
            first, last = offset + 1, offset + int(portion["repeats"])
            return f"第 {first} 次重复" if first == last else f"第 {first}–{last} 次重复"
        if size <= 0:
            return "—"
        first, last = offset + 1, offset + size
        return f"第 {first} 号" if first == last else f"第 {first}–{last} 号"

    def split_preview(self, payload: dict) -> dict:
        """拆分预览：建任务前按方案的批准版本算，拆分前按任务锁定的版本算。不写库。"""
        if payload.get("task_id"):
            task = self.tasks.get(payload["task_id"])
            if task is None:
                raise NotFound("实验任务不存在")
            plan = self.plans.get(task.plan_id)
            if plan is None:
                raise NotFound("实验方案不存在")
            content = self._locked_content(task, plan)
            samples = list(task.sample_ids or []) or (
                list(content.sample_ids or []) if content.plan_type != "matrix" else []
            )
        else:
            plan = self.plans.get(payload.get("plan_id") or "")
            if plan is None:
                raise NotFound("实验方案不存在")
            version = self.plan_versions.latest_approved(plan.id)
            if version is None:
                raise StateConflict("方案还没有批准版本", code="plan_not_approved")
            from .batch_service import BatchService

            content = BatchService._pinned_plan(plan, version)
            samples = list(payload.get("sample_ids") or []) or (
                list(content.sample_ids or []) if content.plan_type != "matrix" else []
            )
        return self._preview_out(content, samples, payload)

    def _preview_out(self, content, samples: list[str], payload: dict) -> dict:
        from .plan_service import PlanService

        recipe = self._recipe_of(content)
        plate = int(recipe.plate or 0) if recipe else 0
        total = len(samples) or PlanService(self.db, self.ctx).sample_total(content)
        needed = total > plate
        wanted = bool(payload.get("chunk_size") or payload.get("parts") or payload.get("replicate"))
        out = {
            "total": total, "capacity": plate, "needs_split": needed, "plan_type": content.plan_type,
            "modes": [{"key": key, "label": label} for key, label in task_rules.SPLIT_MODES.items() if key != "replicate"],
            "parts": [], "mode": payload.get("mode") or task_rules.DEFAULT_SPLIT_MODE, "error": "",
        }
        if not needed and not wanted:
            out["detail"] = f"{total} 个样本；流程每批 {plate} 位，一批完成，不需要拆分"
            return out
        try:
            planned = self.split_parts(content, payload, samples)
        except ValidationFailed as error:
            out["error"] = error.message
            out["detail"] = error.message
            return out
        matrix_plan = content.plan_type == "matrix"
        out |= {
            "mode": planned["mode"], "mode_label": planned["mode_label"], "replicate": planned["replicate"],
            "conditions": planned["conditions"], "total_planned": planned["total"],
            "parts": [
                {
                    "index": number, "size": row["size"], "portion": row["portion"], "sample_ids": row["sample_ids"],
                    "label": self.part_label(row["portion"], row["size"], matrix_plan),
                }
                for number, row in enumerate(planned["parts"], start=1)
            ],
        }
        sizes = "/".join(str(row["size"]) for row in planned["parts"])
        out["detail"] = (
            f"整体执行 {len(planned['parts'])} 次，每次 {planned['parts'][0]['size']} 个样本"
            if planned["replicate"] else
            f"{total} 个样本；流程每批 {plate} 位 → 分 {len(planned['parts'])} 批（{sizes}）"
            + ("，每批都包含全部条件" if matrix_plan else "")
        )
        return out

    def _split(self, task: ExperimentTask, content, samples: list[str], payload: dict, user: User) -> list[ExperimentTask]:
        """按拆分计划建子任务（不提交）。依赖按拆分方式：并行没有、首批验证其余等首批数据复核通过、逐批顺序。"""
        planned = self.split_parts(content, payload, samples)
        rows = planned["parts"]
        if len(rows) < 2:
            raise ValidationFailed(
                f"按这个分法只有 1 份（{rows[0]['size'] if rows else 0} 个样本），不需要拆分", code="split_invalid",
            )
        if len(rows) > 50:
            raise ValidationFailed("一次最多拆成 50 个子任务", code="split_invalid")
        created: list[ExperimentTask] = []
        for number, row in enumerate(rows, start=1):
            child = ExperimentTask(
                id=self.tasks.next_id(), org_id=self.ctx.org_id, plan_id=task.plan_id,
                plan_version=task.plan_version, plan_version_id=task.plan_version_id,
                title=f"{task.title} · {number}/{len(rows)}", owner_user_id=task.owner_user_id,
                reviewer_user_id=task.reviewer_user_id, sample_ids=row["sample_ids"], portion=row["portion"],
                due_at=task.due_at, priority=task.priority, note=f"由 {task.id} 拆分", created_by=user.id,
                state="unassigned", parent_id=task.id,
            )
            self.tasks.add(child)
            self.db.flush()
            created.append(child)
        edges = task_rules.split_dependencies([row.id for row in created], planned["mode"])
        for child in created:
            upstream, gate = edges[child.id]
            child.depends_on, child.dependency_gate = upstream, gate
        task.split_mode = planned["mode"]
        task.updated_at = now()
        self.tasks.bump(task)
        matrix_plan = content.plan_type == "matrix"
        self.audit.record(
            user, "拆分实验任务", task.id, after=f"{len(created)} 个子任务",
            detail=(
                ("整体执行 " + str(len(created)) + " 次" if planned["replicate"] else
                 f"{planned['total']} 个样本按流程每批 {planned['capacity']} 位分 {len(created)} 批（"
                 + "/".join(str(row["size"]) for row in rows) + "）")
                + f"；{task_rules.SPLIT_MODES[planned['mode']]}；子任务 "
                + "、".join(f"{child.id}（{self.part_label(row['portion'], row['size'], matrix_plan)}）"
                           for child, row in zip(created, rows))
            ),
            object_version=task.row_version,
        )
        return created

    def decompose(self, task_id: str, payload: dict, user: User) -> dict:
        """把任务拆成子任务，每个子任务一个批次。父任务不绑定批次，状态由子任务汇总。

        分法见 `split_parts`：缺省按最少批数均分（20 个、每批最多 8 个 → 7、7、6），也可以指定每批最多几个
        （装满）或分几份（均分）。拆分方式缺省并行——同一台设备一次只能跑一批是资源约束，排程按通道数
        自己会错开；首批验证与逐批顺序才加依赖。子任务继承方案、优先级、期望完成时间、负责人与复核人。
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
        self._split(task, content, samples, payload, user)
        self.db.commit()
        return self.out(task, detail=True)

    # ---------- 短缺的处置：补测或签名放弃 ----------

    def _require_split_parent(self, task_id: str) -> ExperimentTask:
        task = self.tasks.get(task_id)
        if not task:
            raise NotFound("实验任务不存在")
        if task.state == "cancelled":
            raise StateConflict("已取消的任务不能再补测或结束", code="task_cancelled")
        if not self.tasks.children(task.id):
            raise StateConflict(
                "只有拆分过的父任务才按样本汇总短缺；单个任务的不合格样本在数据复核里处理", code="task_not_split",
            )
        return task

    def _retest_samples(self, task: ExperimentTask) -> list[str]:
        """有样本清单的拆分：没有有效完成、也不在做或待做的那些物理样本（按出现顺序）。"""
        failed, settled = [], set()
        for leaf, batch in self.leaf_batches([task.id]):
            if batch is None or batch.state in {"planned", "scheduled"}:
                if leaf.state != "cancelled":
                    settled.update(leaf.sample_ids or [])
                continue
            top, bad = self._top_level_samples(batch)
            for row in top:
                if batch.state == "aborted" or row.id in bad:
                    failed.append(row.physical_sample_id)
                else:
                    settled.add(row.physical_sample_id)
        return [sample_id for sample_id in dict.fromkeys(failed) if sample_id and sample_id not in settled]

    def retest(self, task_id: str, payload: dict, user: User) -> dict:
        """补测：在父任务下新建补测子任务，补的是现有短缺。补测子任务照常建批次、排程、执行。

        有样本清单的补没有有效完成的那些样本（也可以指定）；按数量拆的补同样多个（也可以指定个数）；
        矩阵补缺样本的条件组，每组缺几个补几个——一个条件缺的样本不能拿别的条件补。超过流程每批样品位的
        分成几个补测子任务。已签名放弃的个数从补测量里扣掉。
        """
        task = self._require_split_parent(task_id)
        plan = self.plans.get(task.plan_id)
        if plan is None:
            raise NotFound("实验方案不存在")
        content = self._locked_content(task, plan)
        recipe = self._recipe_of(content)
        plate = int(recipe.plate or 0) if recipe else 0
        progress = self.progress(task)
        wanted_ids = [sid.strip() for sid in payload.get("sample_ids") or [] if sid and sid.strip()]
        wanted_count = int(payload.get("sample_count") or 0)
        if progress["shortfall"] <= 0 and not wanted_ids and not wanted_count:
            raise StateConflict(
                "没有短缺：计划的样本都已有效完成、正在做或已签名放弃", code="no_shortfall",
            )
        listed = any(child.sample_ids for child in self.tasks.children(task.id) if child.purpose != "retest")
        previous = [
            child for child in self.tasks.children(task.id) if child.purpose == "retest"
        ]
        specs: list[dict] = []
        try:
            if listed:
                from ..repositories.samples import PhysicalSampleRepository

                wanted = wanted_ids or self._retest_samples(task)[: progress["shortfall"]]
                if not wanted:
                    raise StateConflict("找不到需要补测的样本：请指定样本", code="no_shortfall")
                physical = PhysicalSampleRepository(self.db, self.ctx)
                unknown = [sample_id for sample_id in wanted if physical.get(sample_id) is None]
                if unknown:
                    raise NotFound(f"样本 {'、'.join(unknown)} 不存在或不在本组织")
                sizes = task_rules.split_sizes(len(wanted), plate)
                specs = [
                    {"sample_ids": wanted[start:start + size], "portion": {"offset": start}, "size": size}
                    for size, start in zip(sizes, task_rules.offsets(sizes))
                ]
            elif content.plan_type == "matrix":
                if wanted_count or wanted_ids:
                    raise ValueError("矩阵方案按缺样本的条件组补测，不能只给个数或样本清单")
                needed = {
                    group: max(0, row["target"] - row["valid"] - row["open"])
                    for group, row in progress["groups"].items()
                }
                # 已签名放弃的个数从缺得最多的条件组里扣（确定性的：同样多时按组号倒序）
                for _ in range(progress["accepted"]):
                    candidates = [group for group, count in needed.items() if count > 0]
                    if not candidates:
                        break
                    biggest = max(candidates, key=lambda group: (needed[group], group))
                    needed[biggest] -= 1
                needed = {group: count for group, count in sorted(needed.items()) if count > 0}
                if not needed:
                    raise StateConflict("没有需要补测的条件组", code="no_shortfall")
                repeats = max(1, int(content.repeats or 1))
                offset = repeats + sum(max((row.portion or {}).get("groups", {}).values(), default=0) for row in previous)
                chunk: dict[str, int] = {}
                for group, count in needed.items():
                    for _ in range(count):
                        if sum(chunk.values()) >= plate:
                            specs.append({"sample_ids": [], "portion": {"groups": chunk, "offset": offset},
                                          "size": sum(chunk.values())})
                            chunk = {}
                        chunk[group] = chunk.get(group, 0) + 1
                if chunk:
                    specs.append({"sample_ids": [], "portion": {"groups": chunk, "offset": offset},
                                  "size": sum(chunk.values())})
            else:
                count = wanted_count or progress["shortfall"]
                if count <= 0:
                    raise StateConflict("没有需要补测的样本", code="no_shortfall")
                base = progress["target"] + progress["retest"]
                sizes = task_rules.split_sizes(count, plate)
                specs = [
                    {"sample_ids": [], "portion": {"count": size, "offset": base + start}, "size": size}
                    for size, start in zip(sizes, task_rules.offsets(sizes))
                ]
        except ValueError as error:
            raise ValidationFailed(str(error), code="split_invalid") from error
        note = (payload.get("note") or "").strip()
        created = []
        for number, spec in enumerate(specs, start=len(previous) + 1):
            child = ExperimentTask(
                id=self.tasks.next_id(), org_id=self.ctx.org_id, plan_id=task.plan_id,
                plan_version=task.plan_version, plan_version_id=task.plan_version_id,
                title=f"{task.title} · 补测 {number}", owner_user_id=task.owner_user_id,
                reviewer_user_id=task.reviewer_user_id, sample_ids=spec["sample_ids"], portion=spec["portion"],
                due_at=task.due_at, priority=task.priority, created_by=user.id, state="unassigned",
                parent_id=task.id, purpose="retest", note=note or f"补 {task.id} 的短缺",
            )
            self.tasks.add(child)
            self.db.flush()
            created.append((child, spec))
        task.updated_at = now()
        self.tasks.bump(task)
        matrix_plan = content.plan_type == "matrix"
        self.audit.record(
            user, "新建补测子任务", task.id, before=f"短缺 {progress['shortfall']}",
            after=f"补测 {sum(spec['size'] for _, spec in created)} 个",
            detail="；".join(
                f"{child.id}（{self.part_label(spec['portion'], spec['size'], matrix_plan)}）" for child, spec in created
            ) + (f"；{note}" if note else ""),
            object_version=task.row_version,
        )
        self.db.commit()
        return self.out(task, detail=True)

    def accept_shortfall(self, task_id: str, payload: dict, user: User) -> dict:
        """按现有结果结束、不再补测：写明原因、电子签名，记在父任务上。之后再出现新的短缺还要再处置。"""
        task = self._require_split_parent(task_id)
        reason = (payload.get("reason") or "").strip()
        if not reason:
            raise ValidationFailed("放弃补测必须写明原因")
        progress = self.progress(task)
        if progress["shortfall"] <= 0:
            raise StateConflict("没有短缺，不需要放弃补测", code="no_shortfall")
        count = int(payload.get("count") or progress["shortfall"])
        if count > progress["shortfall"]:
            raise ValidationFailed(f"当前短缺 {progress['shortfall']} 个，放弃的个数不能超过它")
        from .identity_service import IdentityService

        signature = IdentityService(self.db, self.ctx).consume_signature(
            payload.get("signature_id"), user, "放弃补测，按现有结果结束", object_ref=task.id,
            object_version=task.row_version,
        )
        decisions = list(task.shortfall_decisions or [])
        decisions.append({
            "count": count, "reason": reason, "user_id": user.id, "user": user.display_name,
            "at": now().isoformat(timespec="seconds"), "signature_id": signature.id,
            "valid": progress["valid"], "target": progress["target"],
        })
        task.shortfall_decisions = decisions
        task.updated_at = now()
        self.tasks.bump(task)
        self.audit.record(
            user, "放弃补测", task.id, before=f"短缺 {progress['shortfall']}",
            after=f"放弃 {count} 个，按现有结果结束" if count == progress["shortfall"] else f"放弃 {count} 个",
            detail=f"计划 {progress['target']}，有效完成 {progress['valid']}；{reason}", sign=True,
            meaning=signature.meaning, signature_id=signature.id, object_version=task.row_version,
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
        self.db.flush()
        from .batch_service import BatchService
        from .plan_service import PlanService

        content = BatchService._pinned_plan(plan, version)
        samples = list(task.sample_ids or []) or (
            list(content.sample_ids or []) if content.plan_type != "matrix" else []
        )
        total = len(samples) or PlanService(self.db, self.ctx).sample_total(content)
        recipe = self._recipe_of(content)
        plate = int(recipe.plate or 0) if recipe else 0
        self.audit.record(
            user, "建立实验任务", task.id, before="—", after="待分配",
            detail=f"方案 {plan.id} v{version.version}；{total} 个样本",
            object_version=task.row_version,
        )
        split = payload.get("split")
        if split is not None or total > plate:
            # 超过流程每批样品位：建任务时就拆成子任务，每个子任务一个批次；不传分法按最少批数均分、并行
            self._split(task, content, samples, split or {}, user)
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
