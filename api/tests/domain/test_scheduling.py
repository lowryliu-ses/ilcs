"""排程器是纯函数，不需要数据库，也不需要 HTTP。"""
from datetime import datetime, timedelta

import pytest

from app.domain.capability import StationSpec
from app.domain.scheduling import CLEAN, TRANSFER, WORK, Interval, SchedulingContext, SchedulingError, plan_steps

T0 = datetime(2026, 9, 20, 8, 0)

MIXER_A = StationSpec(id="MIX-A", limits={"cap.mix": {"temp": [15, 80], "rpm": [0, 3000]}})
MIXER_B = StationSpec(id="MIX-B", limits={"cap.mix": {"temp": [15, 80], "rpm": [0, 3000]}})
COATER = StationSpec(id="COAT", limits={"cap.coat": {"thickness": [20, 400], "temp": [40, 150]}})
AGV = StationSpec(id="AGV-01", limits={"cap.transfer": {}})

MIX_STEP = {"name": "匀浆", "cap": "cap.mix", "params": {"temp": 25, "rpm": 2000}, "dur": 60}
COAT_STEP = {"name": "涂布", "cap": "cap.coat", "params": {"thickness": 180, "temp": 110}, "dur": 40}


def context(**kwargs) -> SchedulingContext:
    defaults = dict(
        stations=[MIXER_A, MIXER_B, COATER, AGV],
        busy={},
        held_station_ids=set(),
        transfer_station_ids=["AGV-01"],
        transfer_min=10,
        clean_min=10,
    )
    return SchedulingContext(**{**defaults, **kwargs})


def test_station_switch_books_transfer_and_clean_buffer():
    allocations = plan_steps([MIX_STEP, COAT_STEP], T0, context())

    kinds = [(a.step_index, a.station_id, a.kind) for a in allocations]
    assert (0, "MIX-A", WORK) in kinds
    assert (1, "AGV-01", TRANSFER) in kinds, "工位切换必须占用转运资源"
    assert (0, "MIX-A", CLEAN) in kinds, "工位用后挂清洗缓冲参与冲突检测"

    coat = next(a for a in allocations if a.step_index == 1 and a.kind == WORK)
    mix = next(a for a in allocations if a.step_index == 0 and a.kind == WORK)
    assert coat.starts_at >= mix.ends_at + timedelta(minutes=10)


def test_parallel_station_is_chosen_when_first_is_busy():
    busy = {"MIX-A": [Interval(T0, T0 + timedelta(hours=3))]}
    allocations = plan_steps([MIX_STEP], T0, context(busy=busy))

    work = next(a for a in allocations if a.kind == WORK)
    assert work.station_id == "MIX-B"
    assert work.starts_at == T0, "另一台空闲工位可以立即开始，不必等待"


def test_hard_window_violation_is_rejected_with_reason():
    hard_coat = {**COAT_STEP, "hard": {"from": "匀浆结束", "maxGapMin": 5}}
    busy = {"COAT": [Interval(T0, T0 + timedelta(hours=6))]}

    with pytest.raises(SchedulingError) as error:
        plan_steps([MIX_STEP, hard_coat], T0, context(busy=busy))

    assert error.value.step_index == 1
    assert "硬时限 5 min 无法满足" in error.value.message


def test_held_station_makes_plan_infeasible():
    with pytest.raises(SchedulingError) as error:
        plan_steps([COAT_STEP], T0, context(held_station_ids={"COAT"}))

    assert "释放时间未知" in error.value.message


def test_out_of_limit_parameter_has_no_capable_station():
    hot_step = {**MIX_STEP, "params": {"temp": 200, "rpm": 2000}}

    with pytest.raises(SchedulingError) as error:
        plan_steps([hot_step], T0, context())

    assert "没有可承接工位" in error.value.message


# ---------- 资产容量（评审 R6）----------

CYCLER = StationSpec(id="CYC", channels=2, limits={"cap.test": {}})
CYCLER_B = StationSpec(id="CYC-B", channels=1, limits={"cap.test": {}})
TEST_STEP = {"name": "充放电", "cap": "cap.test", "dur": 10}


def test_asset_capacity_counts_parallel_work_on_the_same_station():
    """工位有 2 个通道、资产容量为 1：同工位已有的作业同样占资产容量，不能再并行一份。"""
    ctx = SchedulingContext(
        stations=[CYCLER], busy={"CYC": [Interval(T0, T0 + timedelta(minutes=30))]},
        station_asset={"CYC": "ASSET"}, asset_capacity={"ASSET": 1}, clean_min=0,
    )
    work = next(a for a in plan_steps([TEST_STEP], T0, ctx) if a.kind == WORK)
    assert work.starts_at == T0 + timedelta(minutes=30)


def test_asset_capacity_counts_every_mapped_station():
    """两个工位映射同一资产（容量 2）：各有一份在跑时，第三份哪个工位都不能接。"""
    busy = {"CYC": [Interval(T0, T0 + timedelta(minutes=30))], "CYC-B": [Interval(T0, T0 + timedelta(minutes=30))]}
    ctx = SchedulingContext(
        stations=[CYCLER, CYCLER_B], busy=busy, station_asset={"CYC": "ASSET", "CYC-B": "ASSET"},
        asset_capacity={"ASSET": 2}, clean_min=0,
    )
    work = next(a for a in plan_steps([TEST_STEP], T0, ctx) if a.kind == WORK)
    assert work.starts_at == T0 + timedelta(minutes=30)


