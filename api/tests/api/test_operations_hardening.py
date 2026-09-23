"""A 批加固回归：方法审批职责分离与编号、重排锚点、设备回执闭环、指令人工核查、
软件报警清除、设备与校准告警、零碎并发漏洞、执行器存活、指令超时。
"""
from datetime import timedelta

import pytest

from test_execution_safety import async_device  # noqa: F401  （复用 fixture）
from test_failure_paths import running_batch  # noqa: F401


# ---------- A1 / A2：方法审批与编号 ----------


def _draft(researcher, name: str = "职责分离验收") -> str:
    created = researcher.post("/api/recipes", {"name": name, "plate": 8, "copy_from": "R-201"})
    assert created.status_code == 201, created.text
    return created.json()["id"]


def test_recipe_author_or_submitter_cannot_approve(admin, qa, researcher, reset_runtime):
    """管理员也不例外：同一个人编写、提交又批准，审批就只剩形式。"""
    own = _draft(admin, "管理员自编自批")
    assert admin.post(f"/api/recipes/{own}/submit").status_code == 200
    refused = admin.post(
        f"/api/recipes/{own}/transition",
        {"target_state": "approved", "signature_id": admin.sign_recipe("批准", own)},
    )
    assert refused.status_code == 403
    assert refused.json()["detail"]["code"] == "self_approval_denied"

    other = _draft(researcher)
    assert researcher.post(f"/api/recipes/{other}/submit").status_code == 200
    approved = qa.post(
        f"/api/recipes/{other}/transition",
        {"target_state": "approved", "signature_id": qa.sign_recipe("批准", other)},
    )
    assert approved.status_code == 200, approved.text


def test_recipe_signature_must_target_this_recipe_version(qa, researcher, reset_runtime):
    recipe_id = _draft(researcher)
    assert researcher.post(f"/api/recipes/{recipe_id}/submit").status_code == 200

    untargeted = qa.post(
        f"/api/recipes/{recipe_id}/transition",
        {"target_state": "approved", "signature_id": qa.sign("批准")},
    )
    assert untargeted.status_code == 400, "没写对象的票据不能当通配"

    stale = qa.post(
        f"/api/recipes/{recipe_id}/transition",
        {"target_state": "approved", "signature_id": qa.sign("批准", target=recipe_id, object_version=1)},
    )
    assert stale.status_code == 400, "为旧版本签的票据不能用在已修改过的方法上"


def test_deleted_step_ids_are_never_reused(researcher, reset_runtime):
    recipe_id = _draft(researcher)
    detail = researcher.get(f"/api/recipes/{recipe_id}").json()
    steps = detail["steps"]
    removed = steps[-1]["step_id"]

    shorter = researcher.patch(
        f"/api/recipes/{recipe_id}", {"steps": steps[:-1], "row_version": detail["row_version"]},
    )
    assert shorter.status_code == 200, shorter.text
    new_step = {k: v for k, v in steps[-1].items() if k != "step_id"}
    longer = researcher.patch(
        f"/api/recipes/{recipe_id}",
        {"steps": [*shorter.json()["steps"], new_step], "row_version": shorter.json()["row_version"]},
    )
    assert longer.status_code == 200, longer.text
    assert longer.json()["steps"][-1]["step_id"] != removed, "删掉的步骤 ID 不能再分配给新步骤"


def test_concurrent_recipe_edits_conflict(researcher, reset_runtime):
    recipe_id = _draft(researcher)
    version = researcher.get(f"/api/recipes/{recipe_id}").json()["row_version"]
    assert researcher.patch(f"/api/recipes/{recipe_id}", {"risk": "RA-1", "row_version": version}).status_code == 200
    late = researcher.patch(f"/api/recipes/{recipe_id}", {"risk": "RA-2", "row_version": version})
    assert late.status_code == 409


def _release(researcher, qa, recipe_id: str) -> None:
    assert researcher.post(f"/api/recipes/{recipe_id}/submit").status_code == 200
    for state in ("approved", "released"):
        moved = qa.post(
            f"/api/recipes/{recipe_id}/transition",
            {"target_state": state, "signature_id": qa.sign_recipe(state, recipe_id)},
        )
        assert moved.status_code == 200, moved.text


