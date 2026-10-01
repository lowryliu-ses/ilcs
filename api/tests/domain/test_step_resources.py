"""人工步骤占工位、等待期间样本留在设备里、人工步骤的资质要求：校验与排程。纯函数，不碰数据库。"""
from datetime import datetime, timedelta

import pytest

from app.domain.capability import StationSpec
from app.domain.recipe_rules import validate_steps
from app.domain.scheduling import CLEAN, WORK, Interval, SchedulingContext, SchedulingError, plan_steps
from app.domain.steps import qualification_issues, resource_issues

T0 = datetime(2026, 9, 20, 8, 0)

OVEN_A = StationSpec(id="OVEN-A", limits={"cap.dry": {"temp": [20, 200]}})
OVEN_B = StationSpec(id="OVEN-B", limits={"cap.dry": {"temp": [20, 120]}})
GLOVEBOX = StationSpec(id="GB-01", limits={"cap.assemble": {}}, clean=False, status="offline")

DRY = {"step_id": "s01", "name": "烘干", "cap": "cap.dry", "params": {"temp": 150}, "dur": 60}
COOL = {"step_id": "s02", "name": "炉内冷却", "kind": "wait", "dur": 30, "resource": {"holds_station": True}}
CAPS = {"cap.dry": {"name": "烘干", "params": {"temp": "温度"}}, "cap.assemble": {"name": "组装", "params": {}}}


def context(**kwargs) -> SchedulingContext:
    defaults = dict(stations=[OVEN_A, OVEN_B, GLOVEBOX], busy={}, held_station_ids=set(),
                    transfer_station_ids=[], transfer_min=10, clean_min=10)
    return SchedulingContext(**{**defaults, **kwargs})


def manual(**resource):
    return {"step_id": "s09", "name": "手套箱内人工装样", "kind": "manual", "dur": 20,
            "form": [{"key": "ok", "label": "已装样", "type": "bool"}], "resource": resource}


def test_manual_step_occupies_the_named_station():
    busy = {"GB-01": [Interval(T0, T0 + timedelta(minutes=45))]}
    allocations = plan_steps([manual(station="GB-01")], T0, context(busy=busy))
    work = [a for a in allocations if a.kind == WORK]
    assert [(a.station_id, a.starts_at) for a in work] == [("GB-01", T0 + timedelta(minutes=45))]
    # 人工操作不给设备下发指令：设备离线、未清洗都不挡
    assert GLOVEBOX.status == "offline" and not GLOVEBOX.clean


def test_manual_step_by_capability_takes_any_implementing_station_but_not_a_held_one():
    allocations = plan_steps([manual(capability="cap.dry")], T0, context(held_station_ids={"OVEN-A"}))
    assert [a.station_id for a in allocations if a.kind == WORK] == ["OVEN-B"]
    with pytest.raises(SchedulingError):
        plan_steps([manual(capability="cap.dry")], T0, context(held_station_ids={"OVEN-A", "OVEN-B"}))


def test_holding_wait_stays_on_the_device_and_cleaning_waits_for_it():
    allocations = plan_steps([DRY, COOL], T0, context())
    dry = next(a for a in allocations if a.step_index == 0 and a.kind == WORK)
    cool = next(a for a in allocations if a.step_index == 1 and a.kind == WORK)
    clean = next(a for a in allocations if a.kind == CLEAN)
    assert cool.station_id == dry.station_id == "OVEN-A"
    assert cool.starts_at == dry.ends_at and cool.ends_at == dry.ends_at + timedelta(minutes=30)
    assert clean.starts_at == cool.ends_at, "清洗排在样本取出之后"