def test_asset_load_is_peak_concurrency_not_the_number_of_windows():
    """窗口里有两段前后错开的占用，同一时刻只占 1 份：容量 2 时新作业可以立即开始。"""
    busy = {"CYC-B": [Interval(T0, T0 + timedelta(minutes=10)),
                      Interval(T0 + timedelta(minutes=15), T0 + timedelta(minutes=25))]}
    ctx = SchedulingContext(
        stations=[CYCLER, CYCLER_B], busy=busy, station_asset={"CYC": "ASSET", "CYC-B": "ASSET"},
        asset_capacity={"ASSET": 2}, clean_min=0,
    )
    work = next(a for a in plan_steps([{**TEST_STEP, "dur": 30}], T0, ctx) if a.kind == WORK)
    assert (work.station_id, work.starts_at) == ("CYC", T0)


def test_maintenance_booking_fills_the_asset():
    ctx = SchedulingContext(
        stations=[CYCLER], station_asset={"CYC": "ASSET"}, asset_capacity={"ASSET": 2}, clean_min=0,
        asset_bookings={"ASSET": [(Interval(T0, T0 + timedelta(hours=1)), 2)]},
    )
    work = next(a for a in plan_steps([TEST_STEP], T0, ctx) if a.kind == WORK)
    assert work.starts_at == T0 + timedelta(hours=1)


# ---------- 完整工艺时间与载具互斥（评审 R7）----------


def test_step_ends_include_the_trailing_wait():
    """设备 60 min 之后还要静置 60 min：批次完成在 120 min，不在设备时间窗结束时。"""
    ends: dict = {}
    allocations = plan_steps(
        [MIX_STEP, {"name": "静置", "kind": "wait", "dur": 60}], T0, context(clean_min=0), step_ends=ends,
    )
    work = next(a for a in allocations if a.kind == WORK)
    assert ends[0] == work.ends_at
    assert ends[1] == work.ends_at + timedelta(minutes=60)
    assert max(ends.values()) == T0 + timedelta(minutes=120)


def test_exclusive_carrier_keeps_parallel_device_branches_apart():
    """同一块板上的两个并行设备分支：不绑载具时可以并行；绑定后排程与运行时一样一个一个来，并排转运。"""
    mix = {**MIX_STEP, "step_id": "mix", "after": []}
    coat = {**COAT_STEP, "step_id": "coat", "after": []}
    free = [a for a in plan_steps([mix, coat], T0, context(clean_min=0)) if a.kind == WORK]
    assert [a.starts_at for a in free] == [T0, T0]

    planned = plan_steps([mix, coat], T0, context(clean_min=0), exclusive_carrier=True)
    first, second = sorted((a for a in planned if a.kind == WORK), key=lambda a: a.starts_at)
    assert second.starts_at >= first.ends_at + timedelta(minutes=10), "板要先从上一台设备搬过来"
    assert any(a.kind == TRANSFER and a.step_index == 1 for a in planned)


# ---------- 协同资源与多载具（评审第三批）----------

ROBOT_A = StationSpec(id="ARM-A", limits={"cap.robot": {}})
ROBOT_B = StationSpec(id="ARM-B", limits={"cap.robot": {}})


def test_assist_resource_is_booked_for_the_same_window():
    """主设备与协同资源同起同止：机械臂在整个匀浆期间都被占着。"""
    step = {**MIX_STEP, "assist": ["cap.robot"]}
    allocations = plan_steps([step], T0, context(stations=[MIXER_A, ROBOT_A], clean_min=0))
    work = next(a for a in allocations if a.kind == WORK)
    assist = next(a for a in allocations if a.kind == "assist")
    assert (assist.station_id, assist.starts_at, assist.ends_at) == ("ARM-A", work.starts_at, work.ends_at)


def test_busy_assist_resource_delays_the_device_step():
    """唯一的机械臂在忙：主设备空着也要等，凑齐了一起开工，不会只占到一半。"""
    step = {**MIX_STEP, "assist": ["cap.robot"]}
    busy = {"ARM-A": [Interval(T0, T0 + timedelta(minutes=45))]}
    allocations = plan_steps([step], T0, context(stations=[MIXER_A, ROBOT_A], busy=busy, clean_min=0))
    work = next(a for a in allocations if a.kind == WORK)
    assert work.starts_at == T0 + timedelta(minutes=45)


def test_second_assist_station_is_used_when_the_first_is_busy():
    step = {**MIX_STEP, "assist": ["cap.robot"]}
    busy = {"ARM-A": [Interval(T0, T0 + timedelta(hours=2))]}
    allocations = plan_steps([step], T0, context(stations=[MIXER_A, ROBOT_A, ROBOT_B], busy=busy, clean_min=0))
    assist = next(a for a in allocations if a.kind == "assist")
    assert (assist.station_id, assist.starts_at) == ("ARM-B", T0)


def test_missing_assist_capability_fails_with_reason():
    step = {**MIX_STEP, "assist": ["cap.robot"]}
    with pytest.raises(SchedulingError) as error:
        plan_steps([step], T0, context(stations=[MIXER_A], clean_min=0))
    assert "协同资源 cap.robot 没有可用工位" in error.value.message


def test_steps_on_different_plates_are_not_serialized():
    """两块板各自互斥：同一块板上的步骤一个接一个，不同板上的步骤可以并行。"""
    mix = {**MIX_STEP, "step_id": "mix", "after": []}
    coat = {**COAT_STEP, "step_id": "coat", "after": [], "labware": "B"}
    planned = plan_steps([mix, coat], T0, context(clean_min=0), exclusive_carrier={"", "B"})
    starts = sorted(a.starts_at for a in planned if a.kind == WORK)
    assert starts == [T0, T0]
    same = plan_steps([mix, {**coat, "labware": ""}], T0, context(clean_min=0), exclusive_carrier={""})
    first, second = sorted((a for a in same if a.kind == WORK), key=lambda a: a.starts_at)
    assert second.starts_at >= first.ends_at