def test_revision_numbers_versions_and_supersession(researcher, qa, reset_runtime):
    source = _draft(researcher, "修订编号验收")
    _release(researcher, qa, source)

    first = researcher.post(f"/api/recipes/{source}/revision").json()
    second = researcher.post(f"/api/recipes/{source}/revision").json()
    assert first["version"] != second["version"], "同一来源的两个修订不能拿到同一个版本号"
    assert researcher.delete(f"/api/recipes/{first['id']}").status_code == 200
    third = researcher.post(f"/api/recipes/{source}/revision")
    assert third.status_code == 200, third.text
    assert third.json()["id"] not in {first["id"], second["id"]}, "删掉 r1 后不能再造一个撞号的修订"

    _release(researcher, qa, second["id"])
    assert researcher.get(f"/api/recipes/{source}").json()["state"] == "retired", (
        "修订版发布后来源版本退役，不能同时存在两个已发布版本"
    )


# ---------- A5：重排锚点与状态 ----------


def test_reschedule_tail_waits_for_previous_step(operator, reset_runtime):
    from app.core.clock import now

    batch_id = operator.post("/api/batches", {"plan_id": "EP-205-01"}).json()["id"]
    assert operator.post(f"/api/batches/{batch_id}/schedule", {}).status_code == 200
    before = operator.get(f"/api/batches/{batch_id}").json()["allocations"]
    step_one_end = max(a["ends_at"] for a in before if a["step_index"] == 1 and a["kind"] == "work")

    # 期望开始时间早于第 2 步结束：尾段也不能早于上一步结束开工
    moved = operator.post(
        f"/api/batches/{batch_id}/reschedule",
        {"from_step": 2, "start_from": now().isoformat()},
    )
    assert moved.status_code == 200, moved.text
    after = operator.get(f"/api/batches/{batch_id}").json()["allocations"]
    step_two = min(a["starts_at"] for a in after if a["step_index"] == 2 and a["kind"] == "work")
    assert step_two >= step_one_end


def test_finished_batch_cannot_be_rescheduled(operator, reset_runtime):
    from app.core.clock import now
    from app.core.db import SessionLocal
    from app.models import Batch

    batch_id = operator.post("/api/batches", {"plan_id": "EP-205-01"}).json()["id"]
    with SessionLocal() as db:
        db.get(Batch, batch_id).state = "done"
        db.commit()
    refused = operator.post(
        f"/api/batches/{batch_id}/reschedule",
        {"from_step": 1, "start_from": (now() + timedelta(hours=1)).isoformat()},
    )
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "batch_not_reschedulable"


# ---------- A6：资质按执行时段 ----------


def test_qualification_is_checked_at_both_ends_of_the_window(monkeypatch, db):
    from app.core.clock import now
    from app.core.context import system_context
    from app.services import people_service

    seen = []

    def spy(_spec, _requirements, moment):
        seen.append(moment)
        return ["资质到期"] if moment > start + timedelta(hours=1) else []

    start = now()
    end = start + timedelta(hours=3)
    monkeypatch.setattr(people_service, "blockers_for", spy)
    monkeypatch.setattr(people_service, "requirements_for_steps", lambda *_: ["cap.mix"])
    service = people_service.PeopleService(db, system_context("ORG-001"))
    reasons = service.blockers_for_steps("U-any", [{"cap": "cap.mix"}], start, until=end)
    assert seen == [start, end]
    assert reasons and "按计划结束时间" in reasons[0], "开始时有效、中途到期同样挡住"


# ---------- A7：设备回执闭环 ----------


