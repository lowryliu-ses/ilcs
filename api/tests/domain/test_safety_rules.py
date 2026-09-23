"""P0 安全判据的回归：联锁、硬时限、校准许可、离线工位。纯函数，不需要数据库。"""
from datetime import datetime, timedelta

import pytest

from app.domain import gate
from app.domain.capability import StationSpec
from app.domain.resources import AssetSpec, CalibrationSpec, Window, calibration_blockers
from app.domain.scheduling import Interval, SchedulingContext, SchedulingError, plan_steps

T0 = datetime(2026, 9, 20, 8, 0)


# ---------- 执行门 ----------


def _health(**overrides) -> gate.AdapterHealth:
    values = dict(
        station_id="ST-01", connected=True, site_interlock=False, last_heartbeat=T0, enabled=True,
    )
    values.update(overrides)
    return gate.AdapterHealth(**values)


def test_site_interlock_closes_gate_even_when_adapter_is_disabled():
    """停用或暂不接受指令的设备报联锁，联锁照样生效——它是现场事实，不是设备配置。"""
    state = gate.evaluate([_health(site_interlock=True, enabled=False)], T0, 300, 5)
    assert not state["open"]
    assert "公共保护联锁" in state["reasons"][0]


def test_disabled_adapter_heartbeat_does_not_close_gate():
    stale = T0 - timedelta(hours=2)
    state = gate.evaluate([_health(enabled=False, connected=False, last_heartbeat=stale)], T0, 300, 5)
    assert state["open"]


# ---------- 排程 ----------

MIXER = StationSpec(id="MIX-A", limits={"cap.mix": {"rpm": [0, 3000]}})
COATER = StationSpec(id="COAT", limits={"cap.coat": {"temp": [40, 150]}})
AGV = StationSpec(id="AGV-01", limits={"cap.transfer": {}})

MIX = {"name": "匀浆", "cap": "cap.mix", "params": {"rpm": 2000}, "dur": 60}
COAT_WITHIN_12 = {
    "name": "涂布", "cap": "cap.coat", "params": {"temp": 110}, "dur": 40,
    "hard": {"maxGapMin": 12},
}


def test_hard_window_is_checked_after_transfer_delay():
    """转运车忙时开工被往后推，硬时限必须按推迟后的开工时间判定。"""
    context = SchedulingContext(
        stations=[MIXER, COATER, AGV],
        # AGV 在匀浆结束（09:00）后还要忙到 09:30
        busy={"AGV-01": [Interval(T0, T0 + timedelta(minutes=90))]},
        transfer_station_ids=["AGV-01"],
        transfer_min=10,
        clean_min=0,
    )
    with pytest.raises(SchedulingError) as error:
        plan_steps([MIX, COAT_WITHIN_12], T0, context)
    assert error.value.step_index == 1
    assert "硬时限" in error.value.message


def test_hard_window_passes_when_transfer_fits():
    context = SchedulingContext(
        stations=[MIXER, COATER, AGV], transfer_station_ids=["AGV-01"], transfer_min=10, clean_min=0,
    )
    allocations = plan_steps([MIX, COAT_WITHIN_12], T0, context)
    coat = next(a for a in allocations if a.station_id == "COAT")
    assert coat.starts_at - (T0 + timedelta(minutes=60)) <= timedelta(minutes=12)


def test_offline_station_is_not_scheduled():
    offline = StationSpec(id="MIX-A", status="offline", limits=MIXER.limits)
    context = SchedulingContext(stations=[offline])
    with pytest.raises(SchedulingError) as error:
        plan_steps([MIX], T0, context)
    assert "离线" in error.value.message


# ---------- 校准许可 ----------

WINDOW = Window(T0, T0 + timedelta(hours=1))


def _asset(*calibrations: CalibrationSpec, state: str = "active") -> AssetSpec:
    return AssetSpec(asset_id="A-1", name="EQ-001 涂布机", state=state, calibrations=calibrations)


def _pass(days_ago: int, valid_days: int | None = 365) -> CalibrationSpec:
    start = T0 - timedelta(days=days_ago)
    return CalibrationSpec(
        effective_from=start,
        expires_at=start + timedelta(days=valid_days) if valid_days is not None else None,
    )


