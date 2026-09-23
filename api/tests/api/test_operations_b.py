"""B 批回归：执行门按工位、排程纳入维护与容量、按时开工、矩阵参数下发、设备消耗入账、
遥测上报、维护工单。
"""
from datetime import timedelta

from test_execution_safety import async_device  # noqa: F401  （复用 fixture）
from test_failure_paths import running_batch  # noqa: F401


def _adapter(station_id: str, **values) -> None:
    from app.core.db import SessionLocal
    from app.models import Adapter

    with SessionLocal() as db:
        adapter = db.get(Adapter, station_id)
        for key, value in values.items():
            setattr(adapter, key, value)
        db.commit()


# ---------- B6：执行门按工位 ----------


def test_one_station_offline_does_not_stop_the_site(operator, reset_runtime):
    """充放电柜失联：全站门仍开；用到它的批次排不上，并说清是哪台设备。"""
    _adapter("ST-07", connected=False)
    gate = operator.get("/api/gate").json()
    assert gate["open"] is True
    assert "ST-07" in gate["blocked_stations"]

    batch_id = operator.post("/api/batches", {"plan_id": "EP-205-01"}).json()["id"]
    refused = operator.post(f"/api/batches/{batch_id}/schedule", {})
    assert refused.status_code == 409
    assert "ST-07 适配器失联" in refused.json()["detail"]["message"]


def test_dispatch_is_blocked_only_by_its_own_stations(operator, reset_runtime):
    batch_id = operator.post("/api/batches", {"plan_id": "EP-205-01"}).json()["id"]
    assert operator.post(f"/api/batches/{batch_id}/schedule", {}).status_code == 200
    _adapter("ST-07", connected=False)
    blocked = operator.post(
        f"/api/batches/{batch_id}/dispatch",
        {"manual_review": True, "signature_id": operator.sign("批准执行", target=batch_id)},
    )
    assert blocked.status_code == 423
    assert any("ST-07" in reason for reason in blocked.json()["detail"]["reasons"])


# ---------- B3：维护预约与资产容量 ----------


def test_schedule_steps_around_maintenance_booking(admin, operator, reset_runtime):
    from app.core.clock import now

    station = next(s for s in admin.get("/api/stations").json() if s["id"] == "ST-05")
    assert station["asset_id"], "种子里 ST-05 关联了资产"
    ends = now() + timedelta(hours=3)
    booked = operator.post("/api/resource-bookings", {
        "asset_id": station["asset_id"], "kind": "maintenance",
        "starts_at": now().isoformat(), "ends_at": ends.isoformat(), "reason": "真空泵保养",
    })
    assert booked.status_code == 201, booked.text

    batch_id = operator.post("/api/batches", {"plan_id": "EP-205-01"}).json()["id"]
    assert operator.post(f"/api/batches/{batch_id}/schedule", {}).status_code == 200
    allocations = operator.get(f"/api/batches/{batch_id}").json()["allocations"]
    first = min(a["starts_at"] for a in allocations if a["station_id"] == "ST-05")
    assert first >= ends.isoformat(timespec="minutes"), "维护占满整台资产，排程必须绕开"
    operator.post(f"/api/resource-bookings/{booked.json()['id']}/cancel", {"reason": "测试结束"})


def test_asset_in_maintenance_is_not_scheduled(admin, operator, reset_runtime, db):
    from app.models import Asset, Station

    asset_id = db.get(Station, "ST-07").asset_id
    asset = db.get(Asset, asset_id)
    asset.state = "maintenance"
    db.commit()
    try:
        batch_id = operator.post("/api/batches", {"plan_id": "EP-205-01"}).json()["id"]
        refused = operator.post(f"/api/batches/{batch_id}/schedule", {})
        assert refused.status_code == 409
        assert "维护状态" in refused.json()["detail"]["message"]
    finally:
        db.expire_all()
        db.get(Asset, asset_id).state = "active"
        db.commit()


# ---------- B2：按时开工 ----------


def test_dispatch_too_early_is_blocked(operator, reset_runtime):
    from app.core.clock import now

    batch_id = operator.post("/api/batches", {"plan_id": "EP-205-01"}).json()["id"]
    start = (now() + timedelta(hours=2)).isoformat()
    assert operator.post(f"/api/batches/{batch_id}/schedule", {"start_from": start}).status_code == 200
    refused = operator.post(
        f"/api/batches/{batch_id}/dispatch",
        {"manual_review": True, "signature_id": operator.sign("批准执行", target=batch_id)},
    )
    assert refused.status_code == 409
    labels = [row["detail"] for row in refused.json()["detail"]["blocked"]]
    assert any("提前" in label for label in labels)


def test_executor_waits_for_not_before(operator, running_batch, executor, db):
    from app.core.clock import now
    from app.models import Command

    command = db.query(Command).filter(Command.batch_id == running_batch).one()
    command.not_before = now() + timedelta(minutes=30)
    db.commit()
    executor()
    db.expire_all()
    waiting = db.get(Command, command.id)
    assert (waiting.state, waiting.delivery_state) == ("sent", "queued"), "还没到时间窗，动作指令留在队列"