def test_device_callback_settles_command_and_advances(
    operator, device, running_batch, async_device, executor,
):
    """不支持查询的设备靠回执结束指令：写检查点、释放工位、推进；重复回执只回放。"""
    from app.core.db import SessionLocal
    from app.models import Adapter, Checkpoint

    executor()
    command = operator.get(f"/api/batches/{running_batch}").json()["commands"][0]
    assert command["state"] == "running"

    done = device.post(
        f"/api/runtime/commands/{command['id']}/events",
        {"outcome": "done", "quality": "good", "delivered": {"temp": 120},
         "device_ts": "2026-09-23T10:00:00+08:00"},
    )
    assert done.status_code == 200, done.text
    assert done.json()["state"] == "done"
    replay = device.post(f"/api/runtime/commands/{command['id']}/events", {"outcome": "done"})
    assert replay.status_code == 200 and replay.json()["replayed"] is True

    with SessionLocal() as db:
        checkpoints = db.query(Checkpoint).filter(Checkpoint.command_id == command["id"]).all()
        assert len(checkpoints) == 1, "重复回执不能产生第二个检查点"
        assert db.get(Adapter, command["station_id"]).current_command_id == ""
    runs = operator.get(f"/api/batches/{running_batch}").json()["step_runs"]
    assert runs[0]["state"] == "completed" and len(runs) >= 2


# ---------- A8 / A9：结果未知指令的人工核查 ----------


def _fault_first_command(operator, batch_id: str, executor) -> dict:
    """首条指令投递时网络超时：结果未知、可能已送达。"""
    from app.adapters import AdapterUnreachable
    from app.adapters.simulation import SimulationAdapter

    original = SimulationAdapter.submit

    def timeout(self, request):
        raise AdapterUnreachable("网关超时")

    SimulationAdapter.submit = timeout
    try:
        executor()
    finally:
        SimulationAdapter.submit = original
    command = operator.get(f"/api/batches/{batch_id}").json()["commands"][0]
    assert (command["state"], command["delivery_state"]) == ("unknown", "maybe_sent")
    return command


def _verify(operator, command_id: str, conclusion: str, note: str = "现场查看设备面板"):
    return operator.post(
        f"/api/commands/{command_id}/verify",
        {"conclusion": conclusion, "note": note,
         "signature_id": operator.sign("已到现场核实设备实态", target=command_id)},
    )


def test_verified_not_executed_allows_retry(operator, running_batch, executor):
    command = _fault_first_command(operator, running_batch, executor)
    blocked = operator.get(f"/api/batches/{running_batch}/recovery-options").json()
    assert blocked["blind_retry_allowed"] is False

    assert operator.post(
        f"/api/commands/{command['id']}/verify",
        {"conclusion": "not_executed", "note": "",
         "signature_id": operator.sign("核实", target=command["id"])},
    ).status_code == 422, "核查依据必填"

    verified = _verify(operator, command["id"], "not_executed")
    assert verified.status_code == 200, verified.text
    assert verified.json()["command_state"] == "not_executed"
    evaluation = operator.get(f"/api/batches/{running_batch}/recovery-options").json()
    assert evaluation["blind_retry_allowed"] is True, "现场确认未执行后才允许重试"


def test_verified_executed_writes_manual_checkpoint(operator, running_batch, executor):
    from app.core.db import SessionLocal
    from app.models import Checkpoint

    command = _fault_first_command(operator, running_batch, executor)
    verified = _verify(operator, command["id"], "executed", "干燥箱记录显示已完成 60 min 程序")
    assert verified.status_code == 200, verified.text
    with SessionLocal() as db:
        checkpoint = db.query(Checkpoint).filter(Checkpoint.command_id == command["id"]).one()
        assert checkpoint.payload["origin"] == "manual_verification"
    runs = operator.get(f"/api/batches/{running_batch}").json()["step_runs"]
    assert runs[0]["state"] == "completed"


def test_partial_execution_only_allows_abort(operator, running_batch, executor):
    command = _fault_first_command(operator, running_batch, executor)
    assert _verify(operator, command["id"], "partial", "泵在 40% 处停住").status_code == 200
    options = {
        row["id"]: row for row in
        operator.get(f"/api/batches/{running_batch}/recovery-options").json()["options"]
    }
    assert not options["resume"]["allowed"] and not options["retry"]["allowed"]
    assert options["abort"]["allowed"]