def test_later_failed_calibration_overrides_earlier_pass():
    """周期中复校不合格：旧证书仍在有效期内也不再作数。"""
    failed = CalibrationSpec(effective_from=T0 - timedelta(days=3), expires_at=None, result="fail")
    blockers = calibration_blockers(_asset(_pass(100), failed), "", WINDOW)
    assert blockers and "不合格" in blockers[0]


def test_pass_after_failure_restores_permission():
    failed = CalibrationSpec(effective_from=T0 - timedelta(days=10), expires_at=None, result="fail")
    assert calibration_blockers(_asset(failed, _pass(2)), "", WINDOW) == []


def test_pass_without_expiry_is_not_a_permission():
    blockers = calibration_blockers(_asset(_pass(10, valid_days=None)), "", WINDOW)
    assert blockers and "没有有效期" in blockers[0]


def test_failure_registered_inside_the_window_blocks():
    failed = CalibrationSpec(effective_from=T0 + timedelta(minutes=20), expires_at=None, result="fail")
    blockers = calibration_blockers(_asset(_pass(10), failed), "", WINDOW)
    assert blockers and "执行区间内" in blockers[0]


def test_asset_under_maintenance_blocks_execution():
    blockers = calibration_blockers(_asset(_pass(10), state="maintenance"), "", WINDOW)
    assert blockers == ["EQ-001 涂布机 处于维护状态"]


# ---------- 孔板与矩阵物料（A3） ----------


def test_96_well_plate_uses_standard_8_by_12_grid():
    from app.domain.matrix import well_grid

    wells = well_grid(96)
    assert len(set(wells)) == 96
    assert wells[0] == "A1" and wells[11] == "A12" and wells[-1] == "H12"
    assert well_grid(48)[-1] == "H6", "48 孔及以下沿用历史编号，已有批次的孔位不变"
    with pytest.raises(ValueError):
        well_grid(97)


def test_matrix_material_demand_counts_every_condition():
    """2×3 全因子：第一个因子的每个水平出现在 3 个条件里。"""
    from app.domain.matrix import material_demand

    factors = [
        {"name": "注液量", "levels": [1, 2], "material": {"name": "电解液", "per": 1, "unit": "mL"}},
        {"name": "添加剂", "levels": [0, 1, 2]},
    ]
    assert material_demand(factors, repeats=2)[0]["qty"] == (1 + 2) * 3 * 2


# ---------- 等待节点（A4） ----------


def test_business_event_wait_is_rejected_until_an_emitter_exists():
    from app.domain.steps import wait_issues

    issues = wait_issues({"kind": "wait", "wait_for": {"mode": "event", "event": "lims.ready"}})
    assert issues and "暂不支持" in issues[0]
    assert wait_issues({"kind": "wait", "dur": 3, "wait_for": {"mode": "duration"}}) == []


# ---------- 多通道设备（C 批 ③） ----------


def test_multi_channel_station_runs_windows_in_parallel():
    """8 通道充放电柜：同一时刻最多 8 个时间窗，第 9 个等最早结束的那个。"""
    from app.domain.scheduling import earliest_free

    cycler = StationSpec(id="CYC", channels=2, positions=32, limits={"cap.test": {}})
    long = timedelta(hours=10)
    context = SchedulingContext(
        stations=[cycler],
        busy={"CYC": [Interval(T0, T0 + long)]},
    )
    assert earliest_free(context, "CYC", T0, timedelta(hours=1)) == T0, "还有一个空闲通道，立即开始"
    context.busy["CYC"].append(Interval(T0, T0 + timedelta(hours=3)))
    assert earliest_free(context, "CYC", T0, timedelta(hours=1)) == T0 + timedelta(hours=3), "两个通道都占着，等先结束的那个"


def test_single_position_station_still_serializes():
    from app.domain.scheduling import earliest_free

    station = StationSpec(id="COAT", limits={"cap.coat": {}})
    context = SchedulingContext(stations=[station], busy={"COAT": [Interval(T0, T0 + timedelta(hours=1))]})
    assert earliest_free(context, "COAT", T0, timedelta(minutes=30)) == T0 + timedelta(hours=1)
