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