def test_device_step_looks_for_room_for_the_hold_too():
    """OVEN-A 在烘干结束后 10 min 就被别的批次占了：放不下 30 min 的炉内冷却，整段换到 OVEN-B。"""
    busy = {"OVEN-A": [Interval(T0 + timedelta(minutes=70), T0 + timedelta(hours=5))]}
    allocations = plan_steps([{**DRY, "params": {"temp": 100}}, COOL], T0, context(busy=busy))
    work = {a.step_index: a for a in allocations if a.kind == WORK}
    assert work[0].station_id == work[1].station_id == "OVEN-B"
    assert work[0].starts_at == T0


def test_holding_wait_after_a_frozen_device_step_must_start_right_away():
    known = {0: T0 + timedelta(minutes=60)}
    where = {0: "OVEN-A"}
    allocations = plan_steps([DRY, COOL], T0, context(), first_index=1, known_ends=known, known_where=where)
    cool = next(a for a in allocations if a.kind == WORK)
    assert (cool.station_id, cool.starts_at) == ("OVEN-A", T0 + timedelta(minutes=60))

    busy = {"OVEN-A": [Interval(T0 + timedelta(minutes=70), T0 + timedelta(hours=2))]}
    with pytest.raises(SchedulingError) as error:
        plan_steps([DRY, COOL], T0, context(busy=busy), first_index=1, known_ends=known, known_where=where)
    assert "留在 OVEN-A 里" in str(error.value)


def test_resource_and_qualification_shapes():
    assert resource_issues(manual(station="GB-01")) == []
    assert "要么指定一台" in resource_issues(manual(station="GB-01", capability="cap.dry"))[0]
    assert "没写占哪台工位" in resource_issues({**manual(), "resource": {"note": "x"}})[-1]
    assert resource_issues(COOL) == []
    assert "只认 holds_station" in resource_issues({**COOL, "resource": {"station": "OVEN-A"}})[0]
    assert "不占工位" in resource_issues({"name": "审核", "kind": "review", "resource": {"station": "X"}})[0]

    step = manual()
    assert qualification_issues({**step, "qualification": {"sop": "SOP-ELY-01"}}) == []
    assert qualification_issues({**step, "qualification": {"safety": "HF-01", "sop": ""}}) == [
        "SOP资质编号必须是非空文字"]
    assert "都没写" in qualification_issues({**step, "qualification": {"sop": " "}})[-1]
    assert "不认 cap" in qualification_issues({**step, "qualification": {"cap": "x", "sop": "S"}})[0]
    assert "只有人工步骤" in qualification_issues({**DRY, "qualification": {"sop": "S"}})[0]


def test_validation_of_holding_waits_and_named_stations():
    stations = [OVEN_A, OVEN_B, GLOVEBOX]
    rows = validate_steps([DRY, COOL], stations, CAPS)
    assert rows[1]["ok"] and rows[1]["fits"] == ["OVEN-A"], "占的是能承接前驱的工位"

    lonely = validate_steps([{**COOL, "step_id": "s01"}], stations, CAPS)
    assert not lonely[0]["ok"]
    assert any("前驱只能有一个" in issue for issue in lonely[0]["issues"])

    after_manual = validate_steps([manual(station="GB-01"), {**COOL, "after": ["s09"]}], stations, CAPS)
    assert any("不是设备步骤" in issue for issue in after_manual[1]["issues"])

    twins = [DRY, COOL, {**COOL, "step_id": "s03", "after": ["s01"]}]
    twins[1] = {**COOL, "after": ["s01"]}
    rows = validate_steps(twins, stations, CAPS)
    assert any("样本只能在一处" in issue for issue in rows[1]["issues"] + rows[2]["issues"])

    missing = validate_steps([manual(station="GB-99")], stations, CAPS)
    assert not missing[0]["ok"] and missing[0]["fits"] == []
    assert "指定占用的工位 GB-99 不存在或已停用" in missing[0]["blockers"]
    by_cap = validate_steps([manual(capability="cap.dry")], stations, CAPS)
    assert by_cap[0]["ok"] and by_cap[0]["fits"] == ["OVEN-A", "OVEN-B"]
