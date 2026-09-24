"""环境条件、覆盖全部工位的开跑检查、人员预占、批次完成后的残余释放。"""
from datetime import timedelta

from app.core.clock import now
from app.models import Allocation, EnvironmentReading, Person, PersonBooking, Reservation, SlotOccupancy, Station
from test_graph_workflow import _graph_batch, _run


def _checks(operator, batch_id):
    body = operator.get(f"/api/batches/{batch_id}/preflight?manual_review=true").json()
    return {row["key"]: row for row in body["checks"]}


def _schedule(operator, batch_id):
    scheduled = operator.post(f"/api/batches/{batch_id}/schedule", {})
    assert scheduled.status_code == 200, scheduled.text


def _dispatch(operator, batch_id):
    return operator.post(f"/api/batches/{batch_id}/dispatch",
                         {"manual_review": True, "signature_id": operator.sign("批准执行", target=batch_id)})


def test_environment_requirements_gate_preflight_and_each_dispatch(operator, db, reset_runtime, executor):
    zone = f"GB-{now():%H%M%S%f}"
    requirement = [{"metric": "h2o_ppm", "max": 0.1, "zone": zone}]

    def with_env(steps):
        # 第二个设备步骤要求手套箱水含量 ≤ 0.1 ppm
        steps[1] = {**steps[1], "environment": requirement}
        return steps

    batch_id = _graph_batch(operator, db, with_env)
    _schedule(operator, batch_id)
    checks = _checks(operator, batch_id)
    assert checks["environment"]["state"] == "blocked" and "没有水含量读数" in checks["environment"]["detail"]

    recorded = operator.post("/api/environment/readings", {"zone": zone, "metric": "h2o_ppm", "value": 0.05})
    assert recorded.status_code == 201, recorded.text
    assert _checks(operator, batch_id)["environment"]["state"] == "pass"

    assert _dispatch(operator, batch_id).status_code == 200
    # 第一步执行期间手套箱水含量超标：第二步投递前再核对，不投递并挂起
    operator.post("/api/environment/readings", {"zone": zone, "metric": "h2o_ppm", "value": 0.8})
    detail = _run(operator, batch_id, executor)
    assert detail["state"] != "done"
    assert "环境条件不满足" in (detail["failure_reason"] or ""), detail["failure_reason"]

    stale = db.query(EnvironmentReading).filter(EnvironmentReading.zone == zone).all()
    for row in stale:
        row.measured_at = now() - timedelta(hours=2)
    db.commit()
    latest = operator.get(f"/api/environment/readings?zone={zone}").json()
    assert latest[0]["stale"] is True


def test_preflight_covers_every_station_not_only_the_first(operator, db, reset_runtime):
    batch_id = _graph_batch(operator, db, lambda steps: steps)
    _schedule(operator, batch_id)
    stations = [row.station_id for row in db.query(Allocation).filter(Allocation.batch_id == batch_id, Allocation.kind == "work")
                .order_by(Allocation.step_index).all()]
    later = next(station for station in stations if station != stations[0])
    station = db.get(Station, later)
    try:
        station.status = "offline"
        db.commit()
        check = _checks(operator, batch_id)["stations"]
        assert check["state"] == "blocked" and later in check["detail"] and "离线" in check["detail"]
    finally:
        station.status = "idle"
        db.commit()


def test_manual_steps_reserve_the_executor_and_leave_blocks(operator, admin, db, reset_runtime):
    def with_manual(steps):
        manual = {"step_id": "m1", "name": "人工核对", "kind": "manual", "cap": "", "params": {}, "dur": 20,
                  "form": [{"key": "ok", "label": "核对", "type": "bool"}]}
        return [manual, *steps]

    batch_id = _graph_batch(operator, db, with_manual)
    _schedule(operator, batch_id)
    db.expire_all()
    booking = db.query(PersonBooking).filter(PersonBooking.batch_id == batch_id).one()
    person = db.get(Person, booking.person_id)
    assert person.name == "操作员" and booking.step_id == "m1"
    assert _checks(operator, batch_id)["personnel"]["state"] == "pass"

    leave = admin.post(f"/api/people/{person.id}/bookings", {
        "kind": "leave", "starts_at": (booking.starts_at - timedelta(minutes=5)).isoformat(),
        "ends_at": (booking.ends_at + timedelta(minutes=5)).isoformat(), "reason": "病假",
    })
    assert leave.status_code == 201, leave.text
    try:
        check = _checks(operator, batch_id)["personnel"]
        assert check["state"] == "blocked" and "请假" in check["detail"]
    finally:
        assert admin.post(f"/api/people/bookings/{leave.json()['id']}/cancel").status_code == 200
    assert _checks(operator, batch_id)["personnel"]["state"] == "pass"


def test_completion_releases_leftover_windows_materials_slots_and_people(operator, db, reset_runtime, executor):
    batch_id = _graph_batch(operator, db, lambda steps: steps)
    _schedule(operator, batch_id)
    # 人为留一个不会用到的未来时间窗，模拟跳过的步骤留下的预约
    first = db.query(Allocation).filter(Allocation.batch_id == batch_id).first()
    db.add(Allocation(batch_id=batch_id, step_index=first.step_index, station_id=first.station_id, asset_id=first.asset_id,
                      starts_at=now() + timedelta(days=2), ends_at=now() + timedelta(days=2, hours=1), kind="clean"))
    db.commit()
    assert _dispatch(operator, batch_id).status_code == 200
    detail = _run(operator, batch_id, executor)
    assert detail["state"] == "done", detail["failure_reason"]
    db.expire_all()
    assert not db.query(Allocation).filter(Allocation.batch_id == batch_id, Allocation.starts_at > now()).all()
    assert all(row.state == "released" for row in db.query(Reservation).filter(Reservation.batch_id == batch_id).all())
    slots = db.query(SlotOccupancy).filter(SlotOccupancy.container_id == f"PL-{batch_id.replace('B-', '')}").all()
    assert slots and all(row.released_at is not None for row in slots)
    assert any(row["action"] == "批次收尾释放" for row in detail["audit"])
