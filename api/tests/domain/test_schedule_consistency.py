"""核心链路三次评审（2026-09-28）纯领域回归：清洗缓冲、图前沿、开跑检查的 SOP 阅读确认。

目标行为；对应工作包修复之前标 xfail(strict=True)。
"""
from datetime import datetime, timedelta

import pytest

from app.domain.capability import StationSpec
from app.domain.scheduling import CLEAN, WORK, Interval, SchedulingContext, plan_steps

T = datetime(2026, 9, 28, 8)


def M(minutes: float) -> datetime:
    return T + timedelta(minutes=minutes)


def pending(package: str):
    return pytest.mark.xfail(strict=True, reason=f"{package} 待修")


MIX_A = StationSpec(id="MIX-A", limits={"cap.mix": {}})
MIX_B = StationSpec(id="MIX-B", limits={"cap.mix": {}})
COAT = StationSpec(id="COAT", limits={"cap.coat": {}})
AGV = StationSpec(id="AGV-01", limits={"cap.transfer": {}})


def _context(**extra) -> SchedulingContext:
    return SchedulingContext(
        stations=[MIX_A, MIX_B, COAT, AGV], transfer_station_ids=["AGV-01"], transfer_min=10, clean_min=10, **extra,
    )


def _overlaps(plan, busy: dict[str, list[Interval]]) -> list:
    return [
        row for row in plan
        for other in busy.get(row.station_id, [])
        if row.starts_at < other.end and other.start < row.ends_at
    ]


def _cleaned_after(plan, index: int) -> bool:
    work = next(row for row in plan if row.kind == WORK and row.step_index == index)
    return any(
        row.kind == CLEAN and row.station_id == work.station_id and row.starts_at == work.ends_at for row in plan
    )


# ---------- A06 清洗缓冲（WP11） ----------


def test_clean_buffer_is_never_booked_on_top_of_another_batch():
    busy = {"MIX-A": [Interval(M(60), M(120))]}
    plan = plan_steps(
        [{"name": "mix", "cap": "cap.mix", "dur": 60}, {"name": "coat", "cap": "cap.coat", "dur": 40}],
        T, _context(busy={key: list(value) for key, value in busy.items()}),
    )
    assert not _overlaps(plan, busy), plan
    assert _cleaned_after(plan, 0)


def test_station_left_for_another_station_gets_its_clean_buffer():
    """mix → mix2：两步落在不同工位时，前一个工位必须留清洗；落在同一工位且紧接着开工时不用。"""
    busy = {"MIX-A": [Interval(M(60), M(120))]}
    plan = plan_steps(
        [{"name": "mix", "cap": "cap.mix", "dur": 60}, {"name": "mix2", "cap": "cap.mix", "dur": 60}],
        T, _context(busy={key: list(value) for key, value in busy.items()}),
    )
    assert not _overlaps(plan, busy), plan
    work = {row.step_index: row for row in plan if row.kind == WORK}
    if work[0].station_id != work[1].station_id or work[1].starts_at != work[0].ends_at:
        assert _cleaned_after(plan, 0), plan
    assert _cleaned_after(plan, 1)


def test_clean_decision_follows_the_graph_successor_not_the_list_neighbour():
    """并行 a（cap.mix）与 b（cap.mix）汇合到 c（cap.coat）：a 的图后继在 COAT 上，a 用过的工位要清洗。"""
    steps = [
        {"step_id": "a", "name": "a", "cap": "cap.mix", "dur": 30, "after": []},
        {"step_id": "b", "name": "b", "cap": "cap.mix", "dur": 30, "after": []},
        {"step_id": "c", "name": "c", "cap": "cap.coat", "dur": 30, "after": ["a", "b"]},
    ]
    plan = plan_steps(steps, T, _context())
    assert _cleaned_after(plan, 0), plan
    assert _cleaned_after(plan, 1), plan


# ---------- A17 / A18 图前沿（WP5） ----------


