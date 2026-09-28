from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from ..core.clock import as_utc, now
from ..core.config import settings
from ..core.context import AccessContext
from ..core.db import serialize
from ..core.errors import NotFound, StateConflict
from ..domain.scheduling import (
    CLEAN,
    TRANSFER,
    WORK,
    Interval,
    PlannedAllocation,
    SchedulingContext,
    SchedulingError,
    makespan,
    peak_load,
    plan_steps,
    planned_finish,
)
from ..domain.steps import needs_station, normalize, resource_demand
from ..models import Allocation, Batch, User
from ..repositories.batches import AllocationRepository, BatchRepository
from ..repositories.resources import StationRepository
from .asset_service import AssetService
from .audit_service import AuditService
from .gate_service import GateService
from .material_service import MaterialService

HELD_STATES = {"paused", "fault"}
# 排程模式：optimize 先按交付期拖期、再按总跨度与加权完成时间搜索顺序；其余按规则直接给顺序
MODES = ("optimize", "priority", "deadline", "fifo")
RESCHEDULABLE = {"planned", "scheduled", "running", "paused", "fault"}
SCHEDULE_LOCK = "schedule:allocations"


def lock_schedule(db: Session) -> None:
    """工位时间线的写锁。

    排程先读全站占用再写新占用：两个请求同时排同一工位，会读到同一份时间线、各自写成功。
    所有改占用的写操作（排程、重排、优化写入、恢复后移）都先拿这把锁，并在锁内重读时间线。
    """
    serialize(db, SCHEDULE_LOCK)


