"""异常引擎与动态重排的运行时。

- 指令没离开系统就被拒（工位失联）：有改派策略时改派到等价工位继续；有重试策略时延时重下；没有策略时照旧转人工。
- 事件中心记下类别、影响面、自动处理与结果；批次恢复后事件写上最终结果。
- 工位失联：策略要求改派时把未开始的时间窗挪走；没有策略时生成重排建议，调度确认后写入，过期的建议拒绝应用。
- 排程模式与交付期拖期；紧急插单生成让路建议。
"""
from datetime import timedelta

from test_graph_workflow import _dispatch, _graph_batch


MIX = {"step_id": "s01", "name": "混匀", "kind": "device", "cap": "cap.mix", "params": {"temp": 25, "rpm": 500}, "dur": 20}


def _mix_flow(steps):
    dry, weigh, assemble, test = steps
    return [MIX, {**{k: v for k, v in test.items() if k != "hard"}}]


def _rule(admin, **payload):
    created = admin.post("/api/exception-rules", {"name": "测试策略", "category": "communication", **payload})
    assert created.status_code == 201, created.text
    return created.json()


def _disable_rules(db):
    from app.models import ExceptionRule

    db.query(ExceptionRule).update({"enabled": False}, synchronize_session=False)
    db.commit()


def _set_adapter(db, station_id, **values):
    from app.models import Adapter

    db.expire_all()
    adapter = db.get(Adapter, station_id)
    for key, value in values.items():
        setattr(adapter, key, value)
    db.commit()


def _run(operator, batch_id, executor, rounds=20, until=("done", "fault", "aborted")):
    import time

    detail = {}
    for _ in range(rounds):
        executor()
        detail = operator.get(f"/api/batches/{batch_id}").json()
        if detail["state"] in until:
            return detail
        time.sleep(0.05)
    return detail


def _first_station(operator, batch_id):
    detail = operator.get(f"/api/batches/{batch_id}").json()
    return next(row["station_id"] for row in detail["allocations"] if row["kind"] == "work" and row["step_index"] == 0)


def test_refused_command_is_rerouted_to_an_equivalent_station(operator, admin, reset_runtime, db, executor):
    _disable_rules(db)
    rule = _rule(admin, action="reroute", params={"max_attempts": 1}, match={"capability": "cap.mix"})
    batch_id = _graph_batch(operator, db, _mix_flow)
    _dispatch(operator, batch_id)
    station = _first_station(operator, batch_id)
    _set_adapter(db, station, connected=False)
    try:
        detail = _run(operator, batch_id, executor)
    finally:
        _set_adapter(db, station, connected=True)
    assert detail["state"] == "done", detail["failure_reason"]
    commands = [row for row in detail["commands"] if row["step_index"] == 0 and row["type"] == "dispatch"]
    assert commands[0]["station_id"] == station and commands[0]["state"] == "not_executed"
    assert commands[-1]["station_id"] != station and commands[-1]["state"] == "done", "改派到了另一台同能力工位"
    events = operator.get(f"/api/exceptions?batch_id={batch_id}").json()
    rerouted = next(row for row in events if row["auto_action"] == "reroute" and row["source_type"] == "command")
    assert rerouted["state"] == "auto_resolved" and rerouted["rule_id"] == rule["id"] and rerouted["never_sent"]
    assert rerouted["category"] == "communication" and batch_id in rerouted["impact"]["batches"]
    _disable_rules(db)


def test_without_a_rule_the_fault_goes_to_a_person_and_recovery_closes_it(operator, reset_runtime, db, executor):
    _disable_rules(db)
    batch_id = _graph_batch(operator, db, _mix_flow)
    _dispatch(operator, batch_id)
    station = _first_station(operator, batch_id)
    _set_adapter(db, station, connected=False)
    try:
        detail = _run(operator, batch_id, executor, rounds=4)
    finally:
        _set_adapter(db, station, connected=True)
    assert detail["state"] == "fault"
    event = next(row for row in operator.get(f"/api/exceptions?batch_id={batch_id}").json() if row["source_type"] == "command")
    assert event["state"] == "open" and "没有匹配的处理策略" in event["decision"]
    # 报警条件还在：续跑与重试不可用，只能安全终止；终止同样给异常写上最终结果
    recovered = operator.post(f"/api/batches/{batch_id}/recover", {
        "strategy": "abort", "verified": True, "signature_id": operator.sign("恢复", target=batch_id),
    })
    assert recovered.status_code == 200, recovered.text
    event = operator.get(f"/api/exceptions/{event['id']}").json()
    assert event["state"] == "resolved" and "终止" in event["final_result"]


