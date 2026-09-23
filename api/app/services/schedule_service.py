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
    plan_steps,
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
        held = set()
        for batch in self.batches.by_state(*HELD_STATES):
            allocation = self.allocations.work_step(batch.id, batch.current_step)
            if allocation:
                held.add(allocation.station_id)
        return held

    def context(
        self, exclude_batch_ids: set[str] | None = None, prefer: str | None = None, allow_unclean: bool = True
    ) -> SchedulingContext:
        return SchedulingContext(
            stations=self.stations.specs(),
            busy=self.allocations.busy_timeline(exclude_batch_ids),
            held_station_ids=self.held_station_ids(),
            # 失联 / 心跳超时的设备、处于维护或已退役资产上的工位不承接新排程
            unavailable_station_ids=self._unavailable_stations(),
            **self._asset_constraints(),
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

    # ---------- 写 ----------

    def schedule(self, batch: Batch, start_from: datetime | None, prefer: str | None, user: User) -> list[Allocation]:
        self.gate.require_open()
        lock_schedule(self.db)
        start_from = as_utc(start_from)
        if batch.state not in {"planned", "scheduled"}:
            raise StateConflict("只有计划中或已排程批次可重新排程")
        steps = normalize(batch.recipe_snapshot.get("steps") or [])
        if not steps:
            raise StateConflict("方法快照没有步骤")
        bom = batch.recipe_snapshot.get("bom") or []
        # 合法空 BOM 的方法不做物料检查，否则纯人工流程永远排不上
        if bom and not self.materials.bom_satisfied(batch.id, bom):
            raise StateConflict("物料预留不完整，排程前必须先完成预留")
        demand = resource_demand(steps)
        if demand["needs_station"] == 0:
            # 全是人工 / 等待 / 审核节点：没有工位要占，直接进入已排程
            self.allocations.delete_for_batch(batch.id)
            batch.state = "scheduled"
            batch.current_step = 0
            self.audit.record(
                user, "排程批次", batch.id, before="计划", after="已排程",
                detail=f"{demand['total']} 步全部不占工位，无需资源预约",
            )
            self.db.flush()
            return []

        begin = start_from or (now() + timedelta(minutes=5))
        try:
            planned = plan_steps(steps, begin, self.context({batch.id}, prefer))
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
                starts_at=item.starts_at, ends_at=item.ends_at, kind=item.kind,
            )
            for item in planned
        ]
        self.db.add_all(rows)
        self.db.flush()
        self._refuse_overlaps(batch.id)
        batch.state = "scheduled"
        batch.current_step = 0
        work = [item for item in planned if item.kind == WORK]
        self.audit.record(
            user, "排程批次", batch.id, before="计划", after="已排程",
            detail=(
                f"{len(work)}/{demand['needs_station']} 个需占用步骤已预约，"
                f"{work[0].starts_at:%m-%d %H:%M} 起，跨度 "
                f"{makespan(planned).total_seconds() / 60:.0f} min"
            ),
        )
        self.db.flush()
        return rows

    # ---------- 读 ----------

    def _refuse_overlaps(self, batch_id: str) -> None:
        """写入前的最后一道守门：锁内求解本不该与别的批次重叠，重叠了就拒绝而不是照写。"""
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

    def dry_run(self, batch: Batch, start_from: datetime | None = None) -> dict:
        steps = normalize(batch.recipe_snapshot.get("steps") or [])
        begin = as_utc(start_from) or (now() + timedelta(minutes=5))
        try:
            planned = plan_steps(steps, begin, self.context({batch.id}))
        except SchedulingError as error:
            return {"ok": False, "reason": error.message, "step_index": error.step_index, "path": []}
        return {
            "ok": True,
            "reason": "",
            "step_index": None,
            "makespan_min": round(makespan(planned).total_seconds() / 60),
            "path": [self._planned_out(item, steps) for item in planned],
        }

    @staticmethod
    def _planned_out(item: PlannedAllocation, steps: list[dict]) -> dict:
        return {
            "step_index": item.step_index,
            "step_name": (steps[item.step_index] or {}).get("name") if item.step_index < len(steps) else "",
            "station_id": item.station_id,
            "kind": item.kind,
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
                    "plan_id": batch.plan_id,
                    "material_ok": material_ok,
                    "schedulable": preview["ok"],
                    "blocker": preview["reason"],
                    "makespan_min": preview.get("makespan_min"),
                    "path": preview["path"],
                }
            )
        return sorted(rows, key=lambda r: (r["priority"], r["batch_id"]))

    def board(self) -> dict:
        """步骤级资源泳道。保持中工位标注释放时间未知，其后安排仅为预测。"""
        held = self.held_station_ids()
        batches = {b.id: b for b in self.batches.active()}
        lanes: dict[str, list[dict]] = {station.id: [] for station in self.stations.list()}
        for allocation in (
            self.db.query(Allocation).filter(Allocation.batch_id.in_(list(batches) or [""])).all()
        ):
            batch = batches[allocation.batch_id]
            steps = normalize(batch.recipe_snapshot.get("steps") or [])
            step = steps[allocation.step_index] if allocation.step_index < len(steps) else {}
            lanes.setdefault(allocation.station_id, []).append(
                {
                    "batch_id": allocation.batch_id,
                    "batch_state": batch.state,
                    "step_index": allocation.step_index,
                    "step_name": step.get("name"),
                    "step_kind": step.get("kind", "device"),
                    "kind": allocation.kind,
                    "hard": step.get("hard"),
                    "starts_at": allocation.starts_at.isoformat(timespec="minutes"),
                    "ends_at": allocation.ends_at.isoformat(timespec="minutes"),
                    "uncertain": allocation.station_id in held,
                }
            )
        return {
            "now": now().isoformat(timespec="minutes"),
            "stations": [
                {
                    "id": station.id, "name": station.name, "island": station.island, "status": station.status,
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
        与领域排程 `_station_free` 用同一口径，否则排程算出来的合法结果会在写入前被拒绝。
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
                if len(active) >= capacity:
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

    def optimize_preview(self, batch_ids: list[str], start_from: datetime | None = None) -> dict:
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

        def evaluate(order: tuple[str, ...]) -> optimizer.Candidate:
            """候选顺序共享一份工位时间线，先排的批次会占住资源，后排的只能往后挪。"""
            context = self.context(selected, allow_unclean=True)
            plans: dict[str, list[PlannedAllocation]] = {}
            for batch_id in order:
                try:
                    plans[batch_id] = plan_steps(steps_by_batch[batch_id], begin, context)
                except SchedulingError as error:
                    return optimizer.Candidate(order, False, reason=f"{batch_id}: {error.message}")
            work_items = [a for planned in plans.values() for a in planned if a.kind == WORK]
            if not work_items:
                return optimizer.Candidate(order, True, 0, 0, "所选批次都不占工位，无需优化顺序", {"plans": {}})
            finish = max(a.ends_at for a in work_items)
            completion = {
                batch_id: max((a.ends_at for a in planned if a.kind == WORK), default=begin)
                for batch_id, planned in plans.items()
            }
            weighted = sum(weight[b] * (completion[b] - begin).total_seconds() / 60 for b in order)
            return optimizer.Candidate(
                order, True, round((finish - begin).total_seconds() / 60), round(weighted), "",
                {"finish_at": finish.isoformat(timespec="minutes"), "plans": {
                    batch_id: [self._planned_out(a, steps_by_batch[batch_id]) for a in planned]
                    for batch_id, planned in plans.items()
                }},
            )

        def out(candidate: optimizer.Candidate) -> dict:
            return {
                "ok": candidate.ok, "reason": candidate.reason, "order": list(candidate.order),
                "finish_at": candidate.payload.get("finish_at", begin.isoformat(timespec="minutes")),
                "span_min": candidate.span_min if candidate.ok else None,
                "weighted_min": candidate.weighted_min if candidate.ok else None,
                "plans": candidate.payload.get("plans", {}),
            }

        ids = [b.id for b in batches]
        priority_order = tuple(sorted(ids, key=lambda b: (by_id[b].priority, b)))
        longest_first = tuple(sorted(ids, key=lambda b: (-critical_path_min(steps_by_batch[b]), b)))
        seeds = [priority_order, longest_first, tuple(reversed(longest_first))]

        solver_info = None
        if settings.scheduler_backend in {"auto", "cpsat"} and cpsat.available() and len(ids) > 1:
            solver_info = self._cpsat_order(batches, steps_by_batch, weight, selected, begin)
            if solver_info.get("order"):
                seeds.insert(0, tuple(solver_info["order"]))
        elif settings.scheduler_backend == "cpsat":
            solver_info = {"status": "unavailable", "reason": "未安装 ortools，已用内置顺序搜索"}

        baseline = evaluate(priority_order)
        report = optimizer.search(
            ids, evaluate, seeds=seeds, budget_sec=settings.scheduler_search_budget_sec,
        )
        if not report.best.ok:
            raise StateConflict("所有候选顺序都无法满足约束", {"reason": report.best.reason or baseline.reason})
        return {
            "baseline": out(baseline),
            "best": out(report.best),
            "improvement_min": (baseline.span_min - report.best.span_min) if baseline.ok else None,
            "evaluated": report.evaluated,
            "method": report.method,
            "elapsed_ms": report.elapsed_ms,
            "solver": solver_info,
        }

    def _cpsat_order(self, batches, steps_by_batch, weight, selected, begin) -> dict:
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
                for index, step in enumerate(steps):
                    stations = tuple(candidate_station_ids(context, step, index)) if needs_station(step) else ()
                    gap = (step.get("hard") or {}).get("maxGapMin")
                    specs.append(cpsat.StepSpec(
                        index=index, duration=max(0, round(float(step.get("dur") or 0))), stations=stations,
                        preds=tuple(before[index]), max_gap=round(float(gap)) if gap else None,
                    ))
                jobs.append(cpsat.JobSpec(batch.id, tuple(specs), weight[batch.id]))
        except SchedulingError as error:
            return {"status": "skipped", "reason": error.message}
        busy = {
            station_id: [
                (max(0, round((i.start - begin).total_seconds() / 60)), max(0, round((i.end - begin).total_seconds() / 60)))
                for i in intervals if i.end > begin
            ]
            for station_id, intervals in context.busy.items()
        }
        channels = {spec.id: max(1, int(spec.channels or 1)) for spec in context.stations}
        solution = cpsat.solve(
            jobs, channels, busy, transfer_min=settings.transfer_min,
            time_limit_sec=settings.scheduler_cpsat_time_limit_sec,
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
            rows = self.schedule(batch, begin, None, user)
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
        protected = [
            a for a in self.allocations.for_batch(batch.id) if a.step_index < from_step
        ]
        if batch.state in {"running", "paused", "fault"} and from_step <= batch.current_step:
            raise StateConflict(
                f"第 {batch.current_step + 1} 步正在执行或保持占用，不能从第 {from_step + 1} 步重排",
                {"blocked": [{"key": "step", "label": "正在执行的步骤保持占用"}]},
                code="step_in_progress",
            )
        anchor, anchor_station = self._tail_anchor(batch, steps, from_step, protected)
        context = self.context({batch.id})
        for allocation in protected:
            context.busy.setdefault(allocation.station_id, []).append(
                Interval(allocation.starts_at, allocation.ends_at)
            )
        try:
            planned = plan_steps(
                steps, max(start_from, anchor) if anchor else start_from, context,
                first_index=from_step, previous_end=anchor, previous_station=anchor_station,
            )
        except SchedulingError as error:
            raise StateConflict(error.message, {"step_index": error.step_index}) from error
        tail = steps
        asset_of_station = {
            station.id: station.asset_id for station in self.stations.list() if station.asset_id
        }
        self.allocations.delete_from_step(batch.id, from_step)
        for item in planned:
            self.db.add(
                Allocation(
                    batch_id=batch.id, step_index=item.step_index,
                    station_id=item.station_id,
                    asset_id=asset_of_station.get(item.station_id, ""),
                    starts_at=item.starts_at, ends_at=item.ends_at, kind=item.kind,
                )
            )
        self.db.flush()
        self._refuse_overlaps(batch.id)
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

    def _cross_batch_overlaps(self, batch_id: str) -> list[dict]:
        return [
            row for row in self._overlaps()
            if batch_id in {row["a"]["batch_id"], row["b"]["batch_id"]}
            and row["a"]["batch_id"] != row["b"]["batch_id"]
        ]

    def realign(self, batch: Batch, step_index: int, actual_start: datetime) -> dict:
        """按实际进度对齐本批自这一步起的时间窗。

        - 前面做得快：试着把剩余时间窗整体提前到现在；与别的批次冲突就不提前，
          指令等到原时间窗前的允许提前量再投递——不占用别人预约的设备。
        - 前面做得慢：剩余时间窗整体后移。后移后与别的批次重叠时只报警，不自动挤占
          对方——谁让路是调度决定，不是算法决定。
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
        self.allocations.shift_all_from_step(batch.id, step_index, delay)
        self.db.flush()
        conflicts = self._cross_batch_overlaps(batch.id)
        minutes = delay.total_seconds() / 60
        if delay < timedelta():
            if conflicts:
                self.allocations.shift_all_from_step(batch.id, step_index, -delay)
                self.db.flush()
                return {"shifted_min": 0, "conflicts": [], "waiting": True}
            self.audit.record(
                None, "按实际进度提前", batch.id, before=f"第 {step_index + 1} 步计划开工",
                after=f"提前 {-minutes:.0f} min",
                detail=f"上游提前完成，第 {step_index + 1} 步起的时间窗整体提前；未与其他批次重叠",
            )
            return {"shifted_min": round(minutes), "conflicts": []}
        self.audit.record(
            None, "按实际进度顺延", batch.id, before=f"第 {step_index + 1} 步计划开工",
            after=f"后移 {minutes:.0f} min",
            detail=(
                f"第 {step_index + 1} 步起的时间窗整体后移"
                + (f"；与 {len(conflicts)} 处其他批次占用重叠，已报警" if conflicts else "")
            ),
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

    def release_unused(self, batch: Batch, step_index: int, finished_at: datetime) -> None:
        """设备提前完成：把这一步没用完的时间窗还回去，清洗缓冲跟着前移。"""
        for allocation in self.allocations.for_batch(batch.id):
            if allocation.step_index != step_index:
                continue
            if allocation.kind == WORK and allocation.starts_at < finished_at < allocation.ends_at:
                allocation.ends_at = finished_at
            elif allocation.kind == CLEAN and allocation.starts_at > finished_at:
                duration = allocation.ends_at - allocation.starts_at
                allocation.starts_at = finished_at
                allocation.ends_at = finished_at + duration

    def _tail_anchor(
        self, batch: Batch, steps: list[dict], from_step: int, protected: list[Allocation],
    ) -> tuple[datetime | None, str | None]:
        """尾段的起点：上一步实际结束（有检查点或已完成的步骤实例）优先，否则按计划结束推算。"""
        if from_step == 0:
            return None, None
        from ..repositories.execution import CheckpointRepository
        from ..repositories.workflow import StepRunRepository

        work = sorted(
            (a for a in protected if a.kind == WORK), key=lambda a: (a.step_index, a.ends_at),
        )
        station = work[-1].station_id if work else None
        previous = from_step - 1
        checkpoint = CheckpointRepository(self.db).latest_for_step(batch.id, previous)
        if checkpoint is not None:
            return checkpoint.created_at, station
        finished = [
            run.ended_at for run in StepRunRepository(self.db, self.ctx).for_batch(batch.id)
            if run.step_index == previous and run.state == "completed" and run.ended_at
        ]
        if finished:
            return max(finished), station
        if not work:
            return None, None
        # 计划推算：最后一个受保护的设备时间窗结束，加上其后到本步之前那些不占工位步骤的时长
        anchor = work[-1].ends_at
        for index in range(work[-1].step_index + 1, from_step):
            if not needs_station(steps[index]):
                anchor += timedelta(minutes=float(steps[index].get("dur", 0) or 0))
        return anchor, station

    @staticmethod
    def allocation_out(allocation: Allocation) -> dict:
        return {
            "step_index": allocation.step_index,
            "station_id": allocation.station_id,
            "kind": allocation.kind,
            "starts_at": allocation.starts_at.isoformat(timespec="minutes"),
            "ends_at": allocation.ends_at.isoformat(timespec="minutes"),
            "transfer": allocation.kind == TRANSFER,
            "clean": allocation.kind == CLEAN,
        }
