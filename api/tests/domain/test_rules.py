"""开跑七项、恢复策略、条件矩阵与统计：全部是可单测的纯规则。"""
from datetime import datetime, timedelta

from app.domain import matrix, preflight, recovery, statistics
from app.domain.gate import AdapterHealth, evaluate as evaluate_gate

NOW = datetime(2026, 9, 20, 10, 0)


def preflight_context(**overrides) -> preflight.PreflightContext:
    defaults = dict(
        recipe_state="released", recipe_risk="RA-201 v3", snapshot_version="1.2.0", released_version="1.2.0",
        steps_total=6, steps_needing_station=6, steps_allocated=6,
        resource_checks=[
            {"step_index": 0, "step_id": "s01", "step_name": "匀浆", "applicable": True,
             "ok": True, "reasons": []}
        ],
        reservations=[{"lot_id": "LOT-NMP-2601", "qty": 0.6, "unit": "L", "release": "已放行"}],
        bom_items=[{"material": "NMP", "qty": 0.6, "unit": "L"}], material_steps=6,
        bom_satisfied=True, expired_lots=[],
        first_station={"id": "ST-01-A", "clean": True, "status": "idle", "cal_due": "2026-12-02"},
        station_alarm_active=False, gate_reasons=[], planned_start=NOW + timedelta(minutes=5), now=NOW,
        has_control_permission=True, role_name="操作员", manual_review_done=True,
        qualification_required=True, qualification_blockers=[],
    )
    return preflight.PreflightContext(**{**defaults, **overrides})


def test_all_checks_pass_on_a_clean_batch():
    checks = preflight.evaluate(preflight_context())

    assert len(checks) == 9
    assert not preflight.blocked(checks)
    assert preflight.summary(checks)["not_applicable"] == 0


def test_pure_manual_flow_marks_device_checks_not_applicable():
    """空 BOM、无设备节点的合法流程不能被物料项与工位项误拦。"""
    checks = {
        c.key: c
        for c in preflight.evaluate(
            preflight_context(
                steps_needing_station=0, steps_allocated=0, resource_checks=[], reservations=[],
                bom_items=[], material_steps=0, bom_satisfied=False, first_station=None,
                planned_start=None, qualification_required=False,
            )
        )
    }

    assert checks["material"].state == preflight.NOT_APPLICABLE
    assert checks["allocation"].state == preflight.NOT_APPLICABLE
    assert checks["resource"].state == preflight.NOT_APPLICABLE
    assert checks["station"].state == preflight.NOT_APPLICABLE
    assert not preflight.blocked(list(checks.values()))


def test_expired_lot_blocks_material_check():
    checks = preflight.evaluate(preflight_context(expired_lots=["LOT-1 已超过有效截止 2026-01-01"]))

    assert [c.key for c in preflight.blocked(checks)] == ["material"]


def test_calibration_expiring_inside_window_blocks_resource_check():
    """AC-11：第二个设备步骤的校准在预计执行区间内失效。"""
    checks = preflight.evaluate(
        preflight_context(
            resource_checks=[
                {"step_index": 0, "step_id": "s01", "step_name": "匀浆", "applicable": True,
                 "ok": True, "reasons": []},
                {"step_index": 1, "step_id": "s02", "step_name": "涂布", "applicable": True,
                 "ok": False, "reasons": ["ST-02 的校准在 2026-09-20T11:00 到期，早于该步骤计划结束时间"]},
            ]
        )
    )

    blocked = preflight.blocked(checks)
    assert [c.key for c in blocked] == ["resource"]
    assert "第 2 步" in blocked[0].detail


def test_expired_qualification_blocks_dispatch():
    """AC-10：资质在分配后到期，实际开始前仍要被拦住。"""
    checks = preflight.evaluate(
        preflight_context(qualification_blockers=["李操作员 的设备操作「涂布烘干」资质已于 2026-09-19 到期"])
    )

    assert [c.key for c in preflight.blocked(checks)] == ["qualification"]


def test_unreleased_lot_blocks_dispatch():
    checks = preflight.evaluate(
        preflight_context(reservations=[{"lot_id": "L1", "qty": 1, "unit": "g", "release": "待复验"}])
    )

    assert [c.key for c in preflight.blocked(checks)] == ["material"]