def test_late_start_shifts_downstream_windows(operator, reset_runtime, db):
    from app.models import Allocation, AuditEvent

    batch_id = operator.post("/api/batches", {"plan_id": "EP-205-01"}).json()["id"]
    assert operator.post(f"/api/batches/{batch_id}/schedule", {}).status_code == 200
    # 模拟计划开工已过去 20 分钟（在 30 分钟过期线内）才下发
    for row in db.query(Allocation).filter(Allocation.batch_id == batch_id):
        row.starts_at -= timedelta(minutes=25)
        row.ends_at -= timedelta(minutes=25)
    db.commit()
    before = {(a.step_index, a.kind): a.starts_at for a in db.query(Allocation).filter(Allocation.batch_id == batch_id)}
    dispatched = operator.post(
        f"/api/batches/{batch_id}/dispatch",
        {"manual_review": True, "signature_id": operator.sign("批准执行", target=batch_id)},
    )
    assert dispatched.status_code == 200, dispatched.text
    db.expire_all()
    after = {(a.step_index, a.kind): a.starts_at for a in db.query(Allocation).filter(Allocation.batch_id == batch_id)}
    assert all(after[key] > before[key] for key in before), "晚开工：本批全部时间窗整体顺延"
    assert db.query(AuditEvent).filter(
        AuditEvent.target == batch_id, AuditEvent.action == "按实际进度顺延"
    ).count() == 1


# ---------- B1：矩阵条件参数下发 ----------


def _set_factors(db, factors):
    from app.models import Plan

    plan = db.get(Plan, "EP-205-01")
    original = plan.factors
    plan.factors = factors
    db.commit()
    return original


def test_matrix_levels_are_sent_per_well(operator, researcher, reset_runtime, db, executor):
    original = _set_factors(db, [
        {"name": "FEC 含量", "unit": "%", "levels": [0, 2, 5, 10]},
        {"name": "注液量", "unit": " μL", "levels": [50, 60],
         "material": {"name": "电解液 LP57", "unit": "mL", "per": 0.001},
         "target": {"step_id": "s03", "param": "electrolyte"}},
    ])
    try:
        checks = {row["key"]: row for row in researcher.get("/api/plans/EP-205-01").json()["checks"]}
        assert checks["targets"]["ok"], checks["targets"]

        batch_id = operator.post("/api/batches", {"plan_id": "EP-205-01"}).json()["id"]
        detail = operator.get(f"/api/batches/{batch_id}").json()
        wells = {s["well"]: s["levels"][1] for s in detail["samples"]}
        assert operator.post(f"/api/batches/{batch_id}/schedule", {}).status_code == 200
        assert operator.post(
            f"/api/batches/{batch_id}/dispatch",
            {"manual_review": True, "signature_id": operator.sign("批准执行", target=batch_id)},
        ).status_code == 200
        for _ in range(6):
            executor()
        from app.models import Command

        command = db.query(Command).filter(Command.batch_id == batch_id, Command.step_index == 2).first()
        assert command is not None
        sent = command.params["wells"]
        assert {well: values["electrolyte"] for well, values in sent.items()} == wells, (
            "每个孔位按自己的条件下发注液量"
        )
    finally:
        _set_factors(db, original)


def test_out_of_range_factor_target_fails_plan_check(researcher, reset_runtime, db):
    original = _set_factors(db, [
        {"name": "注液量", "unit": " μL", "levels": [50, 999],
         "target": {"step_id": "s03", "param": "electrolyte"}},
    ])
    try:
        checks = {row["key"]: row for row in researcher.get("/api/plans/EP-205-01").json()["checks"]}
        assert not checks["targets"]["ok"]
        assert "999" in checks["targets"]["detail"]
    finally:
        _set_factors(db, original)


# ---------- B5：设备回报的实际消耗 ----------


def test_device_reported_consumption_is_booked_with_deviation_alarm(
    operator, device, running_batch, async_device, executor,
):
    executor()
    command = operator.get(f"/api/batches/{running_batch}").json()["commands"][0]
    done = device.post(f"/api/runtime/commands/{command['id']}/events", {
        "outcome": "done",
        "delivered": {"materials": [
            {"material": "电解液 LP57", "quantity": 0.9, "unit": "mL"},
            {"material": "不存在的物料", "quantity": 1, "unit": "g"},
        ]},
    })
    assert done.status_code == 200, done.text
    detail = operator.get(f"/api/batches/{running_batch}").json()
    reservation = next(r for r in detail["reservations"] if r["consumed_qty"] != "0.000000")
    assert reservation["consumed_qty"] == "0.900000", "按设备称出来的量入账"

    messages = [a["message"] for a in operator.get("/api/alarms").json() if a["source_id"] == running_batch]
    assert any("偏差" in m and "待复核" in m for m in messages), "偏差超限要报警"
    assert any("没有 不存在的物料 的预留" in m for m in messages), "对不上预留的不入账并报警"

    # 重复回执不重复扣减
    device.post(f"/api/runtime/commands/{command['id']}/events", {"outcome": "done"})
    again = operator.get(f"/api/batches/{running_batch}").json()
    assert next(r for r in again["reservations"] if r["id"] == reservation["id"])["consumed_qty"] == "0.900000"


