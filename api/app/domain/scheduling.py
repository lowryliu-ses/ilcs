"""步骤级贪心排程器。

纯函数：输入步骤、工位、现有占用，输出步骤级分配或失败原因。数据模型是步骤级的，
将来换成 CP-SAT 求解器不需要改这个模型，只替换 `plan_steps` 的实现。
"""
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .capability import StationSpec, station_fits
from .graph import predecessors
from .steps import assist_capabilities, labware_role, needs_station

WORK = "work"
TRANSFER = "transfer"
CLEAN = "clean"
# 协同资源：主设备执行期间一并占用的其他工位（机械臂、配套设备、放置位）
ASSIST = "assist"


@dataclass(frozen=True)
class Interval:
    start: datetime
    end: datetime


@dataclass(frozen=True)
class PlannedAllocation:
    step_index: int
    station_id: str
    starts_at: datetime
    ends_at: datetime
    kind: str = WORK


@dataclass
class SchedulingContext:
    stations: list[StationSpec]
    busy: dict[str, list[Interval]] = field(default_factory=dict)
    held_station_ids: set[str] = field(default_factory=set)
    transfer_station_ids: list[str] = field(default_factory=list)
    transfer_min: int = 10
    clean_min: int = 10
    prefer_station_id: str | None = None
    allow_unclean: bool = False
    # 执行门判定不可用的工位（失联、心跳超时）→ 原因
    unavailable_station_ids: dict[str, str] = field(default_factory=dict)
    # 资产级约束：多个工位映射同一台资产时共享容量；维护 / 校准 / 人工预约占用资产
    station_asset: dict[str, str] = field(default_factory=dict)
    asset_capacity: dict[str, int] = field(default_factory=dict)
    # 资产上的预约：(区间, 占用份数)。维护与校准占满整台资产
    asset_bookings: dict[str, list[tuple[Interval, int]]] = field(default_factory=dict)


class SchedulingError(Exception):
    def __init__(self, message: str, step_index: int):
        super().__init__(message)
        self.message = message
        self.step_index = step_index


def _channels(context: SchedulingContext, station_id: str) -> int:
    """工位并行通道数：同一时刻能同时承接几个批次（充放电柜按通道）。默认 1。

    不是样品位——样品位是一个批次最多放几个样本，和能不能同时跑两个批次无关。
    """
    spec = next((s for s in context.stations if s.id == station_id), None)
    return max(1, int(spec.channels)) if spec is not None and spec.channels else 1


def _station_free(context: SchedulingContext, station_id: str, not_before: datetime, duration: timedelta) -> datetime:
    """工位上同时进行的时间窗少于并行容量的最早时刻。

    重叠数按落在窗口里的区间直接计数，是并发量的上界：宁可多等，也不把通道排超。
    """
    channels = _channels(context, station_id)
    intervals = sorted(context.busy.get(station_id, []), key=lambda i: i.start)
    cursor = not_before
    for _ in range(10_000):
        overlapping = [i for i in intervals if i.start < cursor + duration and i.end > cursor]
        if len(overlapping) < channels:
            return cursor
        cursor = min(i.end for i in overlapping)
    raise SchedulingError(f"{station_id} 在可见时间范围内没有空闲通道", -1)


def peak_load(occupied: list[tuple[Interval, int]], window: Interval) -> int:
    """窗口内同一时刻压着的最大份数。前后错开的两段占用不叠加；一段结束的时刻另一段才开始也不叠加。"""
    events: list[tuple[datetime, int]] = []
    for interval, units in occupied:
        if interval.start < window.end and interval.end > window.start:
            events.append((max(interval.start, window.start), units))
            events.append((min(interval.end, window.end), -units))
    # 同一时刻先结束再开始
    events.sort(key=lambda event: (event[0], event[1]))
    running = peak = 0
    for _, delta in events:
        running += delta
        peak = max(peak, running)
    return peak