class ScheduleService:
    """排程用例。领域算法在 domain.scheduling，这里只负责取数、写库与解释失败原因。"""

    def __init__(self, db: Session, ctx: AccessContext):
        self.db = db
        self.ctx = ctx
        self.batches = BatchRepository(db, ctx)
        self.allocations = AllocationRepository(db)
        self.stations = StationRepository(db, ctx)
        self.materials = MaterialService(db, ctx)
        self.assets = AssetService(db, ctx)
        self.audit = AuditService(db, ctx)
        self.gate = GateService(db)

    # ---------- 上下文 ----------

    def held_station_ids(self) -> set[str]:
        """保持 / 故障中的批次占着、释放时间未知的工位。

        不只是出问题的那一步：并行分支上设备侧仍被占着的每一台（在途、已保持、结果未知）都算。
        """
        from ..repositories.execution import MOTION, CommandRepository

        commands = CommandRepository(self.db, self.ctx)
        held = set()
        for batch in self.batches.by_state(*HELD_STATES):
            allocation = self.allocations.work_step(batch.id, batch.current_step)
            if allocation:
                held.add(allocation.station_id)
            for command in commands.possibly_acting(batch.id, MOTION):
                held.add(command.station_id)
                held.update(command.assist_station_ids or [])
        return held

    def _busy(self, exclude_batch_ids: set[str] | None) -> dict[str, list[Interval]]:
        """工位时间线：未结束批次的时间窗，加上待清洗设备的预计清洗占用。

        批次完成时它剩下的清洗时间窗会随收尾释放；设备在清洗确认前实际不会给别的批次用，
        排程也不能把它当成空闲。
        """
        busy = self.allocations.busy_timeline(exclude_batch_ids)
        moment = now()
        for station in self.stations.list():
            if station.clean or station.dirty_batch_id in (exclude_batch_ids or set()):
                continue
            busy.setdefault(station.id, []).append(
                Interval(moment, moment + timedelta(minutes=max(1, settings.clean_min))),
            )
        return busy

    def samples_of(self, batch: Batch) -> int:
        """这个批次在用的样本数：按样本计通道的工位上，它的每段时间窗占这么多份通道。"""
        from ..repositories.batches import SampleRepository

        return max(1, len(SampleRepository(self.db, self.ctx).active_for_batch(batch.id)))

    def carrier_roles(self, batch: Batch) -> set[str]:
        """批次绑定的载具角色：运行时同一块板一次只在一台设备上，排程也按这个排（空集合表示没绑定）。"""
        from .transfer_service import TransferService

        return TransferService(self.db, self.ctx).roles(batch.id)

    def context(
        self, exclude_batch_ids: set[str] | None = None, prefer: str | None = None, allow_unclean: bool = True
    ) -> SchedulingContext:
        return SchedulingContext(
            stations=self.stations.specs(),
            busy=self._busy(exclude_batch_ids),
            held_station_ids=self.held_station_ids(),
            # 失联 / 心跳超时的设备、处于维护或已退役资产上的工位不承接新排程
            unavailable_station_ids=self._unavailable_stations(),
            **self._asset_constraints(),
            **self._calibration_constraints(),
            transfer_station_ids=self.stations.transfer_station_ids(),
            transfer_min=settings.transfer_min,
            clean_min=settings.clean_min,
            prefer_station_id=prefer,
            allow_unclean=allow_unclean,
        )

    def _unavailable_stations(self) -> dict[str, str]:
        from ..models import Asset

        blocked = dict(self.gate.status().get("blocked_stations") or {})
        for station in self.stations.list():
            if not station.asset_id or station.id in blocked:
                continue
            asset = self.db.get(Asset, station.asset_id)
            if asset is not None and asset.state in {"maintenance", "retired"}:
                label = "处于维护状态" if asset.state == "maintenance" else "已退役"
                blocked[station.id] = f"{station.id} 所属资产 {asset.asset_no} {label}"
        return blocked

    def _calibration_constraints(self) -> dict:
        """每个工位上每种能力的校准：此刻就无效的原因，或有效期的到期时刻。

        维护 / 退役已在不可用工位里；搬运能力不做校准（承运工位没有校准档案要核）。
        """
        from ..domain.resources import Window, calibration_blockers, governing_calibration
        from ..models import Asset
        from .execution_service import TRANSPORT_CAPABILITY

        moment = now()
        window = Window(moment, moment + timedelta(minutes=1))
        invalid: dict[tuple[str, str], str] = {}
        expiry: dict[tuple[str, str], datetime] = {}
        specs: dict[str, object] = {}
        for station in self.stations.list():
            if not station.asset_id or station.retired:
                continue
            if station.asset_id not in specs:
                asset = self.db.get(Asset, station.asset_id)
                if asset is None:
                    continue
                specs[station.asset_id] = AssetService(self.db, self.ctx).spec_for(asset)
            spec = specs[station.asset_id]
            if spec.state in {"maintenance", "retired"}:
                continue
            for capability in (station.limits or {}):
                if capability == TRANSPORT_CAPABILITY:
                    continue
                problems = calibration_blockers(spec, capability, window)
                if problems:
                    invalid[(station.id, capability)] = f"{station.id}：{problems[0]}"
                    continue
                governing = governing_calibration(spec, capability, moment)
                if governing is not None and governing.expires_at is not None:
                    expiry[(station.id, capability)] = governing.expires_at
        return {"calibration_invalid": invalid, "calibration_expiry": expiry}

    def _asset_constraints(self) -> dict:
        """资产容量与预约。维护 / 校准占满整台资产；人工预约占一份；排程占用由时间窗本身表达。"""
        from ..models import Asset
        from ..repositories.resources import BookingRepository

        station_asset = {s.id: s.asset_id for s in self.stations.list() if s.asset_id}
        capacity: dict[str, int] = {}
        for asset_id in set(station_asset.values()):
            asset = self.db.get(Asset, asset_id)
            capacity[asset_id] = max(1, asset.capacity if asset else 1)
        bookings: dict[str, list[tuple[Interval, int]]] = {}
        for row in BookingRepository(self.db, self.ctx).live():
            if row.kind == "schedule" or row.asset_id not in capacity:
                continue
            units = capacity[row.asset_id] if row.kind in {"maintenance", "calibration"} else 1
            bookings.setdefault(row.asset_id, []).append((Interval(row.starts_at, row.ends_at), units))
        return {"station_asset": station_asset, "asset_capacity": capacity, "asset_bookings": bookings}

    # ---------- 任务依赖 ----------

    def _task_of(self, batch: Batch):
        from ..repositories.recipes import ExperimentTaskRepository

        return ExperimentTaskRepository(self.db, self.ctx).get(batch.task_id) if batch.task_id else None

    def batch_end(self, batch: Batch) -> datetime | None:
        """批次结束（或计划结束）的时刻。

        已完成看最后一步的实际结束。其余按步骤依赖往后推：已结束的步骤用实际结束，设备步骤用时间窗，
        不占工位的等待 / 人工 / 审核步骤从前驱结束起加上时长——设备做完之后的静置、培养、冷却
        同样是工艺时间，下游开工与交期都要等它们。
        """
        if batch.state == "done":
            from ..models import StepRun

            ended = [
                row.ended_at for row in self.db.query(StepRun).filter(StepRun.batch_id == batch.id).all()
                if row.ended_at
            ]
            return max(ended) if ended else now()
        ends = self.step_ends(batch)
        return max(ends.values()) if ends else None

    def step_ends(self, batch: Batch) -> dict[int, datetime]:
        """每一步的结束（实际或计划）：已结束的用实际结束，设备步骤用时间窗，等待用到期时刻，
        其余从前驱结束加上时长。重排时范围之外的前驱按这里的结束接手。"""
        from ..domain import graph
        from ..domain import workflow as flow
        from ..domain.steps import step_id_of
        from ..repositories.workflow import StepRunRepository

        steps = normalize(batch.recipe_snapshot.get("steps") or [])
        work = {a.step_index: a for a in self.allocations.for_batch(batch.id) if a.kind == WORK}
        latest = {}
        for run in StepRunRepository(self.db, self.ctx).for_batch(batch.id):
            if run.state not in flow.VOID_STATES:
                latest[run.step_id] = run
        origin = batch.planned_start_at
        if origin is None and work:
            first = min(work)
            origin = work[first].starts_at - timedelta(minutes=graph.lead_min(steps, first))
        before = graph.predecessors(steps)
        ends: dict[int, datetime] = {}
        for index, step in enumerate(steps):
            run = latest.get(step_id_of(step, index))
            if run is not None and run.ended_at and run.state in {flow.COMPLETED, flow.SKIPPED, flow.NOT_TAKEN}:
                ends[index] = run.ended_at
                continue
            if index in work:
                ends[index] = work[index].ends_at
                continue
            if run is not None and run.kind == "wait" and run.state in flow.OPEN_STATES and run.due_at:
                ends[index] = run.due_at
                continue
            parents = [ends[parent] for parent in before[index] if parent in ends]
            start = max(parents) if parents else (run.started_at if run is not None and run.started_at else origin)
            if start is None:
                continue
            ends[index] = start + timedelta(minutes=float(step.get("dur") or 0))
        return ends

    def frozen_steps(self, batch: Batch) -> set[int]:
        """已开出或已判定的步骤：有一条没作废的步骤实例（执行中、保持、结果未知、已完成、已跳过、
        未走此分支…）。手动、自动与滚动重排都不动它们的时间窗——同一条冻结规则。"""
        from ..domain import workflow as flow
        from ..repositories.workflow import StepRunRepository

        return {
            run.step_index for run in StepRunRepository(self.db, self.ctx).for_batch(batch.id)
            if run.state not in flow.VOID_STATES
        }

    def _known_tail(
        self, batch: Batch, steps: list[dict], keep: set[int], protected: list[Allocation],
    ) -> tuple[dict[int, datetime], dict[int, str | None], dict[str, tuple[datetime, str]]]:
        """重排范围之外的步骤（keep）：各自的结束、结束后载具所在的工位，以及每个载具角色最后一次
        设备动作结束的时刻与工位。结束取这一步当前这次尝试的实际结束或计划结束（`step_ends`）：
        回环重做时上一轮留下的检查点不代表这一轮已经做完。"""
        from ..domain import graph
        from ..domain.steps import labware_role

        ends = self.step_ends(batch)
        work = {a.step_index: a for a in protected if a.kind == WORK}
        before = graph.predecessors(steps)
        known_ends: dict[int, datetime] = {}
        known_where: dict[int, str | None] = {}
        for index in sorted(keep):
            end = ends.get(index)
            if end is None:
                continue
            known_ends[index] = end
            if index in work:
                known_where[index] = work[index].station_id
            else:
                parents = [parent for parent in before[index] if parent in known_ends]
                known_where[index] = (
                    known_where[max(parents, key=lambda parent: (known_ends[parent], parent))] if parents else None
                )
        plate_state: dict[str, tuple[datetime, str]] = {}
        for index, allocation in work.items():
            if index not in known_ends:
                continue
            role = labware_role(steps[index]) if index < len(steps) else ""
            if role not in plate_state or known_ends[index] > plate_state[role][0]:
                plate_state[role] = (known_ends[index], allocation.station_id)
        return known_ends, known_where, plate_state

    def dependency_floor(self, batch: Batch, skip: set[str] | None = None) -> tuple[datetime | None, list[str]]:
        """任务上游决定的最早开工时刻，以及还定不下来的上游（没排程、没建批次、已终止）。

        `skip` 是同一次多批次优化里一起排的上游批次：它们的结束由优化器按候选顺序算，不看库里的旧占用。
        """
        from .task_service import TaskService

        task = self._task_of(batch)
        if task is None:
            return None, []
        service = TaskService(self.db, self.ctx)
        # 自己声明的上游加上从父任务继承的上游
        if not service.effective_dependencies(task):
            return None, []
        floor: datetime | None = None
        missing: list[str] = []
        for upstream, upstream_batch in service.upstream_batches(task):
            if upstream_batch is None:
                missing.append(f"上游任务 {upstream.id} 还没有建批次")
                continue
            if upstream_batch.state == "aborted":
                missing.append(f"上游任务 {upstream.id} 的批次 {upstream_batch.id} 已终止")
                continue
            if upstream_batch.id in (skip or set()):
                continue
            end = self.batch_end(upstream_batch)
            if end is None:
                missing.append(f"上游任务 {upstream.id} 的批次 {upstream_batch.id} 还没排程，定不下本批次的开工时间")
                continue
            floor = end if floor is None or end > floor else floor
        return floor, missing

    def upstream_batch_ids(self, batch: Batch) -> list[str]:
        from .task_service import TaskService

        task = self._task_of(batch)
        if task is None:
            return []
        return [row.id for _, row in TaskService(self.db, self.ctx).upstream_batches(task) if row is not None]

    def dependency_conflicts(self, batch: Batch) -> list[str]:
        """本批次（的任务）被下游依赖，而下游已排的开工早于本批次现在的结束：依赖被打破。"""
        from ..repositories.recipes import ExperimentTaskRepository
        from .task_service import TaskService

        task = self._task_of(batch)
        if task is None:
            return []
        tasks = ExperimentTaskRepository(self.db, self.ctx)
        end = self.batch_end(batch)
        if end is None:
            return []
        found: list[str] = []
        service = TaskService(self.db, self.ctx)
        # 依赖可以挂在本任务上，也可以挂在它的任意一层父任务上
        roots = [task.id, *(row.id for row in service.ancestors(task))]
        for root in roots:
            for downstream in tasks.dependents(root):
                for leaf, leaf_batch in service.leaf_batches([downstream.id]):
                    if leaf_batch is None or leaf_batch.state not in {"scheduled", "planned"}:
                        continue
                    work = [a for a in self.allocations.for_batch(leaf_batch.id) if a.kind == WORK]
                    start = min((a.starts_at for a in work), default=None)
                    if start is not None and start < end:
                        found.append(
                            f"下游任务 {leaf.id} 的批次 {leaf_batch.id} 计划 {start:%m-%d %H:%M} 开工，"
                            f"早于上游 {batch.id} 现在的结束 {end:%m-%d %H:%M}"
                        )
        return found

    def _raise_dependency_alarm(self, batch: Batch, conflicts: list[str]) -> None:
        from .alarm_service import AlarmService

        if not conflicts:
            return
        AlarmService(self.db, self.ctx).raise_alarm(
            severity=2, source_type="batch", source_id=batch.id,
            message=("任务依赖被打破：" + "；".join(conflicts))[:500],
            response="在排程页重排下游批次（或推迟上游）；下游开跑检查会挡住上游没结束的下发。",
            owner="调度", origin="system", condition_key=f"batch:{batch.id}:dependency_conflict",
        )

    # ---------- 写 ----------

    def schedule(
        self, batch: Batch, start_from: datetime | None, prefer: str | None, user: User, *, allow_proposals: bool = True,
    ) -> list[Allocation]:
        self.gate.require_open()
        lock_schedule(self.db)
        start_from = as_utc(start_from)
        if batch.state not in {"planned", "scheduled"}:
            raise StateConflict("只有计划中或已排程批次可重新排程")
        steps = normalize(batch.recipe_snapshot.get("steps") or [])
        if not steps:
            raise StateConflict("流程快照没有步骤")
        bom = batch.recipe_snapshot.get("bom") or []
        # 合法空 BOM 的方法不做物料检查，否则纯人工流程永远排不上
        if bom and not self.materials.bom_satisfied(batch.id, bom):
            raise StateConflict("物料预留不完整，排程前必须先完成预留")
        demand = resource_demand(steps)
        begin = start_from or (now() + timedelta(minutes=5))
        floor, missing = self.dependency_floor(batch)
        if missing:
            raise StateConflict(
                "上游任务还定不下来，无法排程",
                {"blocked": [{"key": "dependency", "label": text} for text in missing]},
                code="dependency_unscheduled",
            )
        if floor is not None and floor > begin:
            # 完成—开始：上游计划（或实际）结束之前不开工；纯人工流程同样如此
            begin = floor
        # 计划开始记下来：不占工位的起点步骤与纯人工流程据此推算完成时间
        batch.planned_start_at = begin
        if demand["needs_station"] == 0:
            # 全是人工 / 等待 / 审核节点：没有工位要占，直接进入已排程
            self.allocations.delete_for_batch(batch.id)
            batch.state = "scheduled"
            batch.current_step = 0
            self.audit.record(
                user, "排程批次", batch.id, before="计划", after="已排程",
                detail=f"{demand['total']} 步全部不占工位，无需资源预约；{begin:%m-%d %H:%M} 起",
            )
            self.db.flush()
            self._book_people(batch, begin)
            return []

        ends: dict = {}
        try:
            planned = plan_steps(
                steps, begin, self.context({batch.id}, prefer), step_ends=ends,
                exclusive_carrier=self.carrier_roles(batch), samples=self.samples_of(batch),
            )
        except SchedulingError as error:
            raise StateConflict(error.message, {"step_index": error.step_index}) from error

        self.allocations.delete_for_batch(batch.id)
        asset_of_station = {
            station.id: station.asset_id for station in self.stations.list() if station.asset_id
        }
        rows = [
            Allocation(
                batch_id=batch.id, step_index=item.step_index, station_id=item.station_id,
                asset_id=asset_of_station.get(item.station_id, ""),
                starts_at=item.starts_at, ends_at=item.ends_at, kind=item.kind, units=item.units,
            )
            for item in planned
        ]
        self.db.add_all(rows)
        self.db.flush()
        self._refuse_overlaps(batch.id)
        batch.state = "scheduled"
        batch.current_step = 0
        work = [item for item in planned if item.kind == WORK]
        finish = planned_finish(planned, ends)
        self.audit.record(
            user, "排程批次", batch.id, before="计划", after="已排程",
            detail=(
                f"{len(work)}/{demand['needs_station']} 个需占用步骤已预约，"
                f"{work[0].starts_at:%m-%d %H:%M} 起，跨度 "
                f"{makespan(planned).total_seconds() / 60:.0f} min"
                + (f"，含不占工位步骤计划完成于 {finish:%m-%d %H:%M}" if finish is not None else "")
                + (f"；上游任务结束于 {floor:%m-%d %H:%M}，不早于它开工" if floor is not None else "")
            ),
        )
        self.db.flush()
        self._book_people(batch, begin)
        if allow_proposals:
            # 交期按全部工艺时间判断：设备做完之后的静置同样要算进来
            self._urgent_insert(batch, planned_finish(planned, ends))
        return rows

    def _book_people(self, batch: Batch, begin: datetime | None) -> None:
        """排程之后按新时间窗预占执行人（人工步骤）。"""
        from .staffing_service import StaffingService

        StaffingService(self.db, self.ctx).book_batch(batch, begin)

    def _urgent_insert(self, batch: Batch, planned_end: datetime | None) -> None:
        """紧急插单：最高优先级批次按现有时间线赶不上交付期时，生成一份让低优先级未下发批次让路的重排建议。"""
        task = self._task_of(batch)
        if batch.priority != 1 or planned_end is None or task is None or not task.due_at or planned_end <= task.due_at:
            return
        stations = {a.station_id for a in self.allocations.for_batch(batch.id) if a.kind == WORK}
        others = {
            row.batch_id for row in self.db.query(Allocation).join(Batch, Batch.id == Allocation.batch_id)
            .filter(
                Allocation.station_id.in_(list(stations) or [""]), Allocation.starts_at > now(),
                Batch.state == "scheduled", Batch.priority > 1, Batch.org_id == self.ctx.org_id,
            ).all()
        }
        if not others:
            return
        from .reschedule_service import RescheduleService

        RescheduleService(self.db, self.ctx).propose(
            trigger="priority_insert",
            reason=(
                f"{batch.id}（优先级 1）按现有时间线 {planned_end:%m-%d %H:%M} 完成，晚于交付期 "
                f"{task.due_at:%m-%d %H:%M}：建议让低优先级未下发批次让路"
            ),
            batch_ids=[batch.id, *sorted(others)],
        )

    # ---------- 读 ----------

    def _refuse_overlaps(self, batch_id: str) -> None:
        """写入前的最后一道守门：锁内求解本不该与别的批次重叠，重叠了就拒绝而不是照写。

        工位通道与资产容量是两道独立的约束：同一资产映射的全部工位的时间窗，加上维护 / 校准 /
        人工预约，同一时刻不能超过资产容量——与排程求解用同一个口径。
        """
        clashes = [
            row for row in self._overlaps()
            if batch_id in {row["a"]["batch_id"], row["b"]["batch_id"]}
            and row["a"]["batch_id"] != row["b"]["batch_id"]
        ]
        if clashes:
            raise StateConflict(
                "排程结果与其他批次的工位占用重叠，已拒绝写入",
                {"blocked": [
                    {"key": "overlap", "label": (
                        f"{row['station_id']}：{row['a']['batch_id']} 与 {row['b']['batch_id']} "
                        f"重叠 {row['overlap_min']} min"
                    )}
                    for row in clashes
                ]},
                code="allocation_overlap",
            )
        overloads = self._asset_overloads(batch_id)
        if overloads:
            raise StateConflict(
                "排程结果超出共享资产容量，已拒绝写入",
                {"blocked": [{"key": "asset_capacity", "label": text} for text in overloads]},
                code="asset_capacity_exceeded",
            )

    def _own_clean_overlaps(self, batch_id: str) -> list[Allocation]:
        """本批次的清洗窗口被本批次另一步的工作窗口压着（同一工位）：后一步接手了未清洗的工位。"""
        rows = self.allocations.for_batch(batch_id)
        work = [row for row in rows if row.kind in {WORK, "assist"}]
        return [
            clean for clean in rows if clean.kind == CLEAN and any(
                row.station_id == clean.station_id and row.step_index != clean.step_index
                and row.starts_at < clean.ends_at and clean.starts_at < row.ends_at
                for row in work
            )
        ]

    def _asset_overloads(
        self, batch_id: str, planned: list[PlannedAllocation] | None = None, ignore_ids: set | frozenset = frozenset(),
    ) -> list[str]:
        """本批次的时间窗落在哪些资产的超容量时段里。资产是跨组织共享的物理对象，按全站占用算。

        `planned` 给了就按这份还没写入的计划算（预览用）：本批次库里的旧时间窗不计。预览与写入前的
        最后一道检查用同一个口径，不会出现「预览可行、应用失败」。`ignore_ids` 是不计入的时间窗
        （按实际进度对齐时，被本批次下一步接手、随之撤销的清洗窗口）。
        """
        from ..models import Asset, ResourceBooking, Station

        station_asset = {sid: aid for sid, aid in self.db.query(Station.id, Station.asset_id).all() if aid}
        rows = self.allocations.for_batch(batch_id) if planned is None else planned
        mine = [
            row for row in rows if row.station_id in station_asset and getattr(row, "id", None) not in ignore_ids
        ]
        found: list[str] = []
        for asset_id in sorted({station_asset[row.station_id] for row in mine}):
            asset = self.db.get(Asset, asset_id)
            capacity = max(1, asset.capacity if asset else 1)
            stations = [sid for sid, aid in station_asset.items() if aid == asset_id]
            existing = self.db.query(Allocation).join(Batch, Batch.id == Allocation.batch_id).filter(
                Allocation.station_id.in_(stations), Batch.state.notin_(["done", "aborted"]),
            )
            if planned is not None:
                existing = existing.filter(Allocation.batch_id != batch_id)
            occupied = [
                (Interval(row.starts_at, row.ends_at), max(1, int(row.units or 1)))
                for row in existing.all() if row.id not in ignore_ids
            ]
            if planned is not None:
                occupied += [
                    (Interval(row.starts_at, row.ends_at), max(1, int(row.units or 1)))
                    for row in mine if station_asset[row.station_id] == asset_id
                ]
            for booking in self.db.query(ResourceBooking).filter(
                ResourceBooking.asset_id == asset_id, ResourceBooking.state.in_(["pending", "confirmed"]),
                ResourceBooking.kind != "schedule",
            ).all():
                units = capacity if booking.kind in {"maintenance", "calibration"} else 1
                occupied.append((Interval(booking.starts_at, booking.ends_at), units))
            for row in mine:
                if station_asset[row.station_id] != asset_id:
                    continue
                if peak_load(occupied, Interval(row.starts_at, row.ends_at)) > capacity:
                    found.append(
                        f"{row.station_id} 所属资产 {asset.asset_no if asset else asset_id} 在 "
                        f"{row.starts_at:%m-%d %H:%M} 起的时段超出容量 {capacity}"
                    )
                    break
        return found

    def dry_run(self, batch: Batch, start_from: datetime | None = None) -> dict:
        steps = normalize(batch.recipe_snapshot.get("steps") or [])
        begin = as_utc(start_from) or (now() + timedelta(minutes=5))
        ends: dict = {}
        try:
            planned = plan_steps(
                steps, begin, self.context({batch.id}), step_ends=ends,
                exclusive_carrier=self.carrier_roles(batch), samples=self.samples_of(batch),
            )
        except SchedulingError as error:
            return {"ok": False, "reason": error.message, "step_index": error.step_index, "path": []}
        overloads = self._asset_overloads(batch.id, planned)
        if overloads:
            return {
                "ok": False, "reason": f"排程结果超出共享资产容量：{overloads[0]}", "step_index": None,
                "path": [self._planned_out(item, steps) for item in planned],
            }
        finish = planned_finish(planned, ends)
        return {
            "ok": True,
            "reason": "",
            "step_index": None,
            "makespan_min": round(makespan(planned).total_seconds() / 60),
            "finish_at": finish.isoformat(timespec="minutes") if finish else None,
            "path": [self._planned_out(item, steps) for item in planned],
        }

    @staticmethod
    def _planned_out(item: PlannedAllocation, steps: list[dict]) -> dict:
        return {
            "step_index": item.step_index,
            "step_name": (steps[item.step_index] or {}).get("name") if item.step_index < len(steps) else "",
            "station_id": item.station_id,
            "kind": item.kind,
            "units": item.units,
            "starts_at": item.starts_at.isoformat(timespec="minutes"),
            "ends_at": item.ends_at.isoformat(timespec="minutes"),
        }

    def queue(self) -> list[dict]:
        """待排程队列。阻塞原因优先给物料，其次给排程可行性。"""
        rows = []
        for batch in self.batches.by_state("planned"):
            bom = batch.recipe_snapshot.get("bom") or []
            material_ok = (not bom) or self.materials.bom_satisfied(batch.id, bom)
            preview = self.dry_run(batch) if material_ok else {"ok": False, "reason": "物料预留不完整", "path": []}
            rows.append(
                {
                    "batch_id": batch.id,
                    "recipe": batch.recipe_snapshot.get("name"),
                    "version": batch.recipe_snapshot.get("version"),
                    "priority": batch.priority,
                    "due_at": (
                        task.due_at.isoformat(timespec="minutes")
                        if (task := self._task_of(batch)) is not None and task.due_at else None
                    ),
                    "plan_id": batch.plan_id,
                    "material_ok": material_ok,
                    "schedulable": preview["ok"],
                    "blocker": preview["reason"],
                    "makespan_min": preview.get("makespan_min"),
                    "path": preview["path"],
                }
            )
        return sorted(rows, key=lambda r: (r["priority"], r["batch_id"]))

    def forecast_marks(self, batch: Batch) -> dict[int, str]:
        """哪些步骤的时间窗只是预测：它们在一个还没定出口的条件分支下游（走不走这条路还不知道）。"""
        from ..domain import graph
        from ..domain.steps import BRANCH, kind_of, step_id_of
        from ..repositories.workflow import StepRunRepository

        steps = normalize(batch.recipe_snapshot.get("steps") or [])
        if not graph.graph_mode(steps):
            return {}
        latest = {}
        for run in StepRunRepository(self.db, self.ctx).for_batch(batch.id):
            if run.state not in {"superseded", "cancelled"}:
                latest[run.step_id] = run
        marks: dict[int, str] = {}
        for index, step in enumerate(steps):
            if kind_of(step) != BRANCH:
                continue
            run = latest.get(step_id_of(step, index))
            if run is not None and run.state == "completed":
                continue
            for later in sorted(graph.descendants(steps, index)):
                marks.setdefault(later, f"取决于第 {index + 1} 步「{step.get('name')}」的分支结果")
        return marks

    @staticmethod
    def forecast_of(allocation: Allocation, marks: dict[int, str], moment: datetime) -> str:
        """这段时间窗是承诺还是预测：分支没定的下游、冻结期之后的远期都只是预测。空串表示承诺。"""
        if allocation.step_index in marks:
            return marks[allocation.step_index]
        if allocation.starts_at > moment + timedelta(minutes=settings.schedule_freeze_min):
            return f"远期预测（{settings.schedule_freeze_min / 60:g} 小时之后），按实际进度滚动更新"
        return ""

    def roll_forward(self, batch: Batch, reason: str) -> dict:
        """分支定了路径或发生回环后，按实际路径重算还没开出的尾段（短期冻结、远期滚动）。

        只移动本批次自己未来的时间窗、只排进别人没占的空档，不挤占任何人的预约——与「按实际进度
        提前」同一个原则；排不下就不写，报警交给调度。已开出或已判定的步骤（执行中、保持、结果未知、
        已完成、已跳过、未走此分支）一概不动；重算的是其余全部还没开出的步骤，不是「最大序号之后」的
        后缀——没走的分支、先开出的并行分支可能排在列表后面，按序号截断会漏掉已选路径上的步骤。
        """
        if batch.state not in {"running", "paused", "fault"}:
            return {"rolled": False}
        steps = normalize(batch.recipe_snapshot.get("steps") or [])
        frozen = self.frozen_steps(batch)
        replan = [index for index in range(len(steps)) if index not in frozen]
        if not any(needs_station(steps[index]) for index in replan):
            return {"rolled": False}
        from_step = replan[0]
        lock_schedule(self.db)
        protected = [a for a in self.allocations.for_batch(batch.id) if a.step_index not in replan]
        known_ends, known_where, plate_state = self._known_tail(batch, steps, frozen, protected)
        context = self.context({batch.id})
        for allocation in protected:
            context.busy.setdefault(allocation.station_id, []).append(
                Interval(allocation.starts_at, allocation.ends_at, max(1, int(allocation.units or 1)))
            )
        try:
            planned = plan_steps(
                steps, now(), context, first_index=from_step, frozen=frozen, known_ends=known_ends,
                known_where=known_where, plate_state=plate_state, exclusive_carrier=self.carrier_roles(batch),
                samples=self.samples_of(batch),
            )
        except SchedulingError as error:
            self._roll_alarm(batch, f"{reason}后尾段重排不成立：{error.message}")
            return {"rolled": False, "reason": error.message}
        asset_of_station = {station.id: station.asset_id for station in self.stations.list() if station.asset_id}
        savepoint = self.db.begin_nested()
        try:
            self.allocations.delete_steps(batch.id, replan)
            self.db.add_all([
                Allocation(
                    batch_id=batch.id, step_index=item.step_index, station_id=item.station_id,
                    asset_id=asset_of_station.get(item.station_id, ""),
                    starts_at=item.starts_at, ends_at=item.ends_at, kind=item.kind, units=item.units,
                )
                for item in planned
            ])
            self.db.flush()
            self._refuse_overlaps(batch.id)
        except StateConflict as error:
            savepoint.rollback()
            self._roll_alarm(batch, f"{reason}后尾段重排与其他占用冲突：{error.message}")
            return {"rolled": False, "reason": error.message}
        savepoint.commit()
        work = [item for item in planned if item.kind == WORK]
        self.audit.record(
            None, "滚动重排", batch.id, before=f"自第 {from_step + 1} 步",
            after=(f"{min(item.starts_at for item in work):%m-%d %H:%M} 起" if work else "无设备步骤"),
            detail=f"{reason}：按实际路径重算尚未开出的 {len(work)} 个设备步骤，只用空档、不挤占其他批次",
        )
        return {"rolled": True, "from_step": from_step, "steps": len(work)}

    def _roll_alarm(self, batch: Batch, message: str) -> None:
        from .alarm_service import AlarmService

        AlarmService(self.db, self.ctx).raise_alarm(
            severity=3, source_type="batch", source_id=batch.id, message=message[:500],
            response="在排程页重排该批次尚未开出的步骤，或请求重排建议", owner="调度", origin="system",
            condition_key=f"batch:{batch.id}:roll_forward",
        )

    def board(self) -> dict:
        """步骤级资源泳道。保持中工位标注释放时间未知，其后安排仅为预测；分支未定的下游、冻结期之后也只是预测。"""
        held = self.held_station_ids()
        batches = {b.id: b for b in self.batches.active()}
        marks = {batch_id: self.forecast_marks(batch) for batch_id, batch in batches.items()}
        moment = now()
        lanes: dict[str, list[dict]] = {station.id: [] for station in self.stations.list()}
        for allocation in (
            self.db.query(Allocation).filter(Allocation.batch_id.in_(list(batches) or [""])).all()
        ):
            batch = batches[allocation.batch_id]
            steps = normalize(batch.recipe_snapshot.get("steps") or [])
            step = steps[allocation.step_index] if allocation.step_index < len(steps) else {}
            forecast = self.forecast_of(allocation, marks[batch.id], moment)
            lanes.setdefault(allocation.station_id, []).append(
                {
                    "batch_id": allocation.batch_id,
                    "batch_state": batch.state,
                    "step_index": allocation.step_index,
                    "step_name": step.get("name"),
                    "step_kind": step.get("kind", "device"),
                    "kind": allocation.kind,
                    "units": max(1, int(allocation.units or 1)),
                    "hard": step.get("hard"),
                    "starts_at": allocation.starts_at.isoformat(timespec="minutes"),
                    "ends_at": allocation.ends_at.isoformat(timespec="minutes"),
                    "uncertain": allocation.station_id in held,
                    "forecast": bool(forecast),
                    "forecast_reason": forecast,
                }
            )
        return {
            "now": now().isoformat(timespec="minutes"),
            "stations": [
                {
                    "id": station.id, "name": station.name, "island": station.island, "status": station.status,
                    "channels": station.channels or 1, "channel_unit": station.channel_unit or "batch",
                    "held": station.id in held, "items": sorted(lanes.get(station.id, []), key=lambda i: i["starts_at"]),
                }
                for station in self.stations.list()
            ],
            "conflicts": self.conflicts(),
        }

    def conflicts(self) -> list[dict]:
        """同工位时间窗重叠检测。只报告本组织批次，别的组织的批次号不能出现在看板上。"""
        visible = []
        for row in self._overlaps():
            if row["a"]["org_id"] != self.ctx.org_id or row["b"]["org_id"] != self.ctx.org_id:
                continue
            visible.append({
                **row,
                "a": {k: v for k, v in row["a"].items() if k != "org_id"},
                "b": {k: v for k, v in row["b"].items() if k != "org_id"},
            })
        return visible

    def _overlaps(self) -> list[dict]:
        """全站同工位时间窗超出并行通道数的重叠。工位是跨组织共享的物理资源，守门时必须看全站。

        按开始时刻扫描：新区间开始时仍未结束的区间数已达工位通道数，就与这些区间逐一报重叠。
        单通道工位等价于「任意两段重叠即冲突」；多通道工位（充放电柜）允许重叠到通道数，
        与领域排程 `_station_free` 用同一口径，否则排程算出来的合法结果会在写入前被拒绝。按样本计通道的工位
        上一段时间窗占它批次的样本数那么多份。
        """
        from ..models import Station

        channels = {
            station_id: max(1, int(count or 1))
            for station_id, count in self.db.query(Station.id, Station.channels).all()
        }
        rows: dict[str, list[tuple[Allocation, str]]] = {}
        for allocation, org_id in (
            self.db.query(Allocation, Batch.org_id).join(Batch, Batch.id == Allocation.batch_id)
            .filter(Batch.state.notin_(["done", "aborted"])).all()
        ):
            rows.setdefault(allocation.station_id, []).append((allocation, org_id))
        found = []
        for station_id, pairs in rows.items():
            capacity = channels.get(station_id, 1)
            pairs.sort(key=lambda pair: (pair[0].starts_at, pair[0].ends_at))
            active: list[tuple[Allocation, str]] = []
            for second, second_org in pairs:
                active = [pair for pair in active if pair[0].ends_at > second.starts_at]
                # 份数：按样本计通道的工位上一段时间窗占它批次的样本数，其余 1 份
                if sum(max(1, int(pair[0].units or 1)) for pair in active) + max(1, int(second.units or 1)) > capacity:
                    for first, first_org in active:
                        found.append(
                            {
                                "station_id": station_id,
                                "channels": capacity,
                                "a": {"batch_id": first.batch_id, "step_index": first.step_index,
                                      "kind": first.kind, "org_id": first_org},
                                "b": {"batch_id": second.batch_id, "step_index": second.step_index,
                                      "kind": second.kind, "org_id": second_org},
                                "overlap_min": round(
                                    (min(first.ends_at, second.ends_at) - second.starts_at).total_seconds() / 60
                                ),
                            }
                        )
                active.append((second, second_org))
        return found

    def optimize_preview(self, batch_ids: list[str], start_from: datetime | None = None, mode: str | None = None) -> dict:
        """多批次优化预览：搜索批次投产顺序，每个候选都由同一个排程器解码并校验约束。

        批次不多时穷举；多时迭代局部搜索（`domain/optimizer.py`）。装了 OR-Tools 时再用 CP-SAT
        （`domain/cpsat.py`）给一个候选顺序一起比较。与基线（按优先级排）对比后由操作员确认写入。
        """
        from ..domain import cpsat, optimizer
        from ..domain.graph import critical_path_min

        batches = [self._require(bid) for bid in dict.fromkeys(batch_ids)]
        if not batches:
            raise StateConflict("未选择批次")
        if len(batches) > settings.scheduler_max_batches:
            raise StateConflict(f"一次最多优化 {settings.scheduler_max_batches} 个批次")
        begin = as_utc(start_from) or (now() + timedelta(minutes=5))
        selected = {b.id for b in batches}
        by_id = {b.id: b for b in batches}
        steps_by_batch = {b.id: normalize(b.recipe_snapshot.get("steps") or []) for b in batches}
        weight = {b.id: max(1, 4 - int(b.priority or 2)) for b in batches}
        mode = (mode or settings.scheduler_mode or "optimize").strip().lower()
        if mode not in MODES:
            raise StateConflict(f"排程模式只能是 {'、'.join(MODES)}", code="schedule_mode_invalid")
        due = {b.id: (task.due_at if (task := self._task_of(b)) is not None else None) for b in batches}
        # 任务依赖：所选批次之间的先后必须保持；所选之外的上游给出固定的最早开工时刻
        from ..domain import tasks as task_rules

        upstream = {b.id: [ref for ref in self.upstream_batch_ids(b) if ref in selected] for b in batches}
        external_floor: dict[str, datetime] = {}
        for b in batches:
            outside = [ref for ref in self.upstream_batch_ids(b) if ref not in selected]
            if not outside:
                continue
            floor, missing = self.dependency_floor(b, skip=selected)
            if missing:
                raise StateConflict(
                    f"{b.id} 的上游任务还定不下来", {"blocked": [{"key": "dependency", "label": t} for t in missing]},
                    code="dependency_unscheduled",
                )
            if floor is not None:
                external_floor[b.id] = floor

        exclusive = {b.id: self.carrier_roles(b) for b in batches}
        samples = {b.id: self.samples_of(b) for b in batches}

        def evaluate(order: tuple[str, ...]) -> optimizer.Candidate:
            """候选顺序共享一份工位时间线，先排的批次会占住资源，后排的只能往后挪。

            完成时间按全部工艺时间算（含设备之后的静置等不占工位的步骤），拖期与下游起点都以它为准。
            """
            if not task_rules.respects(order, upstream):
                return optimizer.Candidate(order, False, reason="违反任务依赖：下游批次排在了上游之前")
            context = self.context(selected, allow_unclean=True)
            plans: dict[str, list[PlannedAllocation]] = {}
            completion: dict[str, datetime] = {}
            for batch_id in order:
                start_at = max([begin, *([external_floor[batch_id]] if batch_id in external_floor else []),
                                *[completion[ref] for ref in upstream[batch_id] if ref in completion]])
                ends: dict = {}
                try:
                    plans[batch_id] = plan_steps(
                        steps_by_batch[batch_id], start_at, context, step_ends=ends,
                        exclusive_carrier=exclusive[batch_id], samples=samples[batch_id],
                    )
                except SchedulingError as error:
                    return optimizer.Candidate(order, False, reason=f"{batch_id}: {error.message}")
                completion[batch_id] = planned_finish(plans[batch_id], ends) or start_at
            work_items = [a for planned in plans.values() for a in planned if a.kind == WORK]
            if not work_items:
                return optimizer.Candidate(order, True, 0, 0, "所选批次都不占工位，无需优化顺序", {"plans": {}})
            finish = max(completion.values())
            weighted = sum(weight[b] * (completion[b] - begin).total_seconds() / 60 for b in order)
            lateness = {
                b: round(max(0.0, (completion[b] - due[b]).total_seconds() / 60)) if due[b] else 0 for b in order
            }
            tardiness = sum(weight[b] * lateness[b] for b in order)
            return optimizer.Candidate(
                order, True, round((finish - begin).total_seconds() / 60), round(weighted), "",
                {"finish_at": finish.isoformat(timespec="minutes"), "lateness": lateness, "plans": {
                    batch_id: [self._planned_out(a, steps_by_batch[batch_id]) for a in planned]
                    for batch_id, planned in plans.items()
                }},
                tardiness_min=round(tardiness),
            )

        def out(candidate: optimizer.Candidate) -> dict:
            return {
                "ok": candidate.ok, "reason": candidate.reason, "order": list(candidate.order),
                "finish_at": candidate.payload.get("finish_at", begin.isoformat(timespec="minutes")),
                "span_min": candidate.span_min if candidate.ok else None,
                "weighted_min": candidate.weighted_min if candidate.ok else None,
                "tardiness_min": candidate.tardiness_min if candidate.ok else None,
                "lateness": candidate.payload.get("lateness", {}),
                "plans": candidate.payload.get("plans", {}),
            }

        ids = [b.id for b in batches]
        valid = lambda order: tuple(task_rules.topo_order(list(order), upstream))  # noqa: E731
        priority_order = valid(sorted(ids, key=lambda b: (by_id[b].priority, b)))
        longest_first = valid(sorted(ids, key=lambda b: (-critical_path_min(steps_by_batch[b]), b)))
        seeds = [priority_order, longest_first, valid(reversed(longest_first))]

        solver_info = None
        if settings.scheduler_backend in {"auto", "cpsat"} and cpsat.available() and len(ids) > 1:
            solver_info = self._cpsat_order(
                batches, steps_by_batch, weight, selected, begin, upstream=upstream, floors=external_floor,
            )
            if solver_info.get("order"):
                seeds.insert(0, valid(solver_info["order"]))
        elif settings.scheduler_backend == "cpsat":
            solver_info = {"status": "unavailable", "reason": "未安装 ortools，已用内置顺序搜索"}

        baseline = evaluate(priority_order)
        if mode == "optimize":
            report = optimizer.search(
                ids, evaluate, seeds=seeds, budget_sec=settings.scheduler_search_budget_sec,
            )
        else:
            # 规则模式：顺序由规则直接给出（仍保持任务依赖），不搜索
            rule = {
                "fifo": lambda b: (by_id[b].created_at, b),
                "priority": lambda b: (by_id[b].priority, by_id[b].created_at, b),
                "deadline": lambda b: (due[b] or datetime.max, by_id[b].priority, b),
            }[mode]
            chosen = evaluate(valid(sorted(ids, key=rule)))
            report = optimizer.SearchReport(chosen, 1, mode, 0)
        if not report.best.ok:
            raise StateConflict("所有候选顺序都无法满足约束", {"reason": report.best.reason or baseline.reason})
        return {
            # 应用时带回这个起点：预览与写入用同一个「现在」，分钟翻页不会让时间窗错开
            "start_from": begin.isoformat(timespec="seconds"),
            "mode": mode,
            "due": {b: due[b].isoformat(timespec="minutes") if due[b] else None for b in ids},
            "baseline": out(baseline),
            "best": out(report.best),
            "improvement_min": (baseline.span_min - report.best.span_min) if baseline.ok else None,
            "evaluated": report.evaluated,
            "method": report.method,
            "elapsed_ms": report.elapsed_ms,
            "solver": solver_info,
        }

    def _cpsat_order(self, batches, steps_by_batch, weight, selected, begin, *, upstream=None, floors=None) -> dict:
        """把批次、工位与已有占用翻译成 CP-SAT 模型求一个候选顺序。求不出来不影响内置搜索。"""
        from ..domain import cpsat
        from ..domain.graph import predecessors
        from ..domain.scheduling import candidate_station_ids

        context = self.context(selected, allow_unclean=True)
        jobs = []
        try:
            for batch in batches:
                steps = steps_by_batch[batch.id]
                before = predecessors(steps)
                specs = []
                samples = self.samples_of(batch)
                for index, step in enumerate(steps):
                    stations = tuple(candidate_station_ids(context, step, index, samples)) if needs_station(step) else ()
                    gap = (step.get("hard") or {}).get("maxGapMin")
                    specs.append(cpsat.StepSpec(
                        index=index, duration=max(0, round(float(step.get("dur") or 0))), stations=stations,
                        preds=tuple(before[index]), max_gap=round(float(gap)) if gap else None,
                    ))
                jobs.append(cpsat.JobSpec(batch.id, tuple(specs), weight[batch.id], samples))
        except SchedulingError as error:
            return {"status": "skipped", "reason": error.message}
        busy = {
            station_id: [
                (max(0, round((i.start - begin).total_seconds() / 60)), max(0, round((i.end - begin).total_seconds() / 60)),
                 i.units)
                for i in intervals if i.end > begin
            ]
            for station_id, intervals in context.busy.items()
        }
        channels = {spec.id: max(1, int(spec.channels or 1)) for spec in context.stations}
        solution = cpsat.solve(
            jobs, channels, busy, transfer_min=settings.transfer_min,
            per_sample={spec.id for spec in context.stations if spec.per_sample},
            time_limit_sec=settings.scheduler_cpsat_time_limit_sec,
            precedence=[(before, after) for after, refs in (upstream or {}).items() for before in refs],
            release={
                batch_id: max(0, round((floor - begin).total_seconds() / 60))
                for batch_id, floor in (floors or {}).items()
            },
        )
        return {
            "status": solution.status, "order": solution.order, "span_min": solution.span_min,
            "gap_pct": solution.gap_pct, "wall_ms": solution.wall_ms,
            "note": "CP-SAT 不含承运与清洗缓冲，时间窗以排程器按该顺序生成的结果为准",
        }

    def apply_optimized(self, order: list[str], user: User, start_from: datetime | None = None) -> list[dict]:
        self.gate.require_open()
        lock_schedule(self.db)
        applied = []
        begin = as_utc(start_from) or (now() + timedelta(minutes=5))
        batches = [self._require(batch_id) for batch_id in order]
        # 与预览一致：先把所选批次的旧时间窗全部清掉，再按顺序逐个排。否则排第一个批次时
        # 后面几个批次的旧占用还在，结果与操作员确认的预览对不上
        for batch in batches:
            if batch.state not in {"planned", "scheduled"}:
                raise StateConflict(f"{batch.id} 已下发，不能参与重新优化", code="batch_not_reschedulable")
            self.allocations.delete_for_batch(batch.id)
        self.db.flush()
        for batch in batches:
            rows = self.schedule(batch, begin, None, user, allow_proposals=False)
            applied.append({"batch_id": batch.id, "steps": len([r for r in rows if r.kind == WORK])})
        self.audit.record(user, "应用优化排程", "、".join(order), detail=f"{len(order)} 个批次按优化顺序重排")
        self.db.commit()
        return applied

    def _require(self, batch_id: str) -> Batch:
        batch = self.batches.get(batch_id)
        if not batch:
            raise NotFound(f"批次 {batch_id} 不存在")
        return batch

    def reschedule_from(self, batch: Batch, from_step: int, start_from: datetime, user: User) -> dict:
        """加急与重排只改未执行部分。

        正在执行、结果未知、不可中断的步骤保持占用：把它们的时间窗留在原处，
        只对之后的步骤重新求解，确认后原子替换。
        """
        self.gate.require_open()
        lock_schedule(self.db)
        start_from = as_utc(start_from)
        if batch.state not in RESCHEDULABLE:
            raise StateConflict(
                f"批次处于「{batch.state}」，只有未结束的批次可以重排",
                code="batch_not_reschedulable",
            )
        steps = normalize(batch.recipe_snapshot.get("steps") or [])
        if from_step < 0 or from_step >= len(steps):
            raise StateConflict("没有未执行的步骤需要重排")
        # 依赖图里序号靠后的步骤可以早已与靠前的并行开出，也可能是没走的分支：冻结看的是全部开出或
        # 判定过的步骤（执行中、保持、结果未知、已完成、已跳过、未走此分支），与滚动重排、按实际进度对齐
        # 同一条规则。它们原地保留时间窗，其余第 from_step 步起还没开出的步骤重排
        frozen = self.frozen_steps(batch) if batch.state in {"running", "paused", "fault"} else set()
        if from_step in frozen:
            raise StateConflict(
                f"第 {from_step + 1} 步已经开出或已判定（执行中、保持、结果未知、已完成、已跳过或未走此分支），"
                f"不能从它重排；只能重排还没开出的步骤",
                {"blocked": [{"key": "step", "label": "已开出的步骤保持原有占用与指令"}]},
                code="step_in_progress",
            )
        replan = [index for index in range(from_step, len(steps)) if index not in frozen]
        if batch.state in {"planned", "scheduled"}:
            # 与初次排程、重排建议同一条规则：上游任务的批次结束之前不开工
            floor, missing = self.dependency_floor(batch)
            if missing:
                raise StateConflict(
                    "上游任务还定不下来，无法重排",
                    {"blocked": [{"key": "dependency", "label": text} for text in missing]},
                    code="dependency_unscheduled",
                )
            if floor is not None and floor > start_from:
                start_from = floor
        protected = [a for a in self.allocations.for_batch(batch.id) if a.step_index not in replan]
        keep = {index for index in range(len(steps)) if index not in replan}
        known_ends, known_where, plate_state = self._known_tail(batch, steps, keep, protected)
        context = self.context({batch.id})
        for allocation in protected:
            context.busy.setdefault(allocation.station_id, []).append(
                Interval(allocation.starts_at, allocation.ends_at, max(1, int(allocation.units or 1)))
            )
        try:
            planned = plan_steps(
                steps, start_from, context, first_index=from_step, frozen=frozen, known_ends=known_ends,
                known_where=known_where, plate_state=plate_state, exclusive_carrier=self.carrier_roles(batch),
                samples=self.samples_of(batch),
            )
        except SchedulingError as error:
            raise StateConflict(error.message, {"step_index": error.step_index}) from error
        tail = steps
        asset_of_station = {
            station.id: station.asset_id for station in self.stations.list() if station.asset_id
        }
        self.allocations.delete_steps(batch.id, replan)
        for item in planned:
            self.db.add(
                Allocation(
                    batch_id=batch.id, step_index=item.step_index,
                    station_id=item.station_id,
                    asset_id=asset_of_station.get(item.station_id, ""),
                    starts_at=item.starts_at, ends_at=item.ends_at, kind=item.kind, units=item.units,
                )
            )
        if self._replans_the_start(batch, steps, replan):
            # 没有前驱的非设备节点（开头的备料、静置）按计划起点推算：起点不跟着改，
            # 之后的尾段重排、交期与下游依赖都会拿旧起点算，设备被排到静置结束之前
            batch.planned_start_at = start_from
        self.db.flush()
        self._refuse_overlaps(batch.id)
        self._book_people(batch, None)
        self.audit.record(
            user, "重排未执行步骤", batch.id, before=f"自第 {from_step + 1} 步",
            after=start_from.isoformat(timespec="minutes"),
            detail=(
                f"保护 {len(protected)} 个已占用时间窗；重排 "
                f"{len([i for i in planned if i.kind == WORK])} 个需占用步骤"
            ),
        )
        self.db.commit()
        return {
            "batch_id": batch.id,
            "protected_steps": sorted({a.step_index for a in protected}),
            "replanned": [self._planned_out(item, tail) for item in planned],
        }

    @staticmethod
    def _replans_the_start(batch: Batch, steps: list[dict], replan: list[int]) -> bool:
        """这次重排是否重新决定了批次的开始：还没开跑，且全部没有前驱的步骤都在重排范围里。"""
        from ..domain import graph

        if batch.state not in {"planned", "scheduled"}:
            return False
        before = graph.predecessors(steps)
        roots = [index for index in range(len(steps)) if not before[index]]
        return bool(roots) and set(roots) <= set(replan)

    def _cross_batch_overlaps(self, batch_id: str) -> list[dict]:
        return [
            row for row in self._overlaps()
            if batch_id in {row["a"]["batch_id"], row["b"]["batch_id"]}
            and row["a"]["batch_id"] != row["b"]["batch_id"]
        ]

    def realign(self, batch: Batch, step_index: int, actual_start: datetime) -> dict:
        """按实际进度对齐本批自这一步起的时间窗。

        - 前面做得快：试着把剩余时间窗提前到现在；与别的批次冲突就不提前，
          指令等到原时间窗前的允许提前量再投递——不占用别人预约的设备。
        - 前面做得慢：剩余时间窗后移。后移后与别的批次重叠时只报警，不自动挤占
          对方——谁让路是调度决定，不是算法决定。

        「剩余」指这一步与它在图上的后继里还没开出的步骤：已开出的并行分支、与这一步无关的分支都不动
        （按列表序号平移后缀，会把已经在跑的分支挪走，计划就不再表达实际占用）。每个后继至少跟着它的
        前驱移动同样的量；提前时不早于它不在平移范围内的前驱结束。线性流程里后继就是后面的全部步骤，
        与整体平移一致。
        """
        work = self.allocations.work_step(batch.id, step_index)
        if work is None:
            return {"shifted_min": 0, "conflicts": []}
        # 与指令的最早投递时刻用同一个基准：设备工作时间窗的开始
        delay = actual_start - work.starts_at
        if -timedelta(minutes=settings.early_start_tolerance_min) <= delay <= timedelta(
            minutes=settings.realign_grace_min
        ):
            return {"shifted_min": 0, "conflicts": []}
        lock_schedule(self.db)
        steps = normalize(batch.recipe_snapshot.get("steps") or [])
        shifts = self._realign_shifts(batch, steps, step_index, delay) if steps else {step_index: delay}
        self.allocations.shift_steps(batch.id, shifts)
        self.db.flush()
        # 本批次的下一步提前到了前一步的清洗窗口里：同一批次接着用未清洗的工位，那段清洗不再需要
        absorbed = self._own_clean_overlaps(batch.id)
        conflicts = self._cross_batch_overlaps(batch.id)
        # 与初次排程写入前同一组检查：工位通道之外，共享资产容量（含维护 / 校准预约）也不能被挤爆
        overloads = self._asset_overloads(batch.id, ignore_ids={row.id for row in absorbed})
        minutes = delay.total_seconds() / 60
        moved = len([index for index, value in shifts.items() if value])
        if delay < timedelta():
            if conflicts or overloads:
                self.allocations.shift_steps(batch.id, {index: -value for index, value in shifts.items()})
                self.db.flush()
                return {"shifted_min": 0, "conflicts": [], "waiting": True}
            for row in absorbed:
                self.db.delete(row)
            self.db.flush()
            self.audit.record(
                None, "按实际进度提前", batch.id, before=f"第 {step_index + 1} 步计划开工",
                after=f"提前 {-minutes:.0f} min",
                detail=f"上游提前完成，第 {step_index + 1} 步及其后继共 {moved} 步的时间窗提前；未与其他批次重叠",
            )
            return {"shifted_min": round(minutes), "conflicts": []}
        self.audit.record(
            None, "按实际进度顺延", batch.id, before=f"第 {step_index + 1} 步计划开工",
            after=f"后移 {minutes:.0f} min",
            detail=(
                f"第 {step_index + 1} 步及其后继共 {moved} 步的时间窗后移（已开出与无关的分支不动）"
                + (f"；与 {len(conflicts)} 处其他批次占用重叠，已报警" if conflicts else "")
            ),
        )
        self._raise_dependency_alarm(batch, self.dependency_conflicts(batch))
        if overloads:
            from .alarm_service import AlarmService

            AlarmService(self.db, self.ctx).raise_alarm(
                severity=2, source_type="batch", source_id=batch.id,
                message=f"{batch.id} 顺延 {minutes:.0f} min 后共享资产超容量：{overloads[0]}",
                response="在排程页决定哪一方让路（重排其一），或调整维护 / 校准预约。",
                owner="调度", origin="system", condition_key=f"batch:{batch.id}:asset_overload",
            )
        if conflicts:
            from .alarm_service import AlarmService

            others = sorted({
                row["b"]["batch_id"] if row["a"]["batch_id"] == batch.id else row["a"]["batch_id"]
                for row in conflicts
            })
            AlarmService(self.db, self.ctx).raise_alarm(
                severity=2, source_type="batch", source_id=batch.id,
                message=(
                    f"{batch.id} 顺延 {minutes:.0f} min 后与 {'、'.join(others)} 的工位时间窗重叠"
                ),
                response="在排程页决定哪一方让路（重排其一）；系统不自动挤占其他批次的预约。",
                owner="调度", origin="system", condition_key=f"batch:{batch.id}:schedule_conflict",
            )
        return {"shifted_min": round(minutes), "conflicts": conflicts}

    def _realign_shifts(
        self, batch: Batch, steps: list[dict], step_index: int, delay: timedelta,
    ) -> dict[int, timedelta]:
        """这一步与它还没开出的后继各自平移多少。

        按图的拓扑顺序（前驱序号总在前面）传递：每个后继至少与它在平移范围内的前驱移动同样的量，
        原有的间隔（转运、清洗）不被压缩；提前时也不早于它不在范围内的前驱（另一条分支）结束。
        静置、等待这类非设备节点没有时间窗，也要参与这条约束：它的原计划开始取计划结束减去时长，
        经它汇合的下游才会等另一条分支结束，而不是跟着这一支一起提前。
        """
        from ..domain import graph

        before = graph.predecessors(steps)
        frozen = self.frozen_steps(batch) - {step_index}
        affected = [step_index, *sorted(graph.descendants(steps, step_index) - frozen)]
        starts = {row.step_index: row.starts_at for row in self.allocations.for_batch(batch.id) if row.kind == WORK}
        ends = self.step_ends(batch) if delay < timedelta() else {}
        shifts: dict[int, timedelta] = {}
        for index in affected:
            if index == step_index:
                shifts[index] = delay
                continue
            candidates = [delay, *(shifts[parent] for parent in before[index] if parent in shifts)]
            start = starts.get(index)
            if start is None and index in ends:
                start = ends[index] - timedelta(minutes=float(steps[index].get("dur") or 0))
            if start is not None:
                candidates += [
                    ends[parent] - start for parent in before[index] if parent not in shifts and parent in ends
                ]
            shifts[index] = max(candidates)
        return shifts

    def extend_after_hold(self, batch: Batch, held: set[int], minutes: float) -> None:
        """保持后续跑 / 重试：被保持的步骤时间窗延长（保持期间设备仍占着），它们还没开出的后继跟着后移。

        另一条分支上已开出的步骤、与被保持步骤无关的步骤不动。线性流程里就是当前步骤延长、后面全部后移。
        """
        from ..domain import graph

        steps = normalize(batch.recipe_snapshot.get("steps") or [])
        delta = timedelta(minutes=minutes)
        frozen = self.frozen_steps(batch)
        later: set[int] = set()
        for index in held:
            if index < len(steps):
                later |= graph.descendants(steps, index)
        later = {index for index in later - held if index not in frozen}
        moment = now()
        for allocation in self.allocations.for_batch(batch.id):
            if allocation.step_index in held:
                if allocation.kind == TRANSFER and allocation.starts_at <= moment:
                    # 转运在开工前就做完了（保持只发生在动作进行中）：它不随保持延长，否则承运工位上
                    # 多出一段幻影占用，别的批次排不进这台本来空着的 AGV
                    continue
                allocation.ends_at = allocation.ends_at + delta
                if allocation.kind not in {WORK, "assist"}:
                    allocation.starts_at = allocation.starts_at + delta
            elif allocation.step_index in later:
                allocation.starts_at = allocation.starts_at + delta
                allocation.ends_at = allocation.ends_at + delta

    def release_unused(
        self, batch: Batch, step_index: int, finished_at: datetime, started_at: datetime | None = None,
    ) -> None:
        """设备提前完成：把这一步没用完的时间窗还回去，清洗缓冲跟着前移。

        在计划时间窗开始之前就做完了（允许提前量内开工的短步骤）：工作时间窗改成实际的起止。
        只把清洗挪到完成时刻、留下原来的工作窗，两段会叠在同一台设备上，之后这个批次的任何重排
        都会被资产容量检查拒绝。协同工位的时间窗与主设备同起同止，一起处理。
        """
        for allocation in self.allocations.for_batch(batch.id):
            if allocation.step_index != step_index:
                continue
            if allocation.kind in {WORK, "assist"}:
                if allocation.starts_at < finished_at < allocation.ends_at:
                    allocation.ends_at = finished_at
                elif finished_at <= allocation.starts_at:
                    allocation.starts_at = min(started_at or finished_at, finished_at)
                    allocation.ends_at = finished_at
            elif allocation.kind == CLEAN and allocation.starts_at > finished_at:
                duration = allocation.ends_at - allocation.starts_at
                allocation.starts_at = finished_at
                allocation.ends_at = finished_at + duration

    @staticmethod
    def allocation_out(allocation: Allocation, forecast: str = "") -> dict:
        return {
            "step_index": allocation.step_index,
            "station_id": allocation.station_id,
            "kind": allocation.kind,
            "units": max(1, int(allocation.units or 1)),
            "starts_at": allocation.starts_at.isoformat(timespec="minutes"),
            "ends_at": allocation.ends_at.isoformat(timespec="minutes"),
            "transfer": allocation.kind == TRANSFER,
            "clean": allocation.kind == CLEAN,
            "assist": allocation.kind == "assist",
            # 空串表示承诺窗口；否则是预测，写明为什么（分支未定、冻结期之后）
            "forecast": bool(forecast),
            "forecast_reason": forecast,
        }
