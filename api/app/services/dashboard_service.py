"""工作台聚合。只读，因此可以直接组合其他服务的读方法。

计数与列表同范围：都走同一个访问上下文，组织外的任务不会出现在计数里。
"""
from __future__ import annotations

from collections import Counter

from sqlalchemy.orm import Session

from ..core.clock import now
from ..core.context import AccessContext
from ..models import User
from ..repositories.batches import BatchRepository, SampleRepository
from ..repositories.execution import CommandRepository
from ..repositories.governance import AlarmRepository
from ..repositories.metrics import ResultValueRepository
from ..repositories.recipes import ExperimentTaskRepository, PlanRepository, RecipeRepository
from ..repositories.reports import ReportVersionRepository
from ..repositories.resources import IslandRepository, StationRepository
from .alarm_service import AlarmService
from .asset_service import AssetService
from .batch_service import BatchService
from .gate_service import GateService
from .material_service import MaterialService
from .people_service import PeopleService
from .plan_service import PlanService
from .result_service import ResultService
from .schedule_service import ScheduleService
from .task_service import TaskService
from .workflow_service import WorkflowService


class DashboardService:
    def __init__(self, db: Session, ctx: AccessContext):
        self.db = db
        self.ctx = ctx
        self.batches = BatchRepository(db, ctx)
        self.samples = SampleRepository(db, ctx)
        self.commands = CommandRepository(db, ctx)
        self.alarms = AlarmRepository(db, ctx)
        self.recipes = RecipeRepository(db, ctx)
        self.plans = PlanRepository(db, ctx)
        self.tasks = ExperimentTaskRepository(db, ctx)
        self.values = ResultValueRepository(db, ctx)
        self.report_versions = ReportVersionRepository(db, ctx)
        self.stations = StationRepository(db, ctx)
        self.islands = IslandRepository(db)
        self.gate = GateService(db)
        self.batch_service = BatchService(db, ctx)
        self.schedule = ScheduleService(db, ctx)
        self.alarm_service = AlarmService(db, ctx)
        self.materials = MaterialService(db, ctx)
        self.results = ResultService(db, ctx)
        self.people = PeopleService(db, ctx)
        self.assets = AssetService(db, ctx)
        self.plan_service = PlanService(db, ctx)
        self.task_service = TaskService(db, ctx)
        self.workflow = WorkflowService(db, ctx)

    def kpi(self, window_hours: float = 24) -> dict:
        """运行驾驶舱指标（口径见 domain/kpi.py）。窗口默认最近 24 小时。"""
        from datetime import timedelta

        from ..domain import kpi
        from ..models import Allocation, AuditEvent, Batch, Command, ExceptionEvent, ExperimentTask, Station, StepRun
        from ..repositories.execution import MOTION

        moment = now()
        start = moment - timedelta(hours=max(1.0, min(float(window_hours), 24 * 31)))
        org = self.ctx.org_id
        batches = self.db.query(Batch).filter(Batch.org_id == org).all()
        states = Counter(batch.state for batch in batches)
        ended: dict[str, object] = {}
        for batch_id, ended_at in (
            self.db.query(StepRun.batch_id, StepRun.ended_at)
            .filter(StepRun.org_id == org, StepRun.ended_at.isnot(None)).all()
        ):
            if batch_id not in ended or ended_at > ended[batch_id]:
                ended[batch_id] = ended_at
        done = [batch for batch in batches if batch.state == "done" and ended.get(batch.id) and ended[batch.id] >= start]
        done_ids = [batch.id for batch in done]
        intervened: set[str] = set()
        if done_ids:
            for target, action in self.db.query(AuditEvent.target, AuditEvent.action).filter(
                AuditEvent.org_id == org, AuditEvent.target.in_(done_ids),
            ).all():
                if kpi.is_intervention(action):
                    intervened.add(target)
            for (batch_id,) in self.db.query(ExceptionEvent.batch_id).filter(
                ExceptionEvent.org_id == org, ExceptionEvent.batch_id.in_(done_ids),
                ExceptionEvent.state != "auto_resolved",
            ).all():
                intervened.add(batch_id)
        stations = {row.id: row for row in self.db.query(Station).all() if not row.retired}
        spans: dict[str, list[kpi.Span]] = {}
        for command in self.db.query(Command).filter(
            Command.org_id == org, Command.type.in_(list(MOTION)), Command.started_at.isnot(None),
            Command.updated_at >= start,
        ).all():
            finished = command.updated_at if command.state not in {"accepted", "running", "sent"} else moment
            spans.setdefault(command.station_id, []).append(kpi.Span(command.started_at, finished))
        planned: dict[str, list[kpi.Span]] = {}
        for allocation in self.db.query(Allocation).join(Batch, Batch.id == Allocation.batch_id).filter(
            Batch.org_id == org, Allocation.kind == "work", Allocation.ends_at >= start, Allocation.starts_at <= moment,
        ).all():
            planned.setdefault(allocation.station_id, []).append(kpi.Span(allocation.starts_at, allocation.ends_at))
        per_station = [
            {
                "station_id": station_id, "name": station.name, "channels": station.channels or 1,
                "utilization": round(kpi.utilization(spans.get(station_id, []), start, moment, station.channels or 1), 4),
                "planned_load": round(kpi.utilization(planned.get(station_id, []), start, moment, station.channels or 1), 4),
                "busy_min": round(kpi.clipped_minutes(spans.get(station_id, []), start, moment)),
            }
            for station_id, station in sorted(stations.items())
        ]
        busy_total = sum(row["busy_min"] for row in per_station)
        capacity_total = sum((moment - start).total_seconds() / 60 * row["channels"] for row in per_station)
        exceptions = self.db.query(ExceptionEvent).filter(
            ExceptionEvent.org_id == org, ExceptionEvent.created_at >= start,
        ).all()
        resolved = [row for row in exceptions if row.resolved_at]
        tasks_created = self.db.query(ExperimentTask).filter(
            ExperimentTask.org_id == org, ExperimentTask.created_at >= start,
        ).count()
        return {
            "window": {"start": start.isoformat(timespec="minutes"), "end": moment.isoformat(timespec="minutes"),
                       "hours": round((moment - start).total_seconds() / 3600, 1)},
            "experiments": {
                "running": states.get("running", 0),
                "queued": states.get("planned", 0) + states.get("scheduled", 0),
                "paused": states.get("paused", 0),
                "exception": states.get("fault", 0) + states.get("aborting", 0),
                "completed": len(done),
                "tasks_created": tasks_created,
            },
            "automation": {
                "completed": len(done),
                "without_intervention": len(done) - len(intervened & set(done_ids)),
                "success_rate": kpi.ratio(len(done) - len(intervened & set(done_ids)), len(done)),
            },
            "exceptions": {
                "raised": len(exceptions),
                "auto_resolved": len([row for row in exceptions if row.state == "auto_resolved"]),
                "open": len([row for row in exceptions if row.state in {"open", "manual"}]),
                "mttr_min": kpi.mean_minutes([(row.created_at, row.resolved_at) for row in resolved]),
            },
            "utilization": {
                "overall": round(busy_total / capacity_total, 4) if capacity_total else None,
                "stations": per_station,
            },
        }

    def _open_exceptions(self) -> int:
        from ..models import ExceptionEvent

        return self.db.query(ExceptionEvent).filter(
            ExceptionEvent.org_id == self.ctx.org_id, ExceptionEvent.state.in_(["open", "manual"]),
        ).count()

    def _pending_proposals(self) -> int:
        from ..models import ScheduleProposal

        return self.db.query(ScheduleProposal).filter(
            ScheduleProposal.org_id == self.ctx.org_id, ScheduleProposal.state == "pending",
        ).count()

    def overview(self, user: User) -> dict:
        batches = self.batches.list()
        active = [b for b in batches if b.state not in {"done", "aborted"}]
        states = Counter(b.state for b in batches)
        stations = self.stations.list()
        held = self.schedule.held_station_ids()

        my_tasks = self.task_service.my_tasks(user.id)
        pending_accept = self.task_service.pending_accept()
        manual_todos = self.workflow.my_manual_todos(user.id)
        review_todos = self.workflow.review_todos()
        pending_results = self.values.pending_review(limit=50)
        report_drafts, _ = self.report_versions.page(0, 50, "review")
        expiring_qualifications = self.people.expiring()
        expiring_lots = self.materials.expiry_warnings()
        unavailable_assets = [
            row for row in (self.assets.asset_out(a) for a in self.assets.assets.list())
            if row["unavailable_reasons"]
        ]

        todo = [
            {**self.batch_service.next_action(batch), "batch_id": batch.id, "state": batch.state}
            for batch in active
            if batch.state in {"planned", "scheduled", "paused", "fault"}
        ]

        return {
            "now": now().isoformat(timespec="seconds"),
            "organization_id": self.ctx.org_id,
            "gate": self.gate.status(),
            "counts": {
                "active_batches": len(active),
                "running": states.get("running", 0),
                "held": states.get("paused", 0) + states.get("fault", 0),
                "planned": states.get("planned", 0),
                "scheduled": states.get("scheduled", 0),
                "open_alarms": len(self.alarms.open_alarms()),
                "unknown_commands": self.commands.unknown_count(),
                "recipes_in_review": len([r for r in self.recipes.list() if r.state == "review"]),
                "plans_in_review": len(
                    [p for p in self.plans.list() if p.approval_state == "review"]
                ),
                "stations_online": len([s for s in stations if s.status != "offline"]),
                "stations_total": len(stations),
                # 我的待办：按用户与组织范围算，不把组织外任务混进计数
                "my_tasks": len(my_tasks),
                "pending_accept": len(pending_accept),
                "manual_steps": len(manual_todos),
                "step_reviews": len(review_todos),
                "pending_result_reviews": len(pending_results),
                "report_reviews": len(report_drafts),
                "expiring_qualifications": len(expiring_qualifications),
                "expiring_lots": len([row for row in expiring_lots if row["expired"]]),
                "unavailable_assets": len(unavailable_assets),
                "official_results_today": self._official_results_today(),
                "open_exceptions": self._open_exceptions(),
                "pending_proposals": self._pending_proposals(),
            },
            "my_tasks": my_tasks[:8],
            "pending_accept": pending_accept[:8],
            "manual_todos": manual_todos[:8],
            "review_todos": review_todos[:8],
            "pending_result_reviews": [
                {
                    "id": row.id, "analysis_task_id": row.analysis_task_id,
                    "metric_definition_id": row.metric_definition_id,
                    "result_version": row.result_version,
                    "created_at": row.created_at.isoformat(timespec="seconds"),
                }
                for row in pending_results[:8]
            ],
            "report_reviews": report_drafts[:5],
            "todo": todo,
            "hard_windows": self.batch_service.due_windows(),
            "alarms": [self.alarm_service.out(a) for a in self.alarms.list()[:6]],
            "active_batches": [self.batch_service.summary_out(b) for b in active],
            "islands": self._island_load(held),
            "bottleneck": self._bottleneck(),
            "recent_results": self.results.batches_with_results()[:5],
            "plans": [self._plan_brief(plan) for plan in self.plans.list()],
            "resources": {
                "expiring_qualifications": expiring_qualifications[:6],
                "unavailable_assets": unavailable_assets[:6],
            },
            "materials": {
                "expiring": expiring_lots,
                "waste": [t for t in self.materials.list_waste() if t["over_threshold"]],
            },
            "role": user.role,
        }

    def _plan_brief(self, plan) -> dict:
        return {
            "id": plan.id,
            "name": plan.name,
            "plan_type": plan.plan_type,
            "state": plan.state,
            "approval_state": plan.approval_state,
            "batches": self.plans.bound_batch_ids(plan.id),
            "sample_count": self.plan_service.sample_total(plan),
        }

    def _official_results_today(self) -> int:
        """今天新增的、可进入正式统计的结果数。

        口径明确：审核通过 + 质量有效 + 未被取代。不是「采集成功的条数」。
        """
        today = now().date()
        return len(
            [
                row for row in self.values.query().all()
                if row.created_at.date() == today
                and row.review_state == "approved"
                and row.quality == "valid"
                and not row.superseded_by_id
            ]
        )

    def _island_load(self, held: set[str]) -> list[dict]:
        rows = []
        for island in self.islands.list():
            members = [s for s in self.stations.list() if s.island == island.id]
            rows.append(
                {
                    "id": island.id,
                    "name": island.name,
                    "stations": len(members),
                    "running": len([s for s in members if s.status == "running"]),
                    "fault": len([s for s in members if s.status == "fault"]),
                    "held": len([s for s in members if s.id in held]),
                }
            )
        return rows

    def _bottleneck(self) -> dict | None:
        board = self.schedule.board()
        loads = []
        for lane in board["stations"]:
            minutes = 0.0
            for item in lane["items"]:
                if item["kind"] != "work":
                    continue
                from datetime import datetime

                start = datetime.fromisoformat(item["starts_at"])
                end = datetime.fromisoformat(item["ends_at"])
                minutes += (end - start).total_seconds() / 60
            if minutes:
                loads.append({"station_id": lane["id"], "name": lane["name"], "busy_min": round(minutes)})
        if not loads:
            return None
        return max(loads, key=lambda row: row["busy_min"])