def _asset_load(context: SchedulingContext, asset_id: str, window: Interval):
    """窗口内压在这台资产上的占用：预约 + 映射到它的全部工位（含本工位）的时间窗。

    返回 (同一时刻的最大份数, 最早结束时刻)。工位通道数与资产容量是两道独立的约束：
    本工位的并行作业同样占资产容量，不能因为工位通道约束另算了就把它排除在外。
    """
    occupied = list(context.asset_bookings.get(asset_id, []))
    for station_id, mapped in context.station_asset.items():
        if mapped != asset_id:
            continue
        occupied.extend((interval, 1) for interval in context.busy.get(station_id, []))
    ends = [
        interval.end for interval, _ in occupied
        if interval.start < window.end and interval.end > window.start
    ]
    return peak_load(occupied, window), (min(ends) if ends else None)


def earliest_free(context: SchedulingContext, station_id: str, not_before: datetime, duration: timedelta) -> datetime:
    """工位本身空闲，且所属资产在整段时间内还有余量的最早开工时刻。"""
    cursor = not_before
    asset_id = context.station_asset.get(station_id)
    for _ in range(1000):
        cursor = _station_free(context, station_id, cursor, duration)
        if not asset_id:
            return cursor
        capacity = max(1, context.asset_capacity.get(asset_id, 1))
        load, first_end = _asset_load(context, asset_id, Interval(cursor, cursor + duration))
        if load + 1 <= capacity or first_end is None:
            return cursor
        cursor = max(first_end, cursor + timedelta(seconds=1))
    raise SchedulingError(f"{station_id} 所属资产在可见时间范围内没有余量", -1)


def _occupy(context: SchedulingContext, allocation: PlannedAllocation) -> None:
    context.busy.setdefault(allocation.station_id, []).append(
        Interval(allocation.starts_at, allocation.ends_at)
    )


def _candidates(context: SchedulingContext, step: dict[str, Any], index: int) -> list[StationSpec]:
    able = [s for s in context.stations if station_fits(s, step)]
    if not able:
        raise SchedulingError(f"第 {index + 1} 步「{step.get('name')}」没有可承接工位", index)
    usable = [
        s for s in able
        if s.healthy and (context.allow_unclean or s.clean)
        and s.id not in context.held_station_ids and s.id not in context.unavailable_station_ids
    ]
    if not usable:
        unavailable = [context.unavailable_station_ids[s.id] for s in able if s.id in context.unavailable_station_ids]
        held = [s.id for s in able if s.id in context.held_station_ids]
        faulty = [f"{s.id}{'离线' if s.status == 'offline' else ''}" for s in able if not s.healthy]
        unclean = [s.id for s in able if not s.clean]
        if unavailable and len(unavailable) == len(able):
            why = "；".join(unavailable)
        elif held:
            why = f"{'、'.join(held)} 正在保持，释放时间未知"
        elif faulty:
            why = f"{'、'.join(faulty)} 故障"
        else:
            why = f"{'、'.join(unclean)} 未完成清洗"
        raise SchedulingError(f"第 {index + 1} 步「{step.get('name')}」无可执行工位：{why}", index)
    if context.prefer_station_id and any(s.id == context.prefer_station_id for s in usable):
        return [s for s in usable if s.id == context.prefer_station_id]
    return usable


def _assist_candidates(
    context: SchedulingContext, step: dict[str, Any], index: int, capability: str, exclude: set[str],
) -> list[StationSpec]:
    probe = {"cap": capability, "params": {}}
    usable = [
        s for s in context.stations
        if s.id not in exclude and station_fits(s, probe) and s.healthy
        and (context.allow_unclean or s.clean)
        and s.id not in context.held_station_ids and s.id not in context.unavailable_station_ids
    ]
    if not usable:
        raise SchedulingError(
            f"第 {index + 1} 步「{step.get('name')}」需要的协同资源 {capability} 没有可用工位", index,
        )
    return usable