def test_frontier_does_not_open_a_step_while_an_ancestor_is_open():
    """评审驳回后人工记录重做中，中间的设备步骤仍是已完成：评审不能在人工记录重做完之前重开。"""
    from app.domain import graph

    steps = [
        {"step_id": "m", "kind": "manual", "after": []},
        {"step_id": "d", "kind": "device", "cap": "cap.a", "after": ["m"]},
        {"step_id": "r", "kind": "review", "after": ["d"]},
    ]
    to_open, to_prune = graph.frontier(steps, {"m": "ready", "d": "completed"}, {})
    assert to_open == [] and to_prune == []
    to_open, _ = graph.frontier(steps, {"m": "completed", "d": "completed"}, {})
    assert to_open == [2]


def test_flow_state_treats_a_gate_failed_for_rework_as_open_for_evaluation():
    from types import SimpleNamespace as N

    from app.domain import workflow
    from app.services.workflow_service import WorkflowService

    rows = [
        N(step_id="s04", step_index=0, attempt=1, state="superseded", kind="device", conclusion="", form_data={}),
        N(step_id="s04", step_index=0, attempt=2, state="completed", kind="device", conclusion="", form_data={}),
        N(step_id="s05", step_index=1, attempt=1, state="failed", kind="gate", conclusion="",
          form_data={workflow.REEVALUATE: True}),
        N(step_id="s06", step_index=2, attempt=1, state="failed", kind="gate", conclusion="", form_data={}),
    ]
    status, _ = WorkflowService.flow_state(rows)
    assert "s05" not in status, "返工后的关卡等源步骤重做完再评估"
    assert status["s06"] == "failed", "报废判定的失败是终态"


# ---------- A10 阅读确认独立成项（WP8） ----------


def test_sop_ack_is_checked_even_when_no_step_needs_a_qualification():
    from app.domain import preflight

    context = preflight.PreflightContext(
        recipe_state="released", recipe_risk="RA-1", snapshot_version="1.0.0", released_version="1.0.0",
        steps_total=2, steps_needing_station=0, steps_allocated=0, resource_checks=[], reservations=[],
        bom_items=[], material_steps=0, bom_satisfied=True, first_station=None, planned_start=None,
        now=datetime(2026, 9, 28, 1, 0), has_control_permission=True, role_name="操作员", manual_review_done=True,
        qualification_required=False, qualification_blockers=[],
        sop_snapshot={"code": "SOP-X", "version": "v1", "sop_version_id": "x"},
        sop_checks={"label": "SOP SOP-X v1", "warnings": [], "blockers": []},
        sop_ack_blockers=["张三 缺少 SOP-X v1 的阅读确认或等效资质"],
        dependency_blockers=None, other_stations=[], environment_blockers=None,
        personnel_blockers=[], personnel_warnings=[],
    )
    checks = preflight.evaluate(context)
    ack = next(check for check in checks if check.key == "sop_ack")
    assert ack.state == preflight.BLOCKED
    assert "sop_ack" in [check.key for check in preflight.blocked(checks)]


# ---------- A21 校准进排程（WP13） ----------


def test_station_whose_calibration_expires_inside_the_window_is_not_booked():
    from app.domain.scheduling import SchedulingError

    context = _context(calibration_expiry={("MIX-A", "cap.mix"): M(30)})
    plan = plan_steps([{"name": "mix", "cap": "cap.mix", "dur": 60}], T, context)
    work = next(row for row in plan if row.kind == WORK)
    assert work.station_id == "MIX-B", "MIX-A 的校准在时间窗内到期"
    blocked = _context(calibration_invalid={("MIX-A", "cap.mix"): "MIX-A：校准不合格",
                                            ("MIX-B", "cap.mix"): "MIX-B：没有有效的校准记录"})
    with pytest.raises(SchedulingError) as refused:
        plan_steps([{"name": "mix", "cap": "cap.mix", "dur": 60}], T, blocked)
    assert "校准不合格" in refused.value.message and "没有有效的校准记录" in refused.value.message