def test_device_overconsumption_beyond_reservation_is_not_booked(
    operator, device, running_batch, async_device, executor,
):
    """超出预留的消耗不能静默扣账：拒绝入账并报警，由物料管理员核对后处理。"""
    executor()
    command = operator.get(f"/api/batches/{running_batch}").json()["commands"][0]
    device.post(f"/api/runtime/commands/{command['id']}/events", {
        "outcome": "done",
        "delivered": {"materials": [{"material": "电解液 LP57", "quantity": 1.2, "unit": "mL"}]},
    })
    detail = operator.get(f"/api/batches/{running_batch}").json()
    assert all(r["consumed_qty"] == "0.000000" for r in detail["reservations"])
    messages = [a["message"] for a in operator.get("/api/alarms").json() if a["source_id"] == running_batch]
    assert any("消耗被拒" in m and "1.2" in m for m in messages)


# ---------- B4：遥测上报 ----------


def test_telemetry_ingest_dedups_and_rejects_clock_skew(device, reset_runtime, db):
    from app.core.clock import now
    from app.models import Telemetry

    points = [
        {"metric": "chamber_temp", "value": 119.6, "setpoint": 120, "device_ts": now().isoformat()},
        {"metric": "vacuum", "value": 0.8, "device_ts": now().isoformat()},
    ]
    first = device.post("/api/runtime/stations/ST-05/telemetry", {"event_id": "tel-001", "points": points})
    assert first.status_code == 200, first.text
    assert first.json()["accepted"] == 2
    replay = device.post("/api/runtime/stations/ST-05/telemetry", {"event_id": "tel-001", "points": points})
    assert replay.json() | {"batch_id": ""} == {
        "event_id": "tel-001", "accepted": 0, "duplicates": 2, "batch_id": "",
    }
    assert db.query(Telemetry).filter(Telemetry.event_id == "tel-001").count() == 2

    skewed = device.post("/api/runtime/stations/ST-05/telemetry", {
        "event_id": "tel-002",
        "points": [{"metric": "vacuum", "value": 1, "device_ts": (now() + timedelta(hours=1)).isoformat()}],
    })
    assert skewed.status_code == 422
    assert skewed.json()["detail"]["code"] == "device_clock_skew"


def test_old_telemetry_is_purged(db):
    from app.core.clock import now
    from app.models import Telemetry
    from app.services.execution_service import ExecutorLoop

    db.add(Telemetry(station_id="ST-05", metric="old", value=1, device_ts=now() - timedelta(days=400)))
    db.commit()
    assert ExecutorLoop(db).purge_telemetry() >= 1
    assert db.query(Telemetry).filter(Telemetry.metric == "old").count() == 0


# ---------- B7：维护工单 ----------


def _asset_of(db, station_id: str) -> str:
    from app.models import Station

    return db.get(Station, station_id).asset_id


def test_maintenance_order_lifecycle_controls_asset(operator, reset_runtime, db):
    from app.core.clock import now
    from app.models import Asset, ResourceBooking

    asset_id = _asset_of(db, "ST-06")
    created = operator.post("/api/maintenance-orders", {
        "asset_id": asset_id, "kind": "preventive", "title": "手套箱再生",
        "planned_start": (now() + timedelta(days=2)).isoformat(),
        "planned_end": (now() + timedelta(days=2, hours=4)).isoformat(),
    })
    assert created.status_code == 201, created.text
    order = created.json()
    assert db.get(ResourceBooking, order["booking_id"]).kind == "maintenance"

    assert operator.post(f"/api/maintenance-orders/{order['id']}/start").status_code == 200
    db.expire_all()
    assert db.get(Asset, asset_id).state == "maintenance", "开工即转入维护状态，开跑检查据此拦截"

    missing = operator.post(f"/api/maintenance-orders/{order['id']}/complete", {
        "result": "pass", "record": "", "signature_id": operator.sign("完工", target=order["id"]),
    })
    assert missing.status_code == 422

    failed = operator.post(f"/api/maintenance-orders/{order['id']}/complete", {
        "result": "fail", "record": "水氧含量仍超标",
        "signature_id": operator.sign("完工", target=order["id"]),
    })
    assert failed.status_code == 200, failed.text
    db.expire_all()
    assert db.get(Asset, asset_id).state == "maintenance", "不合格不能回到可用"

    retry = operator.post("/api/maintenance-orders", {
        "asset_id": asset_id, "kind": "corrective", "title": "更换净化柱",
        "planned_start": (now() + timedelta(days=3)).isoformat(),
        "planned_end": (now() + timedelta(days=3, hours=2)).isoformat(),
    }).json()
    operator.post(f"/api/maintenance-orders/{retry['id']}/start")
    passed = operator.post(f"/api/maintenance-orders/{retry['id']}/complete", {
        "result": "pass", "record": "更换后水氧 < 0.1 ppm",
        "signature_id": operator.sign("完工", target=retry["id"]),
    })
    assert passed.status_code == 200
    db.expire_all()
    assert db.get(Asset, asset_id).state == "active"