def _with_assists(
    context: SchedulingContext, step: dict[str, Any], index: int, main: StationSpec, begin: datetime,
    duration: timedelta,
) -> tuple[datetime, list[StationSpec]]:
    """主设备与每种协同资源在同一时段都空着的最早时刻，以及选中的协同工位。

    协同资源与主设备一起取得、一起释放：任何一种凑不齐，这一步就整体往后排，不会只占到一半。
    """
    options = {
        capability: _assist_candidates(context, step, index, capability, {main.id})
        for capability in assist_capabilities(step)
    }
    for _ in range(500):
        chosen: list[StationSpec] = []
        latest = begin
        for capability, stations in options.items():
            free = [s for s in stations if s.id not in {c.id for c in chosen}]
            if not free:
                raise SchedulingError(
                    f"第 {index + 1} 步「{step.get('name')}」的协同资源 {capability} 没有另一台可用工位", index,
                )
            when, station = min(((earliest_free(context, s.id, begin, duration), s) for s in free),
                                key=lambda pair: (pair[0], pair[1].id))
            chosen.append(station)
            latest = max(latest, when)
        if latest == begin:
            return begin, chosen
        begin = earliest_free(context, main.id, latest, duration)
    raise SchedulingError(
        f"第 {index + 1} 步「{step.get('name')}」主设备与协同资源在可见时间范围内凑不到同一时段", index,
    )


def candidate_station_ids(context: SchedulingContext, step: dict[str, Any], index: int) -> list[str]:
    """这一步当前可以排上去的工位（能力、参数范围、健康、清洗、保持、可用性都过了）。"""
    return [station.id for station in _candidates(context, step, index)]