def test_retry_rule_redelivers_after_the_cause_clears_and_stops_at_its_limit(operator, admin, reset_runtime, db, executor):
    _disable_rules(db)
    _rule(admin, action="retry", params={"max_attempts": 1, "delay_sec": 0}, match={"capability": "cap.mix"})
    batch_id = _graph_batch(operator, db, _mix_flow)
    _dispatch(operator, batch_id)
    station = _first_station(operator, batch_id)
    _set_adapter(db, station, connected=False)
    try:
        detail = _run(operator, batch_id, executor, rounds=4, until=("fault",))
    finally:
        _set_adapter(db, station, connected=True)
    # 第一次被拒：按策略重下；第二次还被拒：到上限转人工
    events = [row for row in operator.get(f"/api/exceptions?batch_id={batch_id}").json() if row["source_type"] == "command"]
    assert any(row["auto_action"] == "retry" and row["state"] == "auto_resolved" for row in events)
    assert detail["state"] == "fault" and any("上限" in row["decision"] for row in events)
    _disable_rules(db)


def test_exception_center_handling_and_rule_permissions(operator, admin, qa, reset_runtime, db, executor):
    denied = operator.post("/api/exception-rules", {"name": "x", "category": "communication", "action": "hold"})
    assert denied.status_code == 403
    unsafe = admin.post("/api/exception-rules", {"name": "联锁重试", "category": "safety", "action": "retry",
                                                  "params": {"max_attempts": 1}})
    assert unsafe.status_code == 422
    _disable_rules(db)
    batch_id = _graph_batch(operator, db, _mix_flow)
    _dispatch(operator, batch_id)
    station = _first_station(operator, batch_id)
    _set_adapter(db, station, connected=False)
    try:
        _run(operator, batch_id, executor, rounds=3, until=("fault",))
    finally:
        _set_adapter(db, station, connected=True)
    event = next(row for row in operator.get("/api/exceptions?state=open").json() if row["batch_id"] == batch_id)
    claimed = qa.post(f"/api/exceptions/{event['id']}/handle", {"action": "claim"})
    assert claimed.status_code == 200 and claimed.json()["state"] == "manual"
    missing = qa.post(f"/api/exceptions/{event['id']}/handle", {"action": "resolve"})
    assert missing.status_code == 422, "标为已恢复要写处理结果"
    closed = qa.post(f"/api/exceptions/{event['id']}/handle", {"action": "close", "note": "网线松动，已重插"})
    assert closed.status_code == 200 and closed.json()["final_result"] == "网线松动，已重插"
    summary = operator.get("/api/exceptions/summary").json()
    assert summary["total"] >= 1


def test_station_loss_reroutes_future_windows_when_a_rule_says_so(operator, admin, reset_runtime, db):
    from app.core.clock import now
    from app.services.monitoring_service import DeviceMonitor

    _disable_rules(db)
    _rule(admin, action="reroute", match={})
    batch_id = _graph_batch(operator, db, _mix_flow)
    assert operator.post(f"/api/batches/{batch_id}/schedule", {}).status_code == 200
    station = _first_station(operator, batch_id)
    _set_adapter(db, station, connected=False, last_heartbeat=now() - timedelta(minutes=30))
    try:
        DeviceMonitor(db).evaluate_station(station)
        db.commit()
        moved = _first_station(operator, batch_id)
        assert moved != station, "工位失联：未开始的时间窗改派到等价工位"
        event = next(row for row in operator.get("/api/exceptions").json()
                     if row["source_type"] == "station" and row["station_id"] == station)
        assert event["auto_action"] == "reroute" and batch_id in event["auto_result"]
    finally:
        _set_adapter(db, station, connected=True, last_heartbeat=now())
        DeviceMonitor(db).evaluate_station(station)
        db.commit()
        _disable_rules(db)