def test_offline_abort_is_confirmed_by_field_verification(
    operator, running_batch, async_device, executor,
):
    """设备离线时终止指令发不出去：现场确认已安全停机，签名后批次终止。"""
    from app.core.db import SessionLocal
    from app.models import Adapter

    executor()
    station_id = operator.get(f"/api/batches/{running_batch}").json()["commands"][0]["station_id"]
    with SessionLocal() as db:
        db.get(Adapter, station_id).connected = False
        db.commit()
    aborted = operator.post(
        f"/api/batches/{running_batch}/abort",
        {"reason": "设备离线", "signature_id": operator.sign("安全终止", target=running_batch)},
    )
    assert aborted.json()["state"] == "aborting"
    executor()
    detail = operator.get(f"/api/batches/{running_batch}").json()
    abort_command = next(c for c in detail["commands"] if c["type"] == "abort")
    assert abort_command["state"] == "unknown"

    confirmed = _verify(operator, abort_command["id"], "executed", "现场已按急停规程停机并断电")
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["batch_state"] == "aborted"


# ---------- A10：软件报警清除 ----------


def test_operator_clears_system_alarm_with_reason_and_signature(operator, running_batch, executor):
    _fault_first_command(operator, running_batch, executor)
    alarm = next(
        a for a in operator.get("/api/alarms").json()
        if a["source_id"] == running_batch and a["condition_active"]
    )
    assert alarm["origin"] == "system"
    cleared = operator.post(
        f"/api/alarms/{alarm['id']}/clear-condition",
        {"reason": "网关已重启，链路恢复", "signature_id": operator.sign("异常原因已消除", target=alarm["id"])},
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["condition_active"] is False


def test_device_alarm_cannot_be_cleared_by_hand(operator, ehs, reset_runtime):
    from app.core.db import SessionLocal
    from app.models import Alarm

    with SessionLocal() as db:
        alarm = db.query(Alarm).filter(Alarm.origin == "device", Alarm.condition_active.is_(True)).first()
        if alarm is None:
            pytest.skip("种子里没有持续中的设备侧报警")
        alarm_id = alarm.id
    refused = ehs.post(
        f"/api/alarms/{alarm_id}/clear-condition",
        {"reason": "看着像好了", "signature_id": ehs.sign("清除", target=alarm_id)},
    )
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "device_alarm"


# ---------- A11：设备与校准告警 ----------


def _station_alarms(operator, station_id: str) -> list[dict]:
    return [
        a for a in operator.get("/api/alarms").json()
        if a["source_type"] == "station" and a["source_id"] == station_id
        and a["condition_key"].endswith(":disconnected")
    ]


def test_disconnect_raises_one_alarm_and_clears_on_recovery(operator, reset_runtime, executor):
    from app.core.db import SessionLocal
    from app.models import Adapter

    with SessionLocal() as db:
        db.get(Adapter, "ST-02").connected = False
        db.commit()
    executor()
    executor()
    raised = [a for a in _station_alarms(operator, "ST-02") if a["condition_active"]]
    assert len(raised) == 1, "条件持续期间只报一次"

    with SessionLocal() as db:
        db.get(Adapter, "ST-02").connected = True
        db.commit()
    executor()
    assert not [a for a in _station_alarms(operator, "ST-02") if a["condition_active"]]


def test_calibration_due_soon_raises_alarm(admin, reset_runtime, db):
    from app.core.clock import now
    from app.models import CalibrationRecord
    from app.services.monitoring_service import DeviceMonitor

    asset = admin.post("/api/assets", {"asset_no": "AS-DUE-1", "name": "临期资产"}).json()
    db.add(CalibrationRecord(
        org_id="ORG-001", asset_id=asset["id"], capability_scope=[], result="pass",
        effective_from=now() - timedelta(days=300), expires_at=now() + timedelta(days=5),
        certificate_file_id="", registered_by="test",
    ))
    db.commit()
    DeviceMonitor(db).calibrations()
    db.commit()
    alarms = [a for a in admin.get("/api/alarms").json() if a["source_id"] == asset["id"]]
    assert any(a["condition_key"].endswith("calibration_due") for a in alarms)


# ---------- A12：零碎漏洞 ----------


def test_adapter_change_records_field_diff(admin, reset_runtime, db):
    from app.models import AuditEvent

    adapter = admin.get("/api/stations/ST-02/adapter").json()
    changed = admin.patch("/api/stations/ST-02/adapter", {
        "config": {**(adapter["config"] or {}), "base_url": "https://gw-b.lab.local"},
        "row_version": adapter["row_version"],
        "signature_id": admin.sign("设备集成配置变更批准", target="ST-02", object_version=adapter["row_version"]),
    })
    assert changed.status_code == 200, changed.text
    event = (
        db.query(AuditEvent).filter(AuditEvent.action == "修改设备适配器", AuditEvent.target == "ST-02")
        .order_by(AuditEvent.id.desc()).first()
    )
    assert "config.base_url" in event.detail and "gw-b.lab.local" in event.detail


def test_disabled_adapter_rejects_heartbeat(device, reset_runtime):
    from app.core.db import SessionLocal
    from app.models import Adapter

    with SessionLocal() as db:
        db.get(Adapter, "ST-05").enabled = False
        db.commit()
    try:
        refused = device.post("/api/runtime/stations/ST-05/heartbeat", {"connected": True})
        assert refused.status_code == 409
        assert refused.json()["detail"]["code"] == "adapter_disabled"
    finally:
        with SessionLocal() as db:
            db.get(Adapter, "ST-05").enabled = True
            db.commit()


def test_station_edits_are_optimistically_locked(admin, reset_runtime):
    station = next(s for s in admin.get("/api/stations").json() if s["id"] == "ST-02")
    version = station["row_version"]
    assert admin.patch("/api/stations/ST-02", {"model": "M-1", "row_version": version}).status_code == 200
    stale = admin.patch("/api/stations/ST-02", {"model": "M-2", "row_version": version})
    assert stale.status_code == 409


def test_station_id_taken_by_another_org_is_a_conflict(admin, reset_runtime, db):
    from app.models import Station

    db.add(Station(id="ST-OTHER-ORG", org_id="ORG-002", name="别的组织的工位", island=1))
    db.commit()
    refused = admin.post("/api/stations", {
        "id": "ST-OTHER-ORG", "name": "撞号", "island": 1,
        "signature_id": admin.sign("工程变更批准", target="ST-OTHER-ORG"),
    })
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["code"] == "station_id_taken"
    assert "ORG-002" not in refused.text, "不泄露占用方组织"


# ---------- A13：执行器存活 ----------


def test_gate_closes_when_executor_stops_reporting(operator, reset_runtime, executor, monkeypatch, db):
    from app.core.clock import now
    from app.core.config import settings
    from app.models import ExecutorHeartbeat

    monkeypatch.setattr(settings, "executor_stale_sec", 60)
    executor()
    assert operator.get("/api/gate").json()["open"] is True

    row = db.get(ExecutorHeartbeat, "executor")
    row.last_seen = now() - timedelta(minutes=5)
    db.commit()
    gate = operator.get("/api/gate").json()
    assert gate["open"] is False and "执行器" in gate["reasons"][0]

    executor()
    assert operator.get("/api/gate").json()["open"] is True


# ---------- A14：指令超时 ----------


def test_stuck_command_warns_then_turns_unknown(operator, running_batch, async_device, executor, db):
    from app.core.clock import now
    from app.models import Command

    executor()
    command_id = operator.get(f"/api/batches/{running_batch}").json()["commands"][0]["id"]

    command = db.get(Command, command_id)
    command.started_at = now() - timedelta(minutes=100)  # 干燥 60 min：超过 1.5 倍 + 5 min
    db.commit()
    executor()
    executor()
    overdue = [
        a for a in operator.get("/api/alarms").json()
        if a["condition_key"] == f"command:{command_id}:overdue"
    ]
    assert len(overdue) == 1, "超时先报警，且只报一次"
    assert operator.get(f"/api/batches/{running_batch}").json()["state"] == "running"

    db.expire_all()
    command = db.get(Command, command_id)
    command.started_at = now() - timedelta(minutes=200)  # 超过 3 倍 + 5 min 硬上限
    db.commit()
    executor()
    detail = operator.get(f"/api/batches/{running_batch}").json()
    stuck = next(c for c in detail["commands"] if c["id"] == command_id)
    assert (stuck["state"], stuck["delivery_state"]) == ("unknown", "maybe_sent")
    assert detail["state"] == "fault"