def plan_steps(
    steps: list[dict[str, Any]], start_from: datetime, context: SchedulingContext,
    *, first_index: int = 0, previous_end: datetime | None = None,
    previous_station: str | None = None, step_ends: dict[int, datetime] | None = None,
    exclusive_carrier: bool | Collection[str] = False,
) -> list[PlannedAllocation]:
    """按步骤资源需求排程。

    人工、等待、审核节点不占工位（除非显式声明），但它们的时长照样往后推时间线——
    否则下游设备步骤会被排到一个「上一步还没做完」的时刻。

    只重排后半段时传 `first_index`、`previous_end`（上一步实际或计划结束时刻）与
    `previous_station`：尾段不能早于上一步结束开工，换工位要排转运，硬时限也从上一步
    结束起算——而不是从操作员填的「期望开始时间」起算。

    `step_ends` 传入时填上每一步（含不占工位的步骤）的计划结束：批次完成看它们的最大值，
    设备时间窗之后的静置、培养、冷却同样是工艺时间。

    `exclusive_carrier`：批次绑定了载具时，设备步骤与运行时一样一次只能有一个在用同一块板——
    用同一块板的设备步骤按步骤顺序一个接一个排，板换设备要排转运。传 True 表示只有主载具；
    传角色集合表示这些角色各有一块板（步骤按 `labware` 取板，没写的用主载具），不同板上的步骤可以并行。

    步骤声明了协同资源（`assist`）时，主设备与每种协同资源要在同一时段都空着，协同工位记一段
    `assist` 时间窗，与主设备的时间窗同起同止。
    """
    allocations: list[PlannedAllocation] = []
    not_before = start_from
    tail_end = previous_end or start_from
    tail_station = previous_station
    before = predecessors(steps)
    # 每一步结束的时刻与结束后载具所在的工位：后继从最晚结束的那个前驱接手
    ends: dict[int, datetime] = {}
    where: dict[int, str | None] = {}
    # 独占载具时每块板（按角色）上一次设备动作结束的时刻与所在工位
    roles = {""} if exclusive_carrier is True else set(exclusive_carrier or ())
    plate_free: dict[str, datetime] = {}
    plate_at: dict[str, str] = {}

    for index, step in enumerate(steps):
        if index < first_index:
            continue
        known = [parent for parent in before[index] if parent in ends]
        if known:
            anchor = max(known, key=lambda parent: (ends[parent], parent))
            previous_end, previous_station = ends[anchor], where[anchor]
            if len(known) < len(before[index]):
                # 部分前驱在重排范围之前：它们的结束由调用方给出的尾段起点代表
                previous_end = max(previous_end, tail_end)
        else:
            previous_end, previous_station = tail_end, tail_station
        duration = timedelta(minutes=float(step.get("dur", 0) or 0))
        if not needs_station(step):
            ends[index] = max(previous_end, not_before) + duration
            where[index] = previous_station
            continue
        candidates = _candidates(context, step, index)
        # 硬时限从前驱结束起算；独占载具时开工还要等板从上一次设备动作上空出来，并从那台设备搬过来
        gap_from = previous_end
        ready_from = previous_end
        role = labware_role(step)
        if role in roles and role in plate_free:
            ready_from = max(previous_end, plate_free[role])
            previous_station = plate_at[role]
        elif role in roles and role:
            # 另一块板第一次上设备：它不在前驱所在的工位上
            previous_station = None

        best: tuple[datetime, StationSpec, bool] | None = None
        for station in candidates:
            needs_transfer = previous_station is not None and station.id != previous_station
            ready_at = max(
                not_before,
                ready_from + (timedelta(minutes=context.transfer_min) if needs_transfer else timedelta()),
            )
            begin = earliest_free(context, station.id, ready_at, duration)
            if best is None or begin < best[0] or (begin == best[0] and station.id < best[1].id):
                best = (begin, station, needs_transfer)

        begin, station, needs_transfer = best  # type: ignore[misc]

        if needs_transfer and context.transfer_station_ids:
            transfer_duration = timedelta(minutes=context.transfer_min)
            carrier = min(
                context.transfer_station_ids,
                key=lambda sid: (earliest_free(context, sid, ready_from, transfer_duration), sid),
            )
            transfer_start = earliest_free(context, carrier, ready_from, transfer_duration)
            transfer = PlannedAllocation(index, carrier, transfer_start, transfer_start + transfer_duration, TRANSFER)
            allocations.append(transfer)
            _occupy(context, transfer)
            # 设备工作开始不得早于实际转运完成：不是画面上画一个区间就算
            if transfer.ends_at > begin:
                begin = earliest_free(context, station.id, transfer.ends_at, duration)

        helpers: list[StationSpec] = []
        if assist_capabilities(step):
            begin, helpers = _with_assists(context, step, index, station, begin, duration)

        # 硬时限必须在转运把开工时间往后推之后再判：先判后推会放过转运车忙导致的超时
        hard = step.get("hard") or {}
        if "maxGapMin" in hard:
            gap_min = (begin - gap_from).total_seconds() / 60
            if gap_min > float(hard["maxGapMin"]):
                raise SchedulingError(
                    f"第 {index + 1} 步「{step.get('name')}」硬时限 {hard['maxGapMin']} min 无法满足："
                    f"最早可开工时间在上一步结束 {gap_min:.0f} min 后",
                    index,
                )

        work = PlannedAllocation(index, station.id, begin, begin + duration, WORK)
        allocations.append(work)
        _occupy(context, work)
        for helper in helpers:
            assist = PlannedAllocation(index, helper.id, begin, begin + duration, ASSIST)
            allocations.append(assist)
            _occupy(context, assist)

        next_step = next(
            (steps[i] for i in range(index + 1, len(steps)) if needs_station(steps[i])), None
        )
        stays = next_step is not None and station_fits(station, next_step)
        if not stays and context.clean_min:
            clean = PlannedAllocation(
                index, station.id, work.ends_at, work.ends_at + timedelta(minutes=context.clean_min), CLEAN
            )
            allocations.append(clean)
            _occupy(context, clean)

        ends[index] = work.ends_at
        where[index] = station.id
        if role in roles:
            plate_free[role], plate_at[role] = work.ends_at, station.id

    if step_ends is not None:
        step_ends.update(ends)
    return allocations


def planned_finish(allocations: list[PlannedAllocation], step_ends: dict[int, datetime]) -> datetime | None:
    """一次排程的计划完成：全部步骤（含不占工位的等待）结束的最晚时刻。"""
    candidates = [*step_ends.values(), *(a.ends_at for a in allocations if a.kind == WORK)]
    return max(candidates) if candidates else None


def makespan(allocations: list[PlannedAllocation]) -> timedelta:
    work = [a for a in allocations if a.kind == WORK]
    if not work:
        return timedelta()
    return max(a.ends_at for a in work) - min(a.starts_at for a in work)