def test_station_loss_without_a_rule_produces_a_proposal_that_must_be_confirmed(operator, reset_runtime, db):
    from app.core.clock import now
    from app.models import Allocation
    from app.services.monitoring_service import DeviceMonitor

    _disable_rules(db)
    batch_id = _graph_batch(operator, db, _mix_flow)
    assert operator.post(f"/api/batches/{batch_id}/schedule", {}).status_code == 200
    station = _first_station(operator, batch_id)
    _set_adapter(db, station, connected=False, last_heartbeat=now() - timedelta(minutes=30))
    try:
        DeviceMonitor(db).evaluate_station(station)
        db.commit()
        assert _first_station(operator, batch_id) == station, "没有策略：不自动挪任何预约"
        proposal = next(row for row in operator.get("/api/schedule/proposals?state=pending").json()
                        if batch_id in row["batch_ids"] and row["trigger"] == "station_condition")
        assert proposal["impact"][batch_id]["moved"], "建议里写清楚换到哪台工位"
        applied = operator.post(f"/api/schedule/proposals/{proposal['id']}/apply")
        assert applied.status_code == 200, applied.text
        assert _first_station(operator, batch_id) != station
    finally:
        _set_adapter(db, station, connected=True, last_heartbeat=now())
        DeviceMonitor(db).evaluate_station(station)
        db.commit()

    # 人工请求的建议：生成后时间线被改过，应用时作废
    request = operator.post("/api/schedule/proposals", {"batch_ids": [batch_id], "reason": "调度试排"})
    assert request.status_code == 201, request.text
    db.expire_all()
    row = db.query(Allocation).filter(Allocation.batch_id == batch_id, Allocation.kind == "work").first()
    row.starts_at += timedelta(minutes=7)
    row.ends_at += timedelta(minutes=7)
    db.commit()
    stale = operator.post(f"/api/schedule/proposals/{request.json()['id']}/apply")
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "proposal_stale"


def test_deadline_mode_orders_by_due_date_and_reports_lateness(researcher, operator, reset_runtime):
    from datetime import datetime

    soon = (datetime.utcnow() + timedelta(hours=2)).isoformat()
    later = (datetime.utcnow() + timedelta(days=3)).isoformat()
    urgent = researcher.post("/api/experiment-tasks", {"plan_id": "EP-205-01", "due_at": soon}).json()
    relaxed = researcher.post("/api/experiment-tasks", {"plan_id": "EP-205-01", "due_at": later}).json()
    first = operator.post("/api/batches", {"plan_id": "EP-205-01", "task_id": relaxed["id"]}).json()["id"]
    second = operator.post("/api/batches", {"plan_id": "EP-205-01", "task_id": urgent["id"]}).json()["id"]
    preview = operator.post("/api/schedule/optimize", {"batch_ids": [first, second], "mode": "deadline"})
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["mode"] == "deadline" and body["best"]["order"][0] == second, "交付期早的先排"
    assert body["due"][second] and "lateness" in body["best"]
    fifo = operator.post("/api/schedule/optimize", {"batch_ids": [second, first], "mode": "fifo"}).json()
    assert fifo["best"]["order"] == [first, second], "先进先出按建批次的先后"


def test_urgent_batch_that_misses_its_due_date_asks_others_to_make_way(researcher, operator, reset_runtime):
    from datetime import datetime

    ordinary = operator.post("/api/batches", {"plan_id": "EP-205-01", "priority": 3}).json()["id"]
    assert operator.post(f"/api/batches/{ordinary}/schedule", {}).status_code == 200
    urgent_task = researcher.post("/api/experiment-tasks", {
        "plan_id": "EP-205-01", "priority": 1, "due_at": (datetime.utcnow() + timedelta(minutes=90)).isoformat(),
    }).json()
    urgent = operator.post("/api/batches", {"plan_id": "EP-205-01", "task_id": urgent_task["id"], "priority": 1}).json()["id"]
    assert operator.post(f"/api/batches/{urgent}/schedule", {}).status_code == 200
    proposals = [row for row in operator.get("/api/schedule/proposals?state=pending").json() if row["trigger"] == "priority_insert"]
    assert proposals and urgent in proposals[0]["batch_ids"] and ordinary in proposals[0]["batch_ids"]
    impact = proposals[0]["impact"]
    assert impact[urgent]["new_end"] < impact[urgent]["old_end"], "紧急批次提前"