def test_closed_gate_and_expired_schedule_block_dispatch():
    checks = preflight.evaluate(
        preflight_context(gate_reasons=["ST-03 遥测数据超时 7 min"], planned_start=NOW - timedelta(minutes=90))
    )

    assert {c.key for c in preflight.blocked(checks)} == {"gate", "schedule"}


def test_missing_manual_review_blocks_authority_check():
    checks = preflight.evaluate(preflight_context(manual_review_done=False))

    assert [c.key for c in preflight.blocked(checks)] == ["authority"]


def recovery_context(**overrides) -> recovery.RecoveryContext:
    defaults = dict(
        capability_id="cap.coat", capability_name="涂布烘干",
        recovery={"maxHoldMin": 20, "pausable": True, "retryable": False,
                  "sideEffect": "停机点留下厚度突变，该段极片需剔除", "verify": ["已涂布长度"]},
        step_name="涂布烘干", step_duration_min=40, elapsed_min=12, held_min=8,
        downstream_steps=[{"name": "辊压冲切", "hard": {"maxGapMin": 30}}],
        next_station={"id": "ST-04", "status": "idle"},
    )
    return recovery.RecoveryContext(**{**defaults, **overrides})


def test_irreversible_step_offers_resume_but_not_retry():
    context = recovery_context()
    rows = recovery.preconditions(context)
    options = {o.id: o for o in recovery.options(context, rows)}

    assert options["resume"].allowed
    assert not options["retry"].allowed
    assert "厚度突变" in options["retry"].reason
    assert options["abort"].allowed, "终止始终可用"


def test_unresolved_alarm_blocks_everything_except_abort():
    context = recovery_context(unresolved_alarm_ids=["A-1040"])
    rows = recovery.preconditions(context)
    options = {o.id: o for o in recovery.options(context, rows)}

    assert [r["ok"] for r in rows][0] is False
    assert "确认报警不能解除该阻断" in rows[0]["detail"]
    assert not options["resume"].allowed and not options["retry"].allowed
    assert options["abort"].allowed


def test_exceeded_hold_window_blocks_resume():
    context = recovery_context(held_min=45)
    rows = recovery.preconditions(context)
    options = {o.id: o for o in recovery.options(context, rows)}

    assert not options["resume"].allowed
    assert "已超时" in next(r for r in rows if r["key"] == "hold_window")["detail"]


def test_seeded_randomized_layout_is_reproducible():
    factors = [{"name": "导电剂比例", "unit": "%", "levels": [1, 2, 3, 4]},
               {"name": "粘结剂比例", "unit": "%", "levels": [2, 3]}]

    first = matrix.layout(factors, {"cond": [2, 2]}, 3, 24, "randomized", 20260910)
    second = matrix.layout(factors, {"cond": [2, 2]}, 3, 24, "randomized", 20260910)

    assert [a.well for a in first] == [a.well for a in second]
    assert len(first) == 24
    assert len({a.well for a in first}) == 24, "孔位不能重复"
    assert any(a.is_control for a in first)


def test_statistics_exclude_non_valid_samples():
    samples = [
        {"id": "S1", "well": "A1", "repeat": 1, "condition_group": "C01", "condition_label": "1%",
         "quality": "valid", "metrics": {"discharge_capacity": 200, "areal_density": 15.0}},
        {"id": "S2", "well": "A2", "repeat": 2, "condition_group": "C01", "condition_label": "1%",
         "quality": "valid", "metrics": {"discharge_capacity": 210, "areal_density": 15.2}},
        {"id": "S3", "well": "A3", "repeat": 3, "condition_group": "C01", "condition_label": "1%",
         "quality": "invalid", "metrics": {"discharge_capacity": 90, "areal_density": 9.0}},
    ]

    groups = statistics.group_statistics(samples)

    assert groups[0]["n_valid"] == 2 and groups[0]["n_total"] == 3
    assert groups[0]["mean"] == 205
    assert round(groups[0]["cv_pct"], 3) == round(statistics.cv_percent([200, 210]), 3)


def test_stale_heartbeat_closes_gate():
    adapters = [
        AdapterHealth("ST-01-A", connected=True, site_interlock=False, last_heartbeat=NOW),
        AdapterHealth("ST-03", connected=True, site_interlock=False, last_heartbeat=NOW - timedelta(minutes=7)),
    ]

    state = evaluate_gate(adapters, NOW, stale_sec=300, degraded_sec=5)

    assert not state["open"]
    assert "ST-03 遥测数据超时 7 min" in state["reasons"]
