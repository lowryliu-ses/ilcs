"""批次用例。每个方法是一个事务边界；跨模块的一致性在这里保证。

与改造前的三个区别：
- 批次创建与实验任务绑定原子完成，不再存在「有批次没任务」的第二条数据链。
- 开跑检查按步骤适用性算，不再要求每一步都有设备工位。
- 下发只开起点步骤实例；之后的每一步由流程推进器按事件决定，
  设备回执不再顺手下发下一条命令。
- 子流程在建批次时展开进快照；运行时的跳过与「从指定节点重做」都要方法事先允许或签名负责。
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from ..core.clock import now
from ..core.config import settings
from ..core.context import AccessContext
from ..core.errors import NotFound, PermissionDenied, StateConflict, ValidationFailed
from ..domain import graph as dag
from ..domain import preflight, recovery
from ..domain.lifecycle import batch_delete_blockers
from ..domain.matrix import layout as well_layout
from ..domain.methods import command_method
from ..domain.permissions import ROLE_NAMES
from ..domain.resources import Window, evaluate_steps
from ..domain.scheduling import WORK
from ..domain.steps import (
    DEVICE, KIND_NAMES, consumes_materials, kind_of, needs_station, normalize, resource_demand,
    step_id_of,
)
from ..models import Batch, Command, PhysicalSample, Sample, SlotOccupancy, User
from ..repositories.batches import AllocationRepository, BatchRepository, ResultRepository, SampleRepository
from ..repositories.execution import (
    DISPATCHING, MOTION, CheckpointRepository, CommandRepository, TelemetryRepository,
)
from ..repositories.governance import AlarmRepository
from ..repositories.materials import ReservationRepository
from ..repositories.recipes import ExperimentTaskRepository, PlanRepository, PlanVersionRepository, RecipeRepository
from ..repositories.resources import CapabilityRepository, StationRepository
from ..repositories.samples import PhysicalSampleRepository
from ..repositories.workflow import StepRunRepository
from .asset_service import AssetService
from .audit_service import AuditService
from .gate_service import GateService
from .identity_service import IdentityService
from .material_service import MaterialService
from .people_service import PeopleService
from .sample_service import SampleService
from .schedule_service import ScheduleService
from .sop_service import SopService
from .workflow_service import WorkflowService

STATE_LABEL = {
    "planned": "计划", "scheduled": "已排程", "running": "运行中", "paused": "已保持",
    "fault": "故障", "aborting": "终止中", "aborted": "已终止", "done": "已完成",
}
HOLDABLE = {"running"}
RECOVERABLE = {"paused", "fault"}
UNDISPATCHED = {"planned", "scheduled"}


def held_min_estimate(batch: Batch) -> float:
    """恢复会把剩余时间窗整体后移已保持的时长；资质要覆盖后移后的结束时间。"""
    return max(0.0, (now() - (batch.held_at or now())).total_seconds() / 60)


class BatchService:
    def __init__(self, db: Session, ctx: AccessContext):
        self.db = db
        self.ctx = ctx
        self.batches = BatchRepository(db, ctx)
        self.allocations = AllocationRepository(db)
        self.samples = SampleRepository(db, ctx)
        self.physical = PhysicalSampleRepository(db, ctx)
        self.results = ResultRepository(db, ctx)
        self.commands = CommandRepository(db, ctx)
        self.checkpoints = CheckpointRepository(db)
        self.telemetry = TelemetryRepository(db)
        self.reservations = ReservationRepository(db, ctx)
        self.recipes = RecipeRepository(db, ctx)
        self.plans = PlanRepository(db, ctx)
        self.plan_versions = PlanVersionRepository(db, ctx)
        self.tasks = ExperimentTaskRepository(db, ctx)
        self.stations = StationRepository(db, ctx)
        self.capabilities = CapabilityRepository(db)
        self.alarms = AlarmRepository(db, ctx)
        self.runs = StepRunRepository(db, ctx)
        self.materials = MaterialService(db, ctx)
        self.schedule = ScheduleService(db, ctx)
        self.assets = AssetService(db, ctx)
        self.people = PeopleService(db, ctx)
        self.sops = SopService(db, ctx)
        self.workflow = WorkflowService(db, ctx)
        self.audit = AuditService(db, ctx)
        self.identity = IdentityService(db, ctx)
        self.gate = GateService(db)

    def _require(self, batch_id: str) -> Batch:
        batch = self.batches.get(batch_id)
        if not batch:
            raise NotFound("批次不存在")
        return batch

    def _require_locked(self, batch_id: str) -> Batch:
        """控制动作取批次行锁：与执行器投递、其他控制请求串行。"""
        batch = self.batches.lock(batch_id)
        if not batch:
            raise NotFound("批次不存在")
        return batch

    @staticmethod
    def _planned_start(steps: list[dict], first_work) -> datetime | None:
        """批次的计划开始：首个设备时间窗往前推掉它之前不占工位步骤的时长。

        流程以人工备料开头时，批次在首个设备时间窗之前就该开始，不能拿设备时间窗当开跑时刻。
        """
        if first_work is None:
            return None
        lead = sum(
            float(step.get("dur") or 0) for step in steps[: first_work.step_index]
            if not needs_station(step)
        )
        return first_work.starts_at - timedelta(minutes=lead)

    def _station_ids(self, batch: Batch, from_step: int = 0) -> set[str]:
        """本批次（自某一步起）占用的设备工位。执行门按它们判定单台设备的失联 / 超时。"""
        return {
            a.station_id for a in self.allocations.for_batch(batch.id)
            if a.step_index >= from_step and a.kind == WORK
        }

    def _withdraw_queued(self, batch: Batch, reason: str) -> int:
        """撤回尚未交给适配器的指令。保持 / 终止之后，队列里的动作不能再发出去。"""
        from .execution_service import ExecutionService

        execution = ExecutionService(self.db, self.ctx)
        return sum(
            1 for command in self.commands.queued_for_batch(batch.id)
            if execution.withdraw(command, reason)
        )

    def _simulation_blockers(self, batch: Batch) -> list[dict]:
        """正式环境里仍是模拟适配器的工位。它们会在没有硬件的情况下报告完成。"""
        if settings.simulation_allowed:
            return []
        Adapter = __import__("app.models", fromlist=["Adapter"]).Adapter
        blocked = []
        for station_id in sorted({a.station_id for a in self.allocations.for_batch(batch.id)}):
            adapter = self.db.get(Adapter, station_id)
            if adapter is None or adapter.kind != "real":
                blocked.append({
                    "key": "adapter",
                    "label": f"{station_id} 未配置真实设备驱动；正式环境禁止模拟执行",
                })
        return blocked

    # ---------- 读 ----------

    def steps_of(self, batch: Batch) -> list[dict]:
        return normalize(batch.recipe_snapshot.get("steps") or [])

    def summary_out(self, batch: Batch) -> dict:
        allocations = self.allocations.for_batch(batch.id)
        work = [a for a in allocations if a.kind == WORK]
        samples = self.samples.for_batch(batch.id)
        steps = self.steps_of(batch)
        return {
            "id": batch.id,
            "state": batch.state,
            "state_label": STATE_LABEL.get(batch.state, batch.state),
            "recipe_id": batch.recipe_id,
            "recipe_name": batch.recipe_snapshot.get("name"),
            "version": batch.recipe_snapshot.get("version"),
            "plan_id": batch.plan_id,
            "plan_version": batch.plan_version,
            "task_id": batch.task_id,
            "priority": batch.priority,
            "operator": batch.operator,
            "note": batch.note,
            "failure_reason": batch.failure_reason,
            "current_step": batch.current_step,
            "step_count": len(steps),
            "resource_demand": resource_demand(steps),
            "sample_count": len(samples),
            "sample_done": len([s for s in samples if s.state == "done"]),
            "starts_at": work[0].starts_at.isoformat(timespec="minutes") if work else None,
            "ends_at": max(a.ends_at for a in work).isoformat(timespec="minutes") if work else None,
            "created_at": batch.created_at.isoformat(timespec="seconds"),
            "held_at": batch.held_at.isoformat(timespec="seconds") if batch.held_at else None,
            "current_station": self._current_station_id(batch),
            "next_action": self.next_action(batch),
            "row_version": batch.row_version,
            "delete_blockers": batch_delete_blockers(batch.state, bool(self.commands.for_batch(batch.id))),
        }

    def _current_station_id(self, batch: Batch) -> str | None:
        allocation = self.allocations.work_step(batch.id, batch.current_step)
        return allocation.station_id if allocation else None

    def next_action(self, batch: Batch) -> dict:
        if batch.state == "planned":
            bom = batch.recipe_snapshot.get("bom") or []
            if bom and not self.materials.bom_satisfied(batch.id, bom):
                return {"who": "操作员", "what": "补齐物料预留", "why": "BOM 未满足，排程被阻塞"}
            return {"who": "操作员", "what": "排程", "why": "物料已预留，等待步骤级资源预约"}
        if batch.state == "scheduled":
            work = [a for a in self.allocations.for_batch(batch.id) if a.kind == WORK]
            when = work[0].starts_at.strftime("%m-%d %H:%M") if work else "无需排程"
            return {"who": "操作员", "what": "开跑检查", "why": f"计划 {when} 开始"}
        if batch.state in RECOVERABLE:
            return {"who": "操作员", "what": "恢复评估", "why": batch.failure_reason or batch.note or "已保持"}
        if batch.state == "running":
            run = self.runs.current(batch.id)
            if run is None:
                return {"who": "执行器", "what": "执行中", "why": f"第 {batch.current_step + 1} 步"}
            who = {
                "manual": "操作员", "review": "QA", "wait": "系统", "device": "执行器", "branch": "操作员",
            }.get(run.kind, "系统")
            what = {
                "manual": "填写人工记录", "review": "审核", "device": "执行中", "branch": "选择分支出口",
                "wait": (
                    f"等待事件 {((run.step_snapshot or {}).get('wait_for') or {}).get('event')}"
                    if ((run.step_snapshot or {}).get("wait_for") or {}).get("mode") == "event" else "等待到期"
                ),
            }.get(run.kind, "处理中")
            return {
                "who": who, "what": what,
                "why": f"第 {run.step_index + 1} 步「{(run.step_snapshot or {}).get('name', '')}」",
            }
        if batch.state == "done":
            return {"who": "研究员", "what": "数据复核与报告", "why": "运行结束，任务尚未完成"}
        return {"who": "—", "what": "无待办", "why": STATE_LABEL.get(batch.state, batch.state)}

    def list(self) -> list[dict]:
        return [self.summary_out(batch) for batch in self.batches.list()]

    def page(self, offset: int, limit: int, state: str | None = None, keyword: str = ""):
        rows, total = self.batches.page(offset, limit, state, keyword)
        return [self.summary_out(row) for row in rows], total

    def detail(self, batch_id: str, user: User) -> dict:
        batch = self._require(batch_id)
        steps = self.steps_of(batch)
        allocations = self.allocations.for_batch(batch.id)
        checkpoints = {c.step_index: c for c in self.checkpoints.for_batch(batch.id)}
        capability_names = self.capabilities.names()
        recoveries = {c.id: c.recovery for c in self.capabilities.list()}
        runs_by_step: dict[str, list] = {}
        for run in self.runs.for_batch(batch.id):
            runs_by_step.setdefault(run.step_id, []).append(run)
        before = dag.predecessors(steps)

        step_rows = []
        for index, step in enumerate(steps):
            work = next((a for a in allocations if a.step_index == index and a.kind == WORK), None)
            transfer = next((a for a in allocations if a.step_index == index and a.kind == "transfer"), None)
            checkpoint = checkpoints.get(index)
            step_id = step_id_of(step, index)
            attempts = runs_by_step.get(step_id, [])
            # 当前结论看最新一条有效记录：作废（重做）与取消的记录只留在尝试历史里
            live = [row for row in attempts if row.state not in {"superseded", "cancelled"}]
            latest = live[-1] if live else (attempts[-1] if attempts else None)
            step_rows.append(
                {
                    "index": index,
                    "step_id": step_id,
                    "kind": kind_of(step),
                    "kind_label": KIND_NAMES.get(kind_of(step), kind_of(step)),
                    "needs_station": needs_station(step),
                    "name": step.get("name"),
                    "cap": step.get("cap"),
                    "cap_name": capability_names.get(step.get("cap", ""), step.get("cap")),
                    "params": step.get("params"),
                    "dur": step.get("dur"),
                    "hard": step.get("hard"),
                    "form": step.get("form") or [],
                    "wait_for": step.get("wait_for") or {},
                    "review_role": step.get("review_role", ""),
                    "branch": step.get("branch") or {},
                    "when": step.get("when") or {},
                    "after": [steps[parent].get("step_id") for parent in before[index]],
                    "skippable": bool(step.get("skippable")),
                    "timeout": step.get("timeout") or None,
                    "groups": step.get("groups") or [],
                    "recovery": recoveries.get(step.get("cap"), {}),
                    "station_id": work.station_id if work else None,
                    "planned_start": work.starts_at.isoformat(timespec="minutes") if work else None,
                    "planned_end": work.ends_at.isoformat(timespec="minutes") if work else None,
                    "transfer_station_id": transfer.station_id if transfer else None,
                    "checkpoint_id": checkpoint.id if checkpoint else None,
                    "actual_end": checkpoint.created_at.isoformat(timespec="seconds") if checkpoint else None,
                    "attempts": [self.workflow.run_out(row) for row in attempts],
                    "run": self.workflow.run_out(latest) if latest else None,
                    "state": self._step_state(batch, index, latest, checkpoint is not None),
                }
            )

        return {
            **self.summary_out(batch),
            "snapshot": batch.recipe_snapshot,
            "plan_snapshot": batch.plan_snapshot,
            "sop_snapshot": batch.sop_snapshot or {},
            "steps": step_rows,
            "allocations": [self.schedule.allocation_out(a) for a in allocations],
            "samples": self.sample_rows(batch.id),
            "reservations": self.materials.list_reservations(batch.id),
            "inventory_ledger": self.materials.inventory.ledger_for_batch(batch.id),
            "step_runs": self.workflow.runs_for_batch(batch.id),
            "workflow_events": self.workflow.events_for_batch(batch.id),
            "signals": self.workflow.signals_for_batch(batch.id),
            "graph_mode": dag.graph_mode(steps),
            "subflows": batch.recipe_snapshot.get("subflows") or [],
            "labware": self._labware_out(batch),
            "commands": [
                {
                    "id": c.id, "type": c.type, "state": c.state, "station_id": c.station_id,
                    "step_index": c.step_index, "step_run_id": c.step_run_id,
                    "delivery_state": c.delivery_state, "after_command_id": c.after_command_id,
                    "checkpoint_id": c.checkpoint_id, "error": c.error,
                    "created_at": c.created_at.isoformat(timespec="seconds"),
                }
                for c in self.commands.for_batch(batch.id)
            ],
            "checkpoints": [
                {"id": c.id, "step_index": c.step_index, "state": c.state, "payload": c.payload,
                 "created_at": c.created_at.isoformat(timespec="seconds")}
                for c in self.checkpoints.for_batch(batch.id)
            ],
            "alarms": [
                {"id": a.id, "severity": a.severity, "state": a.state, "message": a.message,
                 "condition_active": a.condition_active}
                for a in self.alarms.for_source("batch", batch.id)
            ],
            "audit": [
                {"time": e.time.isoformat(timespec="seconds"), "user": e.user, "action": e.action,
                 "before": e.before, "after": e.after, "detail": e.detail, "sign": e.sign,
                 "meaning": e.meaning}
                for e in self.audit.for_target(batch.id)
            ],
            "telemetry": [
                {"station_id": t.station_id, "metric": t.metric, "setpoint": t.setpoint,
                 "value": t.value, "quality": t.quality, "origin": t.origin,
                 "device_ts": t.device_ts.isoformat(timespec="seconds")}
                for t in self.telemetry.latest_per_metric(batch.id)
            ],
            "gate": self.gate.status(),
            "preflight": self.preflight(batch, user, manual_review=False) if batch.state == "scheduled" else None,
            "can_control": self.ctx.has("batch.control"),
        }

    def _labware_out(self, batch: Batch) -> dict | None:
        from .transfer_service import TransferService

        transfers = TransferService(self.db, self.ctx)
        labware = transfers.for_batch(batch.id)
        return transfers.labware_out(labware) if labware else None

    def telemetry_series(self, batch_id: str) -> dict:
        batch = self._require(batch_id)
        capability_names = self.capabilities.names()
        params_by_capability = {c.id: (c.params or {}) for c in self.capabilities.list()}
        step_of_metric: dict[str, dict] = {}
        for step in self.steps_of(batch):
            for metric in (step.get("params") or {}):
                step_of_metric.setdefault(metric, {
                    "label": params_by_capability.get(step.get("cap"), {}).get(metric, metric),
                    "cap_name": capability_names.get(step.get("cap", ""), step.get("cap")),
                })

        recipe = self.recipes.get(batch.recipe_id)
        golden_id = (recipe.golden_batch_id or "") if recipe else ""
        golden_values = self._series_values(golden_id) if golden_id and golden_id != batch_id else {}

        series = []
        for key, points in self._series_points(batch_id).items():
            station_id, metric = key
            meta = step_of_metric.get(metric, {})
            series.append({
                "station_id": station_id,
                "metric": metric,
                "label": meta.get("label", metric),
                "capability": meta.get("cap_name", ""),
                "setpoint": points[0]["setpoint"],
                "origin": points[0].get("origin", "simulation"),
                "points": points,
                "golden": golden_values.get(key, []),
            })
        return {
            "batch_id": batch_id,
            "state": batch.state,
            "golden_batch_id": golden_id if golden_values else "",
            "series": sorted(series, key=lambda s: (s["station_id"], s["metric"])),
        }

    def _series_points(self, batch_id: str) -> dict[tuple[str, str], list[dict]]:
        grouped: dict[tuple[str, str], list[dict]] = {}
        for point in self.telemetry.series_for_batch(batch_id):
            grouped.setdefault((point.station_id, point.metric), []).append({
                "t": point.device_ts.isoformat(timespec="seconds"),
                "v": point.value,
                "setpoint": point.setpoint,
                "quality": point.quality,
                "origin": point.origin,
            })
        return grouped

    def _series_values(self, batch_id: str) -> dict[tuple[str, str], list[float | None]]:
        return {key: [p["v"] for p in points] for key, points in self._series_points(batch_id).items()}

    def _step_state(self, batch: Batch, index: int, run, has_checkpoint: bool) -> str:
        if run is not None:
            return run.state
        if has_checkpoint:
            return "completed"
        if batch.state in {"aborted", "done"}:
            return "cancelled" if not has_checkpoint else "completed"
        if index == batch.current_step and batch.state == "running":
            return "running"
        if index == batch.current_step and batch.state in RECOVERABLE:
            return "unknown"
        return "pending"

    def sample_rows(self, batch_id: str) -> list[dict]:
        samples = self.samples.for_batch(batch_id)
        results = self.results.for_samples([s.id for s in samples])
        rows = []
        for sample in samples:
            result = results.get(sample.id)
            physical = self.physical.get(sample.physical_sample_id) if sample.physical_sample_id else None
            rows.append(
                {
                    "id": sample.id,
                    "physical_sample_id": sample.physical_sample_id,
                    "barcode": physical.barcode if physical else "",
                    "container_id": sample.container_id,
                    "well": sample.well,
                    "position": sample.position,
                    "condition_group": sample.condition_group,
                    "condition_label": sample.condition_label,
                    "repeat": sample.repeat,
                    "levels": sample.levels,
                    "is_control": sample.is_control,
                    "state": sample.state,
                    "legacy_quality": sample.quality,
                    "flag_note": sample.flag_note,
                    "metrics": {
                        "areal_density": result.areal_density if result else None,
                        "discharge_capacity": result.discharge_capacity if result else None,
                        "retention": result.retention if result else None,
                    },
                    "raw_uri": result.raw_uri if result else "",
                }
            )
        return rows

    # ---------- 创建 ----------

    def create(
        self, plan_id: str, priority: int, note: str, user: User, task_id: str = "",
    ) -> dict:
        """原子创建：冻结快照 → 预留物料 → 生成运行分配 → 绑定实验任务。任一步失败整体回滚。"""
        if not self.ctx.has("batch.create"):
            raise PermissionDenied("当前角色不能创建批次")
        plan = self.plans.get(plan_id)
        if not plan:
            raise NotFound("实验方案不存在")
        if plan.state != "locked":
            raise StateConflict("只能基于已锁定结构的实验方案创建批次")
        if plan.approval_state != "approved":
            raise StateConflict(
                "方案尚未批准，不能创建正式批次",
                {"blocked": [{"key": "plan", "label": "矩阵锁定只是结构冻结，不等于已审批"}]},
                code="plan_not_approved",
            )
        recipe = self.recipes.get(plan.recipe_id)
        if not recipe:
            raise NotFound("方法不存在")
        if recipe.state != "released" or recipe.needs_revision:
            raise StateConflict("只能从有效的已发布方法创建批次")

        from .task_service import TaskService

        task_service = TaskService(self.db, self.ctx)
        task = None
        if task_id:
            task = self.tasks.get(task_id)
            if task is None:
                raise NotFound("实验任务不存在")
            if task.batch_id:
                raise StateConflict(
                    f"该任务已绑定批次 {task.batch_id}，同一任务不产生重复批次",
                    code="task_already_has_batch",
                )
            if self.tasks.children(task.id):
                # 先于物料预留判：父任务不直接执行，不该为它占一份物料再回滚
                raise StateConflict(
                    "父任务不直接执行：请在它的子任务上建批次",
                    {"blocked": [{"key": "task", "label": "任务已拆成子任务，状态由子任务汇总"}]},
                    code="task_has_children",
                )

        version = self.plan_versions.latest_approved(plan.id)
        batch = Batch(
            id=self.batches.next_id(now()),
            org_id=self.ctx.org_id,
            plan_id=plan.id,
            plan_version=version.version if version else plan.version,
            recipe_id=recipe.id,
            state="planned",
            priority=priority,
            operator=user.display_name,
            note=note,
            recipe_snapshot=self._freeze_recipe(recipe),
            plan_snapshot=self._freeze_plan(plan),
            sop_snapshot=self.sops.snapshot_for(recipe.sop_version_id) if recipe.sop_version_id else {},
        )
        self.batches.add(batch)
        bom = batch.recipe_snapshot.get("bom") or []
        if bom:
            self.materials.reserve_for_batch(batch.id, bom, user)
        rows = self._generate_samples(batch, plan, task.sample_ids if task is not None else None)
        if plan.plan_type == "matrix":
            from ..domain.matrix import condition_params

            # 按孔位冻结因子作用参数：之后方案怎么改，这个批次的设备参数都不变
            expanded = condition_params(plan.factors or [], rows)
            if expanded:
                batch.plan_snapshot = {**batch.plan_snapshot, "condition_params": expanded}
        self.plans.link_batch(plan.id, batch.id)
        if task is None:
            task = task_service.ensure_task_for_batch(batch.id, plan.id, user)
        else:
            task_service.bind_batch(task, batch.id)
        batch.task_id = task.id
        self.audit.record(
            user, "新建批次", batch.id, before="—", after="计划", object_version=batch.row_version,
            detail=(
                f"{recipe.id} v{recipe.version} 快照冻结；方案 {plan.id} v{batch.plan_version}；"
                f"{len(self.samples.for_batch(batch.id))} 个运行分配；任务 {task.id}"
                + (f"；SOP {batch.sop_snapshot.get('code')} {batch.sop_snapshot.get('version')}"
                   if batch.sop_snapshot else "")
            ),
        )
        self.db.commit()
        return self.summary_out(batch)

    def _freeze_recipe(self, recipe) -> dict:
        """冻结方法快照。子流程在这里展开：之后被引用的方法怎么修订，这个批次的步骤都不变。"""
        from ..domain.subflow import SubflowError, has_subflow, merge_bom
        from .flow_expansion import expanded_steps, resolved_steps

        steps = normalize(recipe.steps or [])
        bom = list(recipe.bom or [])
        subflows: list[dict] = []
        if has_subflow(steps):
            try:
                steps, extra_bom = expanded_steps(self.db, self.ctx, recipe)
            except SubflowError as error:
                raise StateConflict(
                    f"子流程无法展开：{error.message}",
                    {"blocked": [{"key": "subflow", "label": error.message}]}, code="subflow_invalid",
                ) from error
            bom = merge_bom(bom, extra_bom)
            seen: set[str] = set()
            for step in steps:
                for group in step.get("groups") or []:
                    if group["step_id"] not in seen:
                        seen.add(group["step_id"])
                        subflows.append(group)
        # 设备方法引用（含子方法里的）：补缺省参数并冻结方法快照；引用失效（未发布 / 已退役 / 不一致）不建批次
        steps, method_problems = resolved_steps(self.db, self.ctx, steps)
        if method_problems:
            labels = [f"{step_id}：{problem}" for step_id, rows in method_problems.items() for problem in rows]
            raise StateConflict(
                f"设备方法引用失效：{labels[0]}",
                {"blocked": [{"key": "method", "label": label} for label in labels]}, code="method_invalid",
            )
        return copy.deepcopy(
            {
                "id": recipe.id, "name": recipe.name, "version": recipe.version, "plate": recipe.plate,
                "risk": recipe.risk, "design": recipe.design,
                "steps": steps, "bom": bom,
                "sop_version_id": recipe.sop_version_id,
                "subflows": subflows,
                "frozen_at": now().isoformat(timespec="seconds"),
            }
        )

    @staticmethod
    def _freeze_plan(plan) -> dict:
        return copy.deepcopy(
            {
                "id": plan.id, "name": plan.name, "plan_type": plan.plan_type, "goal": plan.goal,
                "repeats": plan.repeats, "layout": plan.layout, "seed": plan.seed,
                "factors": plan.factors, "control": plan.control,
                "design_points": plan.design_points or [], "round_no": plan.round_no,
                "sample_count": plan.sample_count, "sample_ids": plan.sample_ids,
                "required_metrics": plan.required_metrics, "version": plan.version,
            }
        )

    def _generate_samples(self, batch: Batch, plan, task_samples: list[str] | None = None) -> list[dict]:
        """生成运行分配。

        矩阵方案按孔位布局生成；单条件与委托方案按样本清单或样本数生成——任务自己带了样本清单
        （例如拆分出来的子任务）就用任务的，否则用方案的。
        每个分配都指向一个物理样本——清单里给了就用它，否则登记一个新的。
        """
        sample_service = SampleService(self.db, self.ctx)
        container_id = f"PL-{batch.id.replace('B-', '')}"
        plan_type = plan.plan_type
        digits = batch.id.replace("B-", "").replace("-", "")

        if plan_type == "matrix":
            assignments = well_layout(
                plan.factors or [], plan.control, plan.repeats,
                batch.recipe_snapshot.get("plate", 0), plan.layout, plan.seed,
                plan.design_points or None,
            )
            rows = [
                {
                    "well": a.well, "group": a.group, "label": a.label, "repeat": a.repeat,
                    "levels": list(a.levels), "is_control": a.is_control, "physical_id": "",
                }
                for a in assignments
            ]
        else:
            listed = list(task_samples or plan.sample_ids or [])
            count = len(listed) or plan.sample_count
            from ..domain.matrix import well_grid

            wells = well_grid(max(count, 1))
            rows = [
                {
                    "well": wells[index] if index < len(wells) else f"P{index + 1}",
                    "group": "C01", "label": "单一条件", "repeat": index + 1, "levels": [],
                    "is_control": False,
                    "physical_id": listed[index] if index < len(listed) else "",
                }
                for index in range(count)
            ]

        for position, row in enumerate(rows):
            physical_id = row["physical_id"]
            if physical_id:
                physical = self.physical.get(physical_id)
                if physical is None:
                    raise NotFound(f"方案引用的样本 {physical_id} 不存在或不在当前组织范围内")
            else:
                physical_id = f"S{digits}-{row['well']}"
                if self.physical.get(physical_id) is None:
                    self.physical.add(
                        PhysicalSample(
                            id=physical_id, org_id=self.ctx.org_id,
                            project_id=plan.project_id or "",
                            barcode=physical_id, source=f"方案 {plan.id} 生成",
                            # 样本类型是业务分类，不是方法名；没录入就留空，
                            # 不塞一个看起来像分类的字符串进去
                            sample_type="",
                            current_location=container_id, custodian=batch.operator,
                            lifecycle_state="in_use", origin="batch_generated",
                            created_by=self.ctx.subject_id,
                        )
                    )
            assignment = Sample(
                id=f"S{digits}-{row['well']}",
                org_id=self.ctx.org_id,
                physical_sample_id=physical_id,
                batch_id=batch.id,
                container_id=container_id,
                well=row["well"],
                position=position,
                condition_group=row["group"],
                condition_label=row["label"],
                repeat=row["repeat"],
                levels=row["levels"],
                is_control=row["is_control"],
            )
            self.samples.add(assignment)
            # 在途孔位占用：唯一约束挡住两个样本占同一个孔
            sample_service.occupy_slot(container_id, row["well"], physical_id, assignment.id)
        return rows

    # ---------- 排程 ----------

    def schedule_batch(self, batch_id: str, start_from: datetime | None, prefer: str | None, user: User) -> dict:
        if not self.ctx.has("batch.schedule"):
            raise PermissionDenied("当前角色不能排程")
        batch = self._require(batch_id)
        self.schedule.schedule(batch, start_from, prefer, user)
        self.db.commit()
        return self.summary_out(batch)

    def unschedule(self, batch_id: str, user: User) -> dict:
        if not self.ctx.has("batch.schedule"):
            raise PermissionDenied("当前角色不能排程")
        batch = self._require(batch_id)
        if batch.state != "scheduled":
            raise StateConflict("只有已排程且未下发的批次可以取消排程")
        self.gate.require_open()
        released = len(self.allocations.for_batch(batch.id))
        self.allocations.delete_for_batch(batch.id)
        batch.state = "planned"
        self.audit.record(
            user, "取消排程", batch.id, before="已排程", after="计划",
            detail=f"归还 {released} 个工位时间窗；物料预留保持不变",
        )
        self.db.commit()
        return self.summary_out(batch)

    def reschedule(self, batch_id: str, from_step: int, start_from: datetime, user: User) -> dict:
        if not self.ctx.has("batch.schedule"):
            raise PermissionDenied("当前角色不能排程")
        batch = self._require(batch_id)
        return self.schedule.reschedule_from(batch, from_step, start_from, user)

    def delete(self, batch_id: str, user: User) -> dict:
        """删除未下发的批次。

        只解除符合条件的关联：运行分配、预留与工位时间窗跟着走，
        物理样本不删——它可能已独立登记、流转，或被别的运行引用。
        """
        if not self.ctx.has("batch.control"):
            raise PermissionDenied("当前角色不能删除批次")
        batch = self._require(batch_id)
        commands = self.commands.for_batch(batch.id)
        blockers = batch_delete_blockers(batch.state, bool(commands))
        if blockers:
            raise StateConflict("批次不可删除", {"blocked": [{"key": "batch", "label": b} for b in blockers]})

        assignments = self.samples.for_batch(batch.id)
        sample_service = SampleService(self.db, self.ctx)
        kept: list[str] = []
        removable_physical = []
        for assignment in assignments:
            physical = (
                self.physical.get(assignment.physical_sample_id)
                if assignment.physical_sample_id else None
            )
            if physical is None:
                continue
            transfers = sample_service.transfers.for_sample(physical.id)
            other_runs = [
                row for row in self.samples.for_physical(physical.id) if row.batch_id != batch.id
            ]
            # 只清理这次批次自己生成的样本；人工登记、分样、迁移来的都保留
            generated = physical.origin == "batch_generated"
            if transfers or other_runs or physical.parent_id or not generated:
                kept.append(physical.id)
            else:
                removable_physical.append(physical)
        allocations = len(self.allocations.for_batch(batch.id))
        reservations = len(self.materials.list_reservations(batch.id))
        sample_service.release_slots(f"PL-{batch.id.replace('B-', '')}")
        # 任务要跟着解绑，否则它会一直指向一个已经不存在的批次，
        # 也会挡住同一任务重新建批次
        task = self.tasks.by_batch(batch.id)
        if task is not None:
            task.batch_id = ""
            task.updated_at = now()
            self.tasks.bump(task)
        self.audit.record(
            user, "删除批次", batch.id, before=STATE_LABEL.get(batch.state, batch.state), after="已删除",
            detail=(
                f"未下发设备；归还 {allocations} 个工位时间窗、释放 {reservations} 项物料预留、"
                f"移除 {len(assignments)} 个运行分配；"
                f"保留 {len(kept)} 个已独立登记或已流转的物理样本"
                + (f"（{'、'.join(kept[:5])}）" if kept else "")
            ),
        )
        self.batches.purge(batch.id)
        # 先删运行分配，再删只由这次批次生成且从未流转的物理样本；反过来删会被外键拒绝。
        removable_ids = [physical.id for physical in removable_physical]
        if removable_ids:
            removable_slots = self.db.query(SlotOccupancy).filter(
                SlotOccupancy.physical_sample_id.in_(removable_ids)
            ).all()
            for occupancy in removable_slots:
                self.db.delete(occupancy)
            self.db.flush()
        for physical in removable_physical:
            self.db.delete(physical)
        self.db.commit()
        return {"id": batch_id, "deleted": True, "kept_samples": kept}

    # ---------- 开跑检查与下发 ----------

    def preflight(self, batch: Batch, user: User, manual_review: bool) -> dict:
        snapshot = batch.recipe_snapshot
        source = self.recipes.get(snapshot.get("id"))
        steps = self.steps_of(batch)
        allocations = self.allocations.for_batch(batch.id)
        work = [a for a in allocations if a.kind == WORK]
        demand = resource_demand(steps)
        first_work = next(
            (a for a in sorted(work, key=lambda row: row.step_index)), None
        )
        station = self.stations.get(first_work.station_id) if first_work else None
        adapter = (
            self.db.get(__import__("app.models", fromlist=["Adapter"]).Adapter, station.id)
            if station else None
        )
        reservations = self.materials.list_reservations(batch.id)

        windows = {
            a.step_index: Window(a.starts_at, a.ends_at) for a in work
        }
        station_of_step = {a.step_index: a.station_id for a in work}
        resource_checks = [
            row.as_dict()
            for row in evaluate_steps(steps, windows, self.assets.specs_by_station(), station_of_step)
        ]
        task = self.tasks.get(batch.task_id) if batch.task_id else None
        executor_id = (task.assignee_user_id if task else "") or user.id
        # 资质覆盖整个计划执行时段：首步开始有效、中途到期同样挡住
        last_end = max((a.ends_at for a in work), default=None)
        qualification_blockers = self.people.blockers_for_steps(
            executor_id, steps, first_work.starts_at if first_work else now(), until=last_end,
        )
        if batch.sop_snapshot:
            qualification_blockers += self.sops.ack_blockers(
                batch.sop_snapshot.get("sop_version_id", ""), executor_id
            )
        context = preflight.PreflightContext(
            recipe_state=source.state if source else "",
            recipe_risk=snapshot.get("risk", ""),
            snapshot_version=snapshot.get("version", ""),
            released_version=source.version if source and source.state == "released" else "",
            steps_total=demand["total"],
            steps_needing_station=demand["needs_station"],
            steps_allocated=len(work),
            resource_checks=resource_checks,
            reservations=reservations,
            bom_items=snapshot.get("bom") or [],
            material_steps=len([s for s in steps if consumes_materials(s)]),
            bom_satisfied=self.materials.bom_satisfied(batch.id, snapshot.get("bom") or []),
            expired_lots=self.materials.expired_reserved_lots(batch.id),
            first_station=(
                {
                    "id": station.id, "clean": station.clean, "status": station.status,
                    "cal_due": station.cal_due,
                    "interlock": bool(adapter and adapter.site_interlock),
                }
                if station else None
            ),
            station_alarm_active=(
                bool(station and self.alarms.active_on_station(station.id))
                or bool(self.alarms.unresolved_for_batch(batch.id))
            ),
            gate_reasons=self.gate.reasons_for(self._station_ids(batch)),
            planned_start=self._planned_start(steps, first_work),
            now=now(),
            expiry_min=settings.schedule_expiry_min,
            early_tolerance_min=settings.early_start_tolerance_min,
            has_control_permission=self.ctx.has("batch.control"),
            role_name=ROLE_NAMES.get(user.role, user.role),
            manual_review_done=manual_review,
            qualification_required=self.people.requires_qualification(steps),
            qualification_blockers=qualification_blockers,
            sop_snapshot=batch.sop_snapshot or None,
            dependency_blockers=self._dependency_blockers(task),
        )
        checks = preflight.evaluate(context)
        return {
            "checks": [c.as_dict() for c in checks],
            "ok": not preflight.blocked(checks),
            "blocked": [c.as_dict() for c in preflight.blocked(checks)],
            "summary": preflight.summary(checks),
            "first_station_id": first_work.station_id if first_work else None,
            "sample_count": len(self.samples.for_batch(batch.id)),
            "pallet_code": f"PL-{batch.id.replace('B-', '')}",
            "resource_checks": resource_checks,
        }

    def _dependency_blockers(self, task) -> list[str] | None:
        if task is None or not task.depends_on:
            return None
        from .task_service import TaskService

        return [row["label"] for row in TaskService(self.db, self.ctx).dependency_blockers(task)]

    def dispatch(self, batch_id: str, manual_review: bool, reason: str, signature_id: str, user: User) -> dict:
        if not self.ctx.has("batch.control"):
            raise PermissionDenied("当前角色不能下发批次")
        batch = self._require_locked(batch_id)
        if batch.state != "scheduled":
            raise StateConflict("只有已排程批次可下发")
        self.gate.require_open(self._station_ids(batch))
        simulated = self._simulation_blockers(batch)
        if simulated:
            raise StateConflict(
                "存在未接入真实设备的工位", {"blocked": simulated}, code="simulation_adapter",
            )
        result = self.preflight(batch, user, manual_review)
        if not result["ok"]:
            raise StateConflict("开跑检查未通过", {"blocked": result["blocked"]})
        steps = self.steps_of(batch)
        if steps and kind_of(steps[0]) == DEVICE:
            from .transfer_service import TransferService

            first = self.allocations.work_step(batch.id, 0)
            TransferService(self.db, self.ctx).require_ready(batch, 0, first.station_id if first else "")
        signature = self.identity.consume_signature(
            signature_id, user, "下发批次执行", object_ref=batch.id, object_version=batch.row_version
        )

        batch.state = "running"
        batch.current_step = 0
        batch.failure_reason = ""
        self.batches.bump(batch)
        self.samples.mark_all(batch.id, "running")
        task = self.tasks.get(batch.task_id) if batch.task_id else None
        # 起点一起开出：设备起点下指令，关卡 / 拆分 / 分支即时判定，人工与审核留待办
        entered = self.workflow.start(batch, assignee_user_id=(task.assignee_user_id if task else user.id))
        if not entered:
            raise StateConflict("流程没有可开出的起点步骤")
        run = entered[0]["run"]
        command_id = next((row.get("command_id") for row in entered if row.get("command_id")), "")
        self.audit.record(
            user, "下发批次", batch.id, sign=True, meaning=signature.meaning, before="已排程",
            after="运行中", signature_id=signature.id,
            command_id=command_id or "", object_version=batch.row_version,
            detail=(
                f"开跑检查 {result['summary']['passed']} 项通过、"
                f"{result['summary']['not_applicable']} 项不适用"
                f"{'；' + reason if reason else ''}；"
                + (
                    f"首节点为{KIND_NAMES.get(run.kind, run.kind)}步骤" if len(entered) == 1
                    else f"{len(entered)} 个起点同时开出"
                )
            ),
        )
        self.db.commit()
        return {**self.summary_out(batch), "first_step_run": self.workflow.run_out(run)}

    def issue_command(
        self, batch: Batch, command_type: str, step_index: int, step_run_id: str = "",
        *, station_id: str | None = None, capability: str | None = None,
    ) -> Command:
        """生成一条设备指令。

        设备动作（dispatch / resume / retry）前，批次绑定了载具且板不在目标工位上时，先生成一条
        转运指令并把它设为本指令的前置：板没被确认送到，设备收不到动作。转运计划不成立（位置
        未知、放置位满、没有可用承运工位）时本指令直接判为未投递并挂起批次，原因写清楚。
        保持 / 终止可以指定工位：在途的是转运时，要停的是承运工位而不是步骤工位。
        """
        steps = self.steps_of(batch)
        step = steps[step_index] if step_index < len(steps) else {}
        allocation = self.allocations.work_step(batch.id, step_index)
        not_before = None
        if command_type == "dispatch" and allocation is not None:
            # 按时开工：晚了就把本批下游顺延（与别的批次冲突则报警，不挤占）；
            # 早了就让执行器等到时间窗开始前的允许提前量再投递
            self.schedule.realign(batch, step_index, now())
            not_before = allocation.starts_at - timedelta(minutes=settings.early_start_tolerance_min)
        params = dict(step.get("params") or {})
        wells = (batch.plan_snapshot or {}).get("condition_params", {}).get(step_id_of(step, step_index)) if step else None
        if wells and command_type in DISPATCHING:
            # 矩阵条件：一条指令带全部孔位的参数，设备按孔位执行；步骤里的固定参数是未覆盖孔位的缺省值
            params["wells"] = wells
        target_station = station_id if station_id is not None else (allocation.station_id if allocation else "")
        command = Command(
            org_id=batch.org_id or self.ctx.org_id,
            batch_id=batch.id,
            step_run_id=step_run_id,
            station_id=target_station,
            capability=capability if capability is not None else step.get("cap", ""),
            params=params,
            # 转运、保持、终止不是按方法做的动作，只有设备动作带方法
            method=command_method(step) if command_type in DISPATCHING and capability is None else {},
            type=command_type,
            state="sent",
            delivery_state="queued",
            step_index=step_index,
            not_before=not_before,
        )
        self.db.add(command)
        self.db.flush()
        if command_type in DISPATCHING and target_station:
            from .transfer_service import TransferService

            transfer, blocker = TransferService(self.db, self.ctx).prepare(
                batch, step_index, target_station, step_run_id,
            )
            if transfer is not None:
                command.after_command_id = transfer.id
            elif blocker:
                self._refuse_unsent(batch, command, f"载具不能送到 {target_station}：{blocker}")
            self.db.flush()
        return command

    def _refuse_unsent(self, batch: Batch, command: Command, reason: str) -> None:
        """指令没离开系统就判为不能投递：记入幂等台账，批次挂起报警。不提交——由调用方的事务决定。"""
        from ..models import AdapterExecution
        from .execution_service import ExecutionService

        self.db.add(AdapterExecution(command_id=command.id, station_id=command.station_id, state="rejected"))
        ExecutionService(self.db, self.ctx).fault(batch, command, reason, delivery="unreachable")

    # ---------- 保持 ----------

    def hold(self, batch_id: str, reason: str, user: User) -> dict:
        if not self.ctx.has("batch.control"):
            raise PermissionDenied("当前角色不能控制批次")
        batch = self._require_locked(batch_id)
        if batch.state not in HOLDABLE:
            raise StateConflict("只有运行中批次可请求保持")
        # 保持是安全动作：执行门关闭（心跳超时、联锁）时恰恰最需要它，不受执行门限制
        run = self.runs.current(batch.id)

        def device_acting() -> list:
            # 设备在动作：有在途动作指令，或当前设备步骤已被设备接受
            in_flight = self.commands.in_flight_for_batch(batch.id, DISPATCHING)
            if in_flight or (run is not None and run.kind == DEVICE and run.state == "running"):
                return in_flight or [None]
            return []

        moving = [c for c in self.commands.in_flight_for_batch(batch.id, {"transfer"})]
        if moving:
            # 板搬到一半停下既不在起点也不在终点；转运不可保持，只能等它完成或终止
            raise StateConflict(
                f"载具转运进行中（{moving[0].station_id}），不能保持：等转运完成后再保持，或直接终止",
                code="transfer_in_progress",
            )
        recovery_rules = self._recovery_rules(batch)
        if device_acting() and not recovery_rules.get("pausable", False):
            raise StateConflict(
                f"当前步骤能力不允许保持：{recovery_rules.get('sideEffect', '不可中断')}",
            )
        withdrawn = self._withdraw_queued(batch, "批次请求保持，未投递的动作指令撤回")
        acting = device_acting()
        batch.state = "paused"
        batch.held_at = now()
        batch.note = f"操作员请求保持{'：' + reason if reason else ''}"
        if not acting:
            # 设备侧没有在途动作：人工 / 等待 / 审核节点，或动作指令还在队列里就被撤回
            if run is not None and run.kind != DEVICE:
                detail = (
                    f"当前是{KIND_NAMES.get(run.kind, run.kind)}节点，无设备动作需要保持；"
                    f"定时到期事件仍会记录，但恢复前不推进设备动作"
                )
            else:
                detail = "设备尚未收到动作指令，无需设备侧保持"
            if withdrawn:
                detail += f"；撤回 {withdrawn} 条未投递指令"
            self.audit.record(
                user, "请求保持", batch.id, before="运行中", after="已保持", detail=detail,
            )
            self.db.commit()
            return {**self.summary_out(batch), "device_hold_command_id": "", "withdrawn": withdrawn}
        # 并行分支时「当前步骤」不一定是在设备上的那一步：保持发给真正在动作的指令所在步骤与工位
        target = acting[0]
        command = self.issue_command(
            batch, "hold", target.step_index if target is not None else batch.current_step,
            step_run_id=(target.step_run_id if target is not None else (run.id if run else "")),
            station_id=target.station_id if target is not None else None,
        )
        self.audit.record(
            user, "请求保持", batch.id, before="运行中", after="已保持", command_id=command.id,
            detail=(
                f"按能力保持规程「{recovery_rules.get('hold', '')}」执行；检查点已保留"
                + (f"；保持针对在途指令 {acting[0].id}" if acting[0] is not None else "")
            ),
        )
        self.db.commit()
        # 批次已不再推进新动作，但设备是否真的停住以保持指令的回执为准
        return {**self.summary_out(batch), "device_hold_command_id": command.id, "withdrawn": withdrawn}

    def _recovery_rules(self, batch: Batch) -> dict:
        steps = self.steps_of(batch)
        if batch.current_step >= len(steps):
            return {}
        return self.capabilities.recovery_of(steps[batch.current_step].get("cap", ""))

    # ---------- 恢复 ----------

    def recovery_context(self, batch: Batch) -> recovery.RecoveryContext:
        steps = self.steps_of(batch)
        index = min(batch.current_step, max(0, len(steps) - 1))
        step = steps[index] if steps else {}
        capability_id = step.get("cap", "")
        allocation = self.allocations.work_step(batch.id, index)
        held_at = batch.held_at or now()
        duration = float(step.get("dur", 0) or 0)
        elapsed = 0.0
        if allocation:
            elapsed = max(0.0, min(duration, (held_at - allocation.starts_at).total_seconds() / 60))
        next_allocation = self.allocations.work_step(batch.id, index + 1)
        next_station = self.stations.get(next_allocation.station_id) if next_allocation else None
        return recovery.RecoveryContext(
            capability_id=capability_id,
            capability_name=self.capabilities.names().get(capability_id, capability_id),
            recovery=self.capabilities.recovery_of(capability_id),
            step_name=step.get("name", ""),
            step_duration_min=duration,
            elapsed_min=elapsed,
            held_min=(now() - held_at).total_seconds() / 60,
            unresolved_alarm_ids=[a.id for a in self.alarms.unresolved_for_batch(batch.id)],
            downstream_steps=steps[index + 1:],
            next_station=({"id": next_station.id, "status": next_station.status} if next_station else None),
            lane_mate_batch_ids=(
                self.allocations.lane_mates(allocation.station_id, batch.id) if allocation else []
            ),
            unfinished_sample_count=self.samples.unfinished_count(batch.id),
        )

    def recovery_options(self, batch_id: str) -> dict:
        batch = self._require(batch_id)
        if batch.state not in RECOVERABLE:
            raise StateConflict("只有已保持或故障批次需要恢复评估")
        context = self.recovery_context(batch)
        preconditions = recovery.preconditions(context)
        options = [o.as_dict() for o in recovery.options(context, preconditions)]
        flow_complete = self._flow_complete(batch)
        if flow_complete:
            # 保持期间流程节点已全部走完：没有可续跑的动作，「续跑」即确认结束。
            # 结束前异常原因仍须消除——未处置的报警不能随批次一起「完成」
            cause_cleared = next(
                (row["ok"] for row in preconditions if row["key"] == "cause_cleared"), True
            )
            for option in options:
                if option["id"] == recovery.RESUME:
                    option.update(
                        allowed=cause_cleared,
                        reason="" if cause_cleared else "异常原因未消除",
                        impact="流程节点已全部完成，确认设备与样品状态后结束批次",
                    )
        unknown_commands = [
            {"id": c.id, "delivery_state": c.delivery_state, "error": c.error, "state": c.state,
             "type": c.type}
            for c in self.commands.for_batch(batch.id)
            if c.state in {"unknown", "manual"}
        ]
        partial = [c for c in self.commands.for_batch(batch.id) if c.state == "partial"]
        if partial:
            # 部分执行的物理状态不可复现：续跑与重试都会在未知的起点上继续，只能终止
            for option in options:
                if option["id"] in {recovery.RESUME, recovery.RETRY}:
                    option.update(allowed=False, reason="存在现场确认「部分执行」的指令，只能终止")
        steps = self.steps_of(batch)
        current = steps[batch.current_step] if batch.current_step < len(steps) else {}
        blockers = self._blind_blockers(batch)
        rerun_targets = [
            {"step_id": step_id_of(steps[at], at), "index": at, "name": steps[at].get("name")}
            for at in sorted(dag.ancestors(steps, batch.current_step) | {batch.current_step})
        ] if steps else []
        return {
            "batch_id": batch.id,
            "skip": {
                "step_id": step_id_of(current, batch.current_step) if current else "",
                "allowed": bool(current.get("skippable")) and not blockers,
                "reason": (
                    "" if current.get("skippable") and not blockers
                    else "方法没有把这一步标为可跳过" if not current.get("skippable")
                    else "存在没有结论的设备指令"
                ),
            },
            "rerun_targets": rerun_targets if not blockers else [],
            "hold_reason": batch.failure_reason or batch.note,
            "held_at": batch.held_at.isoformat(timespec="seconds") if batch.held_at else None,
            "step_index": batch.current_step,
            "step_name": context.step_name,
            "capability_name": context.capability_name,
            "hold_state": context.recovery.get("hold", ""),
            "verify": context.recovery.get("verify") or [],
            "preconditions": preconditions,
            "ready": all(row["ok"] for row in preconditions),
            "options": options,
            "flow_complete": flow_complete,
            "unknown_commands": unknown_commands,
            # 结果未知的命令必须先人工核查，界面上禁止一键盲目重试
            "blind_retry_allowed": not any(
                c["delivery_state"] == "maybe_sent" or c["state"] == "manual"
                for c in unknown_commands
            ),
            "gate": self.gate.status(),
        }

    def _flow_complete(self, batch: Batch) -> bool:
        if self.runs.current(batch.id) is not None:
            return False
        index, _ = self.workflow.next_open_step(self.steps_of(batch), batch, -1)
        return index is None and bool(self.runs.for_batch(batch.id))

    def recover(self, batch_id: str, strategy: str, verified: bool, signature_id: str, user: User) -> dict:
        if not self.ctx.has("batch.recover"):
            raise PermissionDenied("当前角色不能执行恢复")
        batch = self._require_locked(batch_id)
        if batch.state not in RECOVERABLE:
            raise StateConflict("只有已保持或故障批次可恢复")
        if not verified:
            raise StateConflict("必须勾选已核实实际量与设备状态")
        if strategy != recovery.ABORT:
            # 续跑与重试会重新驱动设备，要过执行门；安全终止不受执行门限制
            self.gate.require_open(self._station_ids(batch, from_step=batch.current_step))

        evaluation = self.recovery_options(batch_id)
        option = next((o for o in evaluation["options"] if o["id"] == strategy), None)
        if not option:
            raise StateConflict("未知的恢复策略")
        if not option["allowed"]:
            raise StateConflict(f"策略不可用：{option['reason']}")
        if strategy in {"resume", "retry"} and not evaluation["blind_retry_allowed"]:
            raise StateConflict(
                "存在结果未知且可能已送达的指令，必须先人工核查设备实态",
                {"blocked": [
                    {"key": "command", "label": f"{c['id']}：{c['error']}"}
                    for c in evaluation["unknown_commands"]
                ]},
                code="manual_check_required",
            )
        # 实际恢复时再校验一次资质：分配之后可能已经到期或被撤销
        steps = self.steps_of(batch)
        task = self.tasks.get(batch.task_id) if batch.task_id else None
        remaining_end = max(
            (a.ends_at for a in self.allocations.for_batch(batch.id) if a.step_index >= batch.current_step),
            default=None,
        )
        self.people.require_for_steps(
            (task.assignee_user_id if task else user.id) or user.id, steps, now(), action="恢复运行",
            until=(remaining_end + timedelta(minutes=held_min_estimate(batch))) if remaining_end else None,
        )
        if strategy != recovery.ABORT and batch.current_step < len(steps) and kind_of(steps[batch.current_step]) == DEVICE:
            from .transfer_service import TransferService

            work = self.allocations.work_step(batch.id, batch.current_step)
            TransferService(self.db, self.ctx).require_ready(
                batch, batch.current_step, work.station_id if work else "",
            )
        signature = self.identity.consume_signature(
            signature_id, user, f"恢复策略：{option['label']}",
            object_ref=batch.id, object_version=batch.row_version,
        )

        before = STATE_LABEL.get(batch.state, batch.state)
        held_min = (now() - (batch.held_at or now())).total_seconds() / 60
        if strategy == recovery.ABORT:
            return self._finish_abort(batch, user, signature, "恢复评估选择安全终止")

        step_duration = float(
            (steps[batch.current_step] if batch.current_step < len(steps) else {}).get("dur", 0) or 0
        )
        shift = held_min + (step_duration if strategy == recovery.RETRY else 0)
        from .schedule_service import lock_schedule

        lock_schedule(self.db)
        self.allocations.shift_from_step(batch.id, batch.current_step, shift)
        overlaps = [
            row for row in self.schedule.conflicts()
            if batch.id in {row["a"]["batch_id"], row["b"]["batch_id"]}
        ]
        batch.state = "running"
        batch.note = ""
        batch.failure_reason = ""
        batch.held_at = None
        self.batches.bump(batch)
        for alarm in self.alarms.for_source("batch", batch.id):
            if alarm.state == "active":
                alarm.state = "acked"
        run = self.runs.current(batch.id)
        pending_current = False
        if dag.graph_mode(steps):
            # 并行分支：恢复针对出问题的那一步（故障时记在 current_step），不是最靠前的开着的一步
            history = self.runs.for_batch(batch.id)
            done_ids = {r.step_id for r in history if r.state == "completed"}
            run = next((r for r in self.runs.open_runs(batch.id) if r.step_index == batch.current_step), None)
            if run is None and step_id_of(steps[batch.current_step], batch.current_step) in done_ids:
                run = self.runs.current(batch.id)
                if run is None:
                    ready = dag.ready_after(steps, done_ids, {
                        r.step_id for r in history if r.state not in {"completed", "superseded", "cancelled"}
                    })
                    if ready:
                        batch.current_step = ready[0]
            pending_current = run is None and (
                step_id_of(steps[batch.current_step], batch.current_step) not in done_ids
            )
        if run is None and strategy != recovery.RETRY and not pending_current:
            index, _ = self.workflow.next_open_step(steps, batch, -1)
            if index is None:
                # 保持期间最后一步已经走完：没有要恢复的动作，确认后直接结束
                self.workflow.finish_batch(
                    batch, user, detail="保持期间流程节点已全部完成，恢复评估确认后结束",
                )
                self.audit.record(
                    user, f"恢复：{option['label']}", batch.id, sign=True,
                    meaning=signature.meaning, before=before, after="已完成",
                    signature_id=signature.id, object_version=batch.row_version,
                    detail="无待恢复步骤，批次结束",
                )
                self.db.commit()
                return self.summary_out(batch)
            run = self.workflow.open_step(
                batch, index, assignee_user_id=(task.assignee_user_id if task else user.id),
            )
        elif run is None or strategy == recovery.RETRY:
            run = self.workflow.open_step(
                batch, batch.current_step,
                assignee_user_id=(task.assignee_user_id if task else user.id),
            )
        else:
            run.state = "ready" if run.kind != "wait" else "waiting"
            run.row_version = int(run.row_version or 0) + 1
        command = None
        if run.kind == DEVICE:
            # 这一步的动作指令从未送达设备（保持时还在队列里就被撤回）：续跑就是首次下发，
            # 不能给设备发一个它从没见过的动作的「继续」
            command_type = strategy
            if strategy == recovery.RESUME and not self.commands.ever_delivered_for_run(run.id):
                command_type = "dispatch"
            command = self.issue_command(batch, command_type, run.step_index, step_run_id=run.id)
        checkpoint = self.checkpoints.latest_for_step(batch.id, max(0, batch.current_step - 1))
        self.audit.record(
            user, f"恢复：{option['label']}", batch.id, sign=True, meaning=signature.meaning,
            before=before, after="运行中", signature_id=signature.id,
            command_id=command.id if command else "",
            checkpoint_id=checkpoint.id if checkpoint else "",
            object_version=batch.row_version,
            detail=(
                f"下游整体后移 {shift:.0f} min；{option['impact']}"
                + (
                    f"；后移后与 {len(overlaps)} 个其他占用重叠，需在排程页处理"
                    if overlaps else ""
                )
            ),
        )
        self._settle_exceptions(batch, f"恢复评估：{option['label']}", user)
        self.db.commit()
        return self.summary_out(batch)

    # ---------- 跳过与从指定节点重做 ----------

    def _settle_exceptions(self, batch: Batch, final: str, user: User | None) -> None:
        from .exception_service import ExceptionService

        ExceptionService(self.db, self.ctx).settle_batch(batch, final, user)

    def _step_index(self, batch: Batch, step_id: str) -> int:
        steps = self.steps_of(batch)
        ids = [step_id_of(step, index) for index, step in enumerate(steps)]
        if step_id not in ids:
            raise NotFound(f"批次快照里没有步骤 {step_id}")
        return ids.index(step_id)

    def _blind_blockers(self, batch: Batch) -> list[dict]:
        """结果未知、人工核查中、部分执行的指令：这些没有结论之前，不能在它们之上改流程。"""
        rows = []
        for command in self.commands.for_batch(batch.id):
            # 从未交给适配器就被拒的指令设备没见过，不算「结果未知」；交给过的一律要现场核查
            from .exception_service import never_left_system

            unsettled = command.state == "manual" or (command.state == "unknown" and not never_left_system(command))
            if unsettled:
                rows.append({"key": "command", "label": f"指令 {command.id[:8]} 结果未知，先到现场核查"})
            elif command.state == "partial":
                rows.append({"key": "command", "label": f"指令 {command.id[:8]} 部分执行，只能终止"})
        return rows

    def skip_step(self, batch_id: str, step_id: str, reason: str, signature_id: str, user: User) -> dict:
        """跳过一个步骤。只有方法里标了「可跳过」的步骤能跳，且要写理由并签名。

        - 运行中：步骤还没动（待开始 / 待办 / 等待中），设备步骤的指令还在队列里（撤回它）；
        - 保持或故障：步骤已明确失败（设备步骤要么明确失败、要么现场核查为「未执行」），
          另开一条「已跳过」记录，原失败记录保留；批次回到运行中继续往下走。
        设备已经收到指令、结果未知或部分执行时一律不能跳：物理动作可能已经发生，跳过等于假装没发生。
        """
        from .execution_service import ExecutionService

        if not self.ctx.has("batch.recover"):
            raise PermissionDenied("当前角色不能跳过步骤（需要异常恢复权限）")
        reason = (reason or "").strip()
        if not reason:
            raise ValidationFailed("跳过步骤必须写明理由")
        batch = self._require_locked(batch_id)
        if batch.state not in {"running", *RECOVERABLE}:
            raise StateConflict(f"批次处于「{STATE_LABEL.get(batch.state, batch.state)}」，不能跳过步骤")
        index = self._step_index(batch, step_id)
        steps = self.steps_of(batch)
        step = steps[index]
        if not step.get("skippable"):
            raise StateConflict(
                f"第 {index + 1} 步「{step.get('name')}」在方法里没有标为可跳过：关键步骤不能在运行时临时跳掉",
                code="step_not_skippable",
            )
        history = [row for row in self.runs.for_batch(batch.id) if row.step_id == step_id]
        live = next((row for row in reversed(history) if row.state not in {"superseded", "cancelled"}), None)
        if live is None:
            raise StateConflict("这一步还没开出：只能跳过已经开出或已失败的步骤，不能预先跳过", code="step_not_open")
        if live.state in {"completed", "skipped", "not_taken"}:
            raise StateConflict(f"这一步已是「{live.state}」，不需要跳过")
        from .exception_service import never_left_system

        own = [c for c in self.commands.for_batch(batch.id) if c.step_run_id == live.id]
        if live.state == "unknown" and any(
            not (never_left_system(c) or c.delivery_state in {"queued", "not_sent"}) for c in own
        ):
            raise StateConflict("这一步的设备动作结果未知：先到现场核查，再决定跳过或重试", code="manual_check_required")
        blockers = self._blind_blockers(batch)
        if blockers:
            raise StateConflict("存在没有结论的设备指令，不能跳过", {"blocked": blockers}, code="manual_check_required")
        execution = ExecutionService(self.db, self.ctx)
        for command in self.commands.for_batch(batch.id):
            if command.step_run_id != live.id:
                continue
            reached_device = command.delivery_state in {"maybe_sent", "delivered"}
            settled = command.state in {"cancelled", "not_executed"}
            if command.state in {"accepted", "running"} or (reached_device and not settled):
                raise StateConflict(
                    "设备已收到这一步的指令，不能跳过：等设备给出结论或保持后走恢复", code="command_delivered",
                )
        withdrawn = 0
        for command in own:
            if command.state == "sent" and command.delivery_state == "queued":
                withdrawn += int(execution.withdraw(command, f"第 {index + 1} 步被跳过，指令撤回"))
            elif command.state == "unknown" and never_left_system(command):
                # 没离开系统的指令：随步骤跳过结束，不再挂在「结果未知」清单里
                command.state = "not_executed"
                command.error = (command.error + "；" if command.error else "") + "未送达设备，随步骤跳过结束"
        if live.state == "unknown":
            live.state = "failed"
            live.ended_at = now()
            live.reason = (live.reason + "；" if live.reason else "") + "指令未送达设备"
        signature = self.identity.consume_signature(
            signature_id, user, f"跳过步骤：{step.get('name')}", object_ref=batch.id,
            object_version=batch.row_version,
        )
        before = STATE_LABEL.get(batch.state, batch.state)
        if live.state in {"pending", "ready", "waiting", "running"}:
            if live.state == "running" and live.kind == DEVICE:
                raise StateConflict("设备步骤正在执行，不能跳过", code="command_delivered")
            live.state = "skipped"
            live.ended_at = now()
            live.reason = f"人工跳过：{reason}"
            live.row_version = int(live.row_version or 0) + 1
            skipped = live
        else:
            skipped = self.workflow.open_step(batch, index)
            skipped.state = "skipped"
            skipped.ended_at = now()
            skipped.reason = f"人工跳过（原记录失败）：{reason}"
        skipped.submitted_by = user.id
        released = self.workflow.release_step_windows(batch, index)
        if batch.state in RECOVERABLE:
            batch.state = "running"
            batch.held_at = None
            batch.failure_reason = ""
            batch.note = ""
            for alarm in self.alarms.for_source("batch", batch.id):
                if alarm.state == "active":
                    alarm.state = "acked"
        self.batches.bump(batch)
        self.audit.record(
            user, "跳过步骤", batch.id, sign=True, meaning=signature.meaning, signature_id=signature.id,
            before=before, after="已跳过", object_version=batch.row_version,
            detail=(
                f"第 {index + 1} 步「{step.get('name')}」：{reason}；撤回 {withdrawn} 条未投递指令，"
                f"归还 {released} 个时间窗"
            ),
        )
        self.db.flush()
        self._settle_exceptions(batch, f"人工跳过第 {index + 1} 步：{reason}", user)
        outcome = self.workflow._advance(skipped, batch)
        self.db.commit()
        return {**self.summary_out(batch), "advance": outcome}

    def rerun_from(self, batch_id: str, step_id: str, reason: str, signature_id: str, user: User) -> dict:
        """从指定节点重做：这一步与它的全部下游作废（记录保留），从这一步重新开出。

        只在保持或故障时可用，且批次没有在途或结果未知的设备指令——重做建立在「现场状态已知」之上。
        会重新执行的设备步骤列在审计里：重做一次注液不是一条日志的代价，签名的人要看得见。
        """
        from .execution_service import ExecutionService

        if not self.ctx.has("batch.recover"):
            raise PermissionDenied("当前角色不能执行恢复")
        reason = (reason or "").strip()
        if not reason:
            raise ValidationFailed("从指定节点重做必须写明理由")
        batch = self._require_locked(batch_id)
        if batch.state not in RECOVERABLE:
            raise StateConflict("只有已保持或故障批次可以从指定节点重做")
        index = self._step_index(batch, step_id)
        steps = self.steps_of(batch)
        scope = {index} | dag.descendants(steps, index)
        blockers = self._blind_blockers(batch)
        in_flight = [c for c in self.commands.for_batch(batch.id) if c.state in {"accepted", "running"}]
        blockers += [{"key": "command", "label": f"指令 {c.id[:8]} 仍在设备上执行"} for c in in_flight]
        if blockers:
            raise StateConflict("存在没有结论的设备指令，不能重做", {"blocked": blockers}, code="manual_check_required")
        self.gate.require_open(self._station_ids(batch, from_step=index))
        signature = self.identity.consume_signature(
            signature_id, user, f"从第 {index + 1} 步重做", object_ref=batch.id, object_version=batch.row_version,
        )
        execution = ExecutionService(self.db, self.ctx)
        from .exception_service import never_left_system

        for command in self.commands.for_batch(batch.id):
            if command.step_index in scope and command.state == "sent" and command.delivery_state == "queued":
                execution.withdraw(command, f"从第 {index + 1} 步重做，未投递指令撤回")
            elif command.step_index in scope and command.state == "unknown" and never_left_system(command):
                command.state = "not_executed"
                command.error = (command.error + "；" if command.error else "") + "未送达设备，随从指定节点重做结束"
        voided: list[str] = []
        rerun_devices: list[str] = []
        for row in self.runs.for_batch(batch.id):
            if row.step_index not in scope:
                continue
            if row.state in {"pending", "ready", "running", "waiting"}:
                row.state = "cancelled"
            elif row.state in {"completed", "skipped", "not_taken", "failed", "unknown"}:
                # unknown 在这里只可能是指令从未送达设备（其余结果未知已被上面的核查挡住）
                if row.state == "completed" and row.kind == DEVICE:
                    rerun_devices.append(f"第 {row.step_index + 1} 步「{(row.step_snapshot or {}).get('name')}」")
                row.state = "superseded"
            else:
                continue
            row.reason = (row.reason + "；" if row.reason else "") + f"从第 {index + 1} 步重做，本次结论作废"
            row.row_version = int(row.row_version or 0) + 1
            voided.append(row.step_id)
        before = STATE_LABEL.get(batch.state, batch.state)
        batch.state = "running"
        batch.held_at = None
        batch.failure_reason = ""
        batch.note = ""
        batch.current_step = index
        for alarm in self.alarms.for_source("batch", batch.id):
            if alarm.state == "active":
                alarm.state = "acked"
        self.batches.bump(batch)
        task = self.tasks.get(batch.task_id) if batch.task_id else None
        run = self.workflow.open_step(batch, index, assignee_user_id=(task.assignee_user_id if task else user.id))
        outcome = self.workflow.enter(batch, run, index)
        self.audit.record(
            user, "从指定节点重做", batch.id, sign=True, meaning=signature.meaning, signature_id=signature.id,
            before=before, after="运行中", object_version=batch.row_version,
            command_id=outcome.get("command_id") or "",
            detail=(
                f"自第 {index + 1} 步「{steps[index].get('name')}」重做：{reason}；作废 {len(voided)} 条记录"
                + (f"；将重新执行的设备步骤：{'、'.join(dict.fromkeys(rerun_devices))}" if rerun_devices else "")
            ),
        )
        self._settle_exceptions(batch, f"从第 {index + 1} 步重做：{reason}", user)
        self.db.commit()
        return {**self.summary_out(batch), "advance": outcome}

    # ---------- 结果未知指令的人工核查 ----------

    VERIFY_CONCLUSIONS = {
        "executed": "现场确认已执行",
        "not_executed": "现场确认未执行",
        "partial": "现场确认部分执行",
    }

    def verify_command(
        self, command_id: str, conclusion: str, note: str, delivered: dict | None,
        signature_id: str, user: User,
    ) -> dict:
        """给结果未知的指令下现场结论。

        系统不替人猜设备到底动没动：只有到现场核实的人才能给结论，并签名负责。
        - 已执行：按人工核实写检查点，流程照常推进（保持 / 故障中的批次仍需恢复评估续跑）；
          终止指令已执行 = 现场已安全停机，批次直接终止（设备离线时终止只能走这条路）。
        - 未执行：该步骤可以安全重试或续跑。
        - 部分执行：物理状态不可复现，续跑与重试都被禁止，只能终止。
        """
        from ..models import AdapterExecution, Checkpoint
        from .execution_service import ExecutionService

        if not self.ctx.has("batch.recover"):
            raise PermissionDenied("当前角色不能核查指令结果")
        if conclusion not in self.VERIFY_CONCLUSIONS:
            raise ValidationFailed("核查结论只能是 executed、not_executed 或 partial")
        if not (note or "").strip():
            raise ValidationFailed("必须写明现场核查依据", code="verification_note_required")
        command = self.commands.get(command_id)
        if command is None:
            raise NotFound("指令不存在")
        batch = self._require_locked(command.batch_id)
        if command.state not in {"unknown", "manual"}:
            raise StateConflict(
                f"只有结果未知或人工核查中的指令需要核查（当前 {command.state}）",
                code="command_not_unknown",
            )
        if command.type == "hold" and conclusion == "partial":
            raise ValidationFailed("保持指令没有「部分执行」：请确认设备是否已停在保持状态")
        signature = self.identity.consume_signature(
            signature_id, user, f"指令核查：{self.VERIFY_CONCLUSIONS[conclusion]}",
            object_ref=command.id, strict=True,
        )
        execution = ExecutionService(self.db, self.ctx)
        record = execution.adapters.get(command.station_id)
        ledger = execution.executions.get(command.id)
        if ledger is None:
            ledger = AdapterExecution(command_id=command.id, station_id=command.station_id, state="unknown")
            self.db.add(ledger)
        before = command.state
        verdict = f"{self.VERIFY_CONCLUSIONS[conclusion]}：{note.strip()}（{user.display_name}）"
        run = execution._run_for(command, batch) if command.type in DISPATCHING else None
        outcome: dict = {"conclusion": conclusion}

        if command.type == "transfer":
            from .transfer_service import TransferService

            transfers = TransferService(self.db, self.ctx)
            execution._release(record, command)
            if conclusion == "executed":
                mismatch = transfers.complete(command, source="verification", by=user.display_name, note=verdict)
                if mismatch:
                    raise StateConflict(f"不能按「已执行」结论记录：{mismatch}", code="transfer_mismatch")
                ledger.state, command.state = "done", "done"
                command.delivery_state = "delivered"
            elif conclusion == "not_executed":
                ledger.state, command.state = "not_executed", "not_executed"
            else:
                # 搬到一半：板的位置不可信，扫码重新定位后才能继续；与设备动作的部分执行不同，
                # 转运部分执行没有改变样品的物理状态，所以不强制终止
                ledger.state, command.state = "partial", "not_executed"
                transfers.lost_by_command(command, f"转运部分执行：{note.strip()}", by=user.display_name)
            command.error = verdict
        elif conclusion == "executed":
            ledger.state = "done"
            command.delivery_state = "delivered"
            if command.type in DISPATCHING:
                from ..adapters.base import CommandResult

                execution.complete_device_step(
                    batch, command,
                    CommandResult(
                        command_id=command.id, state="done", device_ts=now(), quality="uncertain",
                        delivered=delivered or {}, origin="manual_verification",
                    ),
                )
                command.error = verdict
            else:
                command.state = "done"
                command.error = verdict
                if command.type == "abort":
                    # 设备离线时终止确认不了：现场确认已安全停机，由核查人签名结束批次
                    execution._confirm_abort(batch, command, record)
                    for other in self.commands.for_batch(batch.id):
                        if other.id != command.id and other.state in {"unknown", "manual"}:
                            other.state = "cancelled"
                            other.error = f"随终止人工确认结束：{verdict}"
            execution._release(record, command)
        elif conclusion == "not_executed":
            ledger.state = "not_executed"
            command.state = "not_executed"
            command.error = verdict
            execution._release(record, command)
            if run is not None and run.state == "unknown":
                run.state = "failed"
                run.ended_at = now()
                run.reason = verdict
                run.row_version = int(run.row_version or 0) + 1
            if command.type == "abort" and batch.state in {"fault", "aborting"}:
                # 终止没发到设备：批次回到可评估状态，由操作员重新决定终止或恢复
                batch.state = "fault"
                batch.failure_reason = f"终止指令未执行：{note.strip()}"
        else:
            ledger.state = "partial"
            command.state = "partial"
            command.error = verdict
            execution._release(record, command)
            if run is not None and run.state == "unknown":
                run.state = "failed"
                run.ended_at = now()
                run.reason = verdict
                run.row_version = int(run.row_version or 0) + 1
            batch.failure_reason = f"指令部分执行，只能终止：{note.strip()}"
        ledger.updated_at = now()
        command.updated_at = now()
        # 「结果未知」这个条件已由现场结论消除；报警本身的确认与关闭仍由人处理
        from .alarm_service import AlarmService

        alarms = AlarmService(self.db, self.ctx)
        for suffix in ("fault", "overdue"):
            alarms.resolve_condition(f"command:{command.id}:{suffix}", f"现场核查结论：{verdict}")
        self.audit.record(
            user, "指令人工核查", command.id, sign=True, meaning=signature.meaning,
            before=before, after=command.state, signature_id=signature.id, command_id=command.id,
            detail=f"{batch.id} 第 {command.step_index + 1} 步 {command.type}；{verdict}",
        )
        self.db.commit()
        outcome.update(command_state=command.state, batch_state=batch.state)
        return outcome

    # ---------- 终止 ----------

    def abort(self, batch_id: str, reason: str, signature_id: str, user: User) -> dict:
        if not self.ctx.has("batch.control"):
            raise PermissionDenied("当前角色不能终止批次")
        batch = self._require_locked(batch_id)
        if batch.state in {"done", "aborted"}:
            raise StateConflict("批次已结束")
        # 终止是安全动作，不受执行门限制：门关着时现场最需要能停下来
        signature = self.identity.consume_signature(
            signature_id, user, "终止批次", object_ref=batch.id, object_version=batch.row_version
        )
        return self._finish_abort(batch, user, signature, reason)

    def _finish_abort(self, batch: Batch, user: User, signature, reason: str) -> dict:
        before = STATE_LABEL.get(batch.state, batch.state)
        batch.failure_reason = reason or "人工终止"
        self.samples.mark_all(batch.id, "failed", only_if_not="done")
        cancelled = self.workflow.cancel_open_runs(batch, batch.failure_reason)
        # 终止只自动释放未领用未消耗部分；已领用的生成归还或处置待办
        material = self.materials.release_reservations(batch.id, user)
        pending = material.get("pending_return") or []
        self._settle_exceptions(batch, f"批次终止：{reason or '人工终止'}", user)

        if batch.state in UNDISPATCHED:
            batch.state = "aborted"
            self.allocations.delete_for_batch(batch.id)
            SampleService(self.db, self.ctx).release_slots(f"PL-{batch.id.replace('B-', '')}")
            self.audit.record(
                user, "终止批次", batch.id, sign=True, meaning=signature.meaning, before=before,
                after="已终止", signature_id=signature.id, object_version=batch.row_version,
                detail=(
                    f"{reason or '人工终止'}；未下发设备，工位时间窗与未领用预留已释放；"
                    f"取消 {cancelled} 个待办"
                    + (f"；{len(pending)} 项已领用物料待归还或处置" if pending else "")
                ),
            )
        else:
            withdrawn = self._withdraw_queued(batch, "批次终止，未投递的动作指令撤回")
            acting_now = self.commands.possibly_acting(batch.id, MOTION)
            if not acting_now:
                # 设备侧没有在途或结果未知的动作：没有物理动作要确认，直接终止
                batch.state = "aborted"
                self.audit.record(
                    user, "终止批次", batch.id, sign=True, meaning=signature.meaning,
                    before=before, after="已终止", signature_id=signature.id,
                    object_version=batch.row_version,
                    detail=(
                        f"{reason or '人工终止'}；设备侧没有在途动作，无需设备确认；"
                        f"撤回 {withdrawn} 条未投递指令，取消 {cancelled} 个待办"
                        + (f"；{len(pending)} 项已领用物料待归还或处置" if pending else "")
                    ),
                )
                self.db.commit()
                return {**self.summary_out(batch), "pending_material_return": pending}
            batch.state = "aborting"
            run = self.runs.current(batch.id)
            # 在动作的可能是转运（承运工位）或并行分支上的设备步骤：终止发给它所在的工位
            target = next((c for c in acting_now if c.type == "transfer"), None) or acting_now[0]
            command = self.issue_command(
                batch, "abort", target.step_index, step_run_id=target.step_run_id or (run.id if run else ""),
                station_id=target.station_id, capability=target.capability,
            )
            self.audit.record(
                user, "终止批次", batch.id, sign=True, meaning=signature.meaning, before=before,
                after="终止中", signature_id=signature.id, command_id=command.id,
                object_version=batch.row_version,
                detail=(
                    f"{reason or '人工终止'}；未完成样品待隔离处置，工位清理后释放；"
                    f"取消 {cancelled} 个待办"
                    + (f"；{len(pending)} 项已领用物料待归还或处置" if pending else "")
                ),
            )
        self.db.commit()
        return {**self.summary_out(batch), "pending_material_return": pending}

    # ---------- 交班摘要 ----------

    def handover(self) -> dict:
        active = self.batches.active()
        return {
            "generated_at": now().isoformat(timespec="seconds"),
            "gate": self.gate.status(),
            "batches": [self.summary_out(b) for b in active],
            "open_alarms": [
                {"id": a.id, "severity": a.severity, "message": a.message,
                 "source": f"{a.source_type}:{a.source_id}"}
                for a in self.alarms.open_alarms()
            ],
            "unknown_commands": self.commands.unknown_count(),
            "expiring_lots": self.materials.expiry_warnings(),
            "expiring_qualifications": self.people.expiring(),
            "manual_todos": self.workflow.review_todos(),
            "waste": [t for t in self.materials.list_waste() if t["over_threshold"]],
        }

    def due_windows(self) -> list[dict]:
        rows = []
        for batch in self.batches.by_state("running", "paused", "fault"):
            steps = self.steps_of(batch)
            index = batch.current_step + 1
            if index >= len(steps):
                continue
            hard = steps[index].get("hard") or {}
            if "maxGapMin" not in hard:
                continue
            checkpoint = self.checkpoints.latest_for_step(batch.id, batch.current_step)
            anchor = checkpoint.created_at if checkpoint else (batch.held_at or batch.created_at)
            deadline = anchor + timedelta(minutes=float(hard["maxGapMin"]))
            rows.append(
                {
                    "batch_id": batch.id,
                    "step_index": index,
                    "step_name": steps[index].get("name"),
                    "max_gap_min": hard["maxGapMin"],
                    "deadline": deadline.isoformat(timespec="minutes"),
                    "remaining_min": round((deadline - now()).total_seconds() / 60),
                }
            )
        return rows
