"""设备接入验收入库与配置变更后的验收闸门。

改了配置的真实设备先「待接入验收」，执行器自动跑一次只读级，通过了（级别够）才重新接指令。
验收由执行器执行、报告入库且只追加；动作级要签名并写明现场批准人，等设备空闲才开始。
模拟设备走真实协议，驱动不打桩。
"""
import time

import pytest
from sqlalchemy import text

from sim_harness import gateway_config, gateway_sim

LIMITS = {"cap.vacuum_dry": {"temp": [60, 180], "vacuum": [0.1, 5]}}


@pytest.fixture()
def credential_root(tmp_path, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "adapter_credential_root", str(tmp_path))
    return tmp_path


def _station(admin, station_id: str) -> str:
    """验收用的工位：用完即停用（见 `station` 夹具），免得排程把别的用例的批次排到它上面。"""
    from app.core.db import SessionLocal
    from app.models import Station

    with SessionLocal() as db:
        exists = db.get(Station, station_id) is not None
    if not exists:
        created = admin.post("/api/stations", {
            "id": station_id, "name": "接入验收测试站", "protocol": "sim", "limits": LIMITS,
            "adapter_kind": "simulation", "adapter_driver": "simulation",
            "signature_id": admin.sign("工程变更批准", target=station_id),
        })
        assert created.status_code == 201, created.text
    else:
        assert admin.post(f"/api/stations/{station_id}/retire", {"retired": False}).status_code == 200
    return station_id


@pytest.fixture()
def station(admin):
    yield _station(admin, "ST-96")
    _back_to_simulation(admin, "ST-96")
    assert admin.post("/api/stations/ST-96/retire", {"retired": True}).status_code == 200


def _patch(admin, station_id: str, **changes):
    adapter = admin.get(f"/api/stations/{station_id}/adapter").json()
    return admin.patch(f"/api/stations/{station_id}/adapter", {
        **changes, "row_version": adapter["row_version"],
        "signature_id": admin.sign("设备集成配置变更批准", target=station_id, object_version=adapter["row_version"]),
    })


def _to_gateway(admin, station_id: str, port: int, credential_root):
    config, token = gateway_config(port, credential_root)
    switched = _patch(admin, station_id, kind="real", driver="http_json_v1", protocol="HTTPS JSON",
                      config=config, credential_ref=token)
    assert switched.status_code == 200, switched.text
    return switched.json()


def _back_to_simulation(admin, station_id: str) -> None:
    _patch(admin, station_id, kind="simulation", driver="simulation", protocol="sim", config={}, credential_ref="")


def _station_pass(station_id: str) -> dict:
    from app.core.db import SessionLocal
    from app.services.execution_service import ExecutorLoop

    with SessionLocal() as db:
        return ExecutorLoop(db).station_pass(station_id)


def _runs(admin, station_id: str) -> dict:
    listed = admin.get(f"/api/stations/{station_id}/adapter/acceptance")
    assert listed.status_code == 200, listed.text
    return listed.json()


def _physical(admin, station_id: str, **extra):
    adapter = admin.get(f"/api/stations/{station_id}/adapter").json()
    return admin.post(f"/api/stations/{station_id}/adapter/acceptance", {
        "level": "physical", "approval": "现场负责人 张工 已确认设备空载、周边无人", **extra,
        "signature_id": admin.sign("批准设备接入验收", target=station_id, object_version=adapter["config_version"]),
    })


def test_switching_to_a_real_driver_waits_for_acceptance(admin, credential_root, reset_runtime, station):
    station_id = station
    with gateway_sim(credential_root, task_seconds=0.5) as (device, _, port):
        switched = _to_gateway(admin, station_id, port, credential_root)
        # 第一次接成真实设备：欠动作级；已自动排了一次只读级
        assert switched["acceptance"]["required"] == "physical", switched["acceptance"]
        listed = _runs(admin, station_id)
        first = listed["runs"][0]
        assert first["state"] == "queued" and first["level"] == "readonly" and first["trigger"] == "config_change"
        gate = admin.get("/api/gate").json()
        # 刚改完配置先离线（要重新握手）；待接入验收单独列着
        assert station_id in gate["blocked_stations"] and "待接入验收" in gate["acceptance_pending"][station_id]

        report = _station_pass(station_id)
        assert report["accepted"] == 1
        run = _runs(admin, station_id)["runs"][0]
        assert run["state"] == "done" and run["ok"] and run["simulator"], run
        assert run["driver"] == "http_json_v1" and run["config_version"] == switched["config_version"]
        assert not device.executions, "只读级验收不让设备动作"

        # 自报为模拟器：只读级就清掉动作级要求（模拟设备不会造成物理后果、正式环境也不许接入）
        adapter = admin.get(f"/api/stations/{station_id}/adapter").json()
        assert adapter["acceptance"]["required"] == ""
        assert adapter["acceptance"]["accepted_run_id"] == run["id"]
        assert adapter["connected"], "验收之后同一轮探测照常把设备标为在线"
        assert station_id not in admin.get("/api/gate").json()["blocked_stations"]

        detail = admin.get(f"/api/acceptance-runs/{run['id']}").json()
        states = {check["key"]: check["state"] for check in detail["checks"]}
        assert states["identity"] == states["health"] == states["query_unknown"] == "pass"
        assert states["complete"] == "skip"
        assert detail["report_md"].startswith("# 设备接入验收报告：ST-96") and run["id"] in detail["report_md"]
        download = admin.get(f"/api/acceptance-runs/{run['id']}/report.md")
        assert download.status_code == 200 and "text/markdown" in download.headers["content-type"]


def test_real_device_needs_an_approved_physical_acceptance(admin, credential_root, reset_runtime, monkeypatch, station):
    station_id = station
    with gateway_sim(credential_root, task_seconds=0.3) as (device, _, port):
        reported = device.identity
        monkeypatch.setattr(device, "identity", lambda: {**reported(), "simulator": False})
        _to_gateway(admin, station_id, port, credential_root)
        _station_pass(station_id)
        run = _runs(admin, station_id)["runs"][0]
        assert run["ok"] and not run["simulator"]
        gate = _runs(admin, station_id)["gate"]
        assert gate["required"] == "physical", "真实设备只读级不够：还欠动作级"

        missing = admin.post(f"/api/stations/{station_id}/adapter/acceptance", {"level": "physical"})
        assert missing.status_code == 422 and missing.json()["detail"]["code"] == "acceptance_approval_required"
        unsigned = admin.post(f"/api/stations/{station_id}/adapter/acceptance", {
            "level": "physical", "approval": "现场负责人 张工",
        })
        assert unsigned.status_code == 400, unsigned.text
        outside = _physical(admin, station_id, params={"temp": 500, "vacuum": 1})
        assert outside.status_code == 422 and "超出" in outside.json()["detail"]["message"]

        requested = _physical(admin, station_id)
        assert requested.status_code == 201, requested.text
        assert requested.json()["level"] == "physical" and requested.json()["params"] == {"temp": 120, "vacuum": 2.55}
        again = _physical(admin, station_id)
        assert again.status_code == 409 and again.json()["detail"]["code"] == "acceptance_busy"

        _station_pass(station_id)
        run = _runs(admin, station_id)["runs"][0]
        assert run["state"] == "done" and run["ok"], admin.get(f"/api/acceptance-runs/{run['id']}").json()
        detail = admin.get(f"/api/acceptance-runs/{run['id']}").json()
        states = {check["key"]: check["state"] for check in detail["checks"]}
        assert all(states[key] == "pass" for key in ("complete", "restart_query", "hold", "abort")), states
        # 真实设备读不到设备侧动作次数：「重复提交只动作一次」判跳过，由现场核对设备记录
        assert states["duplicate"] == "skip", states
        assert "现场批准：现场负责人 张工" in detail["report_md"]
        assert _runs(admin, station_id)["gate"]["required"] == ""


def test_failed_acceptance_refuses_dispatch_until_the_device_is_back(admin, credential_root, reset_runtime, station):
    from app.core.db import SessionLocal
    from app.models import Adapter
    from app.services.acceptance_service import dispatch_hold

    station_id = station
    with gateway_sim(credential_root, task_seconds=0.3) as (_, runner, port):
        _to_gateway(admin, station_id, port, credential_root)
        runner.go_offline(3)
        time.sleep(0.5)
        _station_pass(station_id)
        run = _runs(admin, station_id)["runs"][0]
        assert run["state"] == "done" and run["ok"] is False
        with SessionLocal() as db:
            hold, reason = dispatch_hold(db, db.get(Adapter, station_id))
        assert hold == "refuse" and "待接入验收" in reason

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and _runs(admin, station_id)["gate"]["required"]:
            _station_pass(station_id)  # 设备回来：探测判在线，上次没通过的自动验收再排一次，下一轮执行
            time.sleep(0.3)
        listed = _runs(admin, station_id)
        assert listed["gate"]["required"] == "", listed
        assert listed["runs"][0]["trigger"] == "device_online" and listed["runs"][0]["ok"]


def test_request_rules_and_cancel(admin, reset_runtime):
    station_id = "ST-05"
    faults_alone = admin.post(f"/api/stations/{station_id}/adapter/acceptance", {"level": "readonly", "faults": True})
    assert faults_alone.status_code == 422 and faults_alone.json()["detail"]["code"] == "acceptance_faults_need_physical"
    unknown = admin.post(f"/api/stations/{station_id}/adapter/acceptance", {"capability": "cap.nonexistent"})
    assert unknown.status_code == 422

    queued = admin.post(f"/api/stations/{station_id}/adapter/acceptance", {})
    assert queued.status_code == 201, queued.text
    busy = admin.post(f"/api/stations/{station_id}/adapter/acceptance", {})
    assert busy.status_code == 409
    cancelled = admin.post(f"/api/acceptance-runs/{queued.json()['id']}/cancel")
    assert cancelled.status_code == 200 and cancelled.json()["state"] == "cancelled"
    assert admin.post(f"/api/acceptance-runs/{queued.json()['id']}/cancel").status_code == 409

    # 内置模拟适配器也能验收（开发与测试环境）：由执行器执行
    queued = admin.post(f"/api/stations/{station_id}/adapter/acceptance", {})
    _station_pass(station_id)
    run = admin.get(f"/api/acceptance-runs/{queued.json()['id']}").json()
    assert run["state"] == "done" and run["ok"] and run["simulator"], run


def test_physical_acceptance_waits_for_an_idle_device(admin, operator, reset_runtime):
    from app.core.clock import now
    from app.core.db import SessionLocal
    from app.models import Adapter, Command
    from app.services.acceptance_service import AcceptanceRunner, dispatch_hold

    created = operator.post("/api/batches", {"plan_id": "EP-205-01"})
    batch_id = created.json()["id"]
    assert operator.post(f"/api/batches/{batch_id}/schedule", {}).status_code == 200
    assert operator.post(f"/api/batches/{batch_id}/dispatch", {
        "manual_review": True, "signature_id": operator.sign("批准执行", target=batch_id),
    }).status_code == 200
    with SessionLocal() as db:
        command = db.query(Command).filter(Command.batch_id == batch_id).order_by(Command.created_at).first()
        command.state, command.delivery_state, command.started_at = "running", "delivered", now()
        command_id, station_id = command.id, command.station_id
        db.commit()

    requested = _physical(admin, station_id)
    assert requested.status_code == 201, requested.text
    assert requested.json()["waiting_for"] == 1
    with SessionLocal() as db:
        assert AcceptanceRunner(db).run_due(station_id) == 0, "设备上还有动作：动作级验收等它结束"
        hold, _ = dispatch_hold(db, db.get(Adapter, station_id))
        assert hold == "wait", "排队期间新的动作指令先不投，免得一直等不到空档"
        db.get(Command, command_id).state = "done"
        db.commit()
        assert AcceptanceRunner(db).run_due(station_id) == 1
    run = admin.get(f"/api/acceptance-runs/{requested.json()['id']}").json()
    assert run["state"] == "done" and run["level"] == "physical", run


def test_acceptance_records_are_append_only_and_interrupted_runs_close(admin, db, reset_runtime):
    from app.core.clock import now
    from app.models import AcceptanceRun
    from app.services.acceptance_service import AcceptanceRunner

    queued = admin.post("/api/stations/ST-05/adapter/acceptance", {})
    _station_pass("ST-05")
    run_id = queued.json()["id"]
    with pytest.raises(Exception, match="已出结论"):
        db.execute(text("UPDATE acceptance_runs SET ok = false WHERE id = :id"), {"id": run_id})
    db.rollback()
    with pytest.raises(Exception, match="禁止删除"):
        db.execute(text("DELETE FROM acceptance_runs WHERE id = :id"), {"id": run_id})
    db.rollback()

    orphan = AcceptanceRun(org_id="ORG-001", station_id="ST-05", level="physical", state="running", started_at=now(),
                           created_at=now())
    readonly = AcceptanceRun(org_id="ORG-001", station_id="ST-04", level="readonly", state="running", started_at=now(),
                             created_at=now())
    db.add_all([orphan, readonly])
    db.commit()
    assert AcceptanceRunner(db).interrupt_orphans() == 2
    db.refresh(orphan)
    db.refresh(readonly)
    assert orphan.state == "error" and "ACC-" in orphan.error, "动作级中断：设备上可能留有验收指令"
    assert readonly.state == "error" and "ACC-" not in readonly.error, "只读级不让设备动作，留不下什么"


def test_config_change_cancels_queued_runs_and_requests_readonly_again(admin, credential_root, reset_runtime, station):
    station_id = station
    try:
        with gateway_sim(credential_root) as (_, _, port):
            _to_gateway(admin, station_id, port, credential_root)
            first = _runs(admin, station_id)["runs"][0]
            tuned = _patch(admin, station_id, note="放宽超时",
                           config={**gateway_config(port, credential_root)[0], "request_timeout_sec": 5})
            assert tuned.status_code == 200
            # 还没补上的动作级要求不因为又改了一次而降级
            assert tuned.json()["acceptance"]["required"] == "physical"
            runs = _runs(admin, station_id)["runs"]
            old = next(run for run in runs if run["id"] == first["id"])
            assert old["state"] == "cancelled" and "作废" in old["error"]
            assert runs[0]["state"] == "queued" and runs[0]["config_version"] == tuned.json()["config_version"]
    finally:
        _back_to_simulation(admin, station_id)
    assert _runs(admin, station_id)["gate"]["required"] == "", "改回模拟适配器：不设闸门"


def test_physical_acceptance_waits_behind_the_same_gate_as_commands(admin, reset_runtime, monkeypatch):
    """动作级验收和动作指令守同一道门：执行门、在线、心跳、联锁、串行模式、同时进行的数量；等待原因写在记录上。"""
    from datetime import timedelta

    from app.core.clock import now
    from app.core.config import settings
    from app.core.db import SessionLocal
    from app.models import AcceptanceRun, Adapter
    from app.services.acceptance_service import AcceptanceRunner, dispatch_hold

    station_id = "ST-05"
    requested = _physical(admin, station_id)
    assert requested.status_code == 201, requested.text
    run_id = requested.json()["id"]

    def waiting(**options) -> str:
        with SessionLocal() as db:
            assert AcceptanceRunner(db).run_due(station_id, **options) == 0
            return db.get(AcceptanceRun, run_id).error

    def adapter(**values) -> None:
        with SessionLocal() as db:
            row = db.get(Adapter, station_id)
            for key, value in values.items():
                setattr(row, key, value)
            db.commit()

    assert "串行模式" in waiting(physical=False)
    assert "执行门关闭" in waiting(dispatch_open=False)
    adapter(connected=False)
    assert "失联" in waiting(dispatch_open=True)
    adapter(connected=True, site_interlock=True)
    assert "联锁" in waiting(dispatch_open=True)
    adapter(site_interlock=False, last_heartbeat=now() - timedelta(seconds=settings.heartbeat_stale_sec + 5))
    assert "心跳" in waiting(dispatch_open=True)
    adapter(last_heartbeat=now())
    listed = _runs(admin, station_id)["runs"][0]
    assert listed["state"] == "queued" and listed["error"].startswith("等待："), "界面看得到在等什么"

    monkeypatch.setattr(settings, "executor_workers", 2)
    with SessionLocal() as db:
        other = AcceptanceRun(org_id="ORG-001", station_id="ST-04", level="physical", state="running",
                              created_at=now(), started_at=now())
        db.add(other)
        db.commit()
        other_id = other.id
    try:
        assert "上限 1" in waiting(dispatch_open=True)
    finally:
        with SessionLocal() as db:
            db.query(AcceptanceRun).filter(AcceptanceRun.id == other_id).update({"state": "cancelled"})
            db.commit()

    # 续跑不等动作级验收：它接续的是验收之前就在设备上保持着的作业，两边互等谁也动不了
    with SessionLocal() as db:
        row = db.get(Adapter, station_id)
        assert dispatch_hold(db, row, "dispatch")[0] == "wait"
        assert dispatch_hold(db, row, "resume") == ("", "")
    with SessionLocal() as db:
        assert AcceptanceRunner(db).run_due(station_id, dispatch_open=True) == 1
    assert admin.get(f"/api/acceptance-runs/{run_id}").json()["state"] == "done"


def test_a_run_queued_for_an_older_config_is_not_executed(admin, reset_runtime):
    """申请时签的是那一版配置：排队之后配置变了（哪怕绕过了服务层），这条作废，不拿旧申请去动新配置的设备。"""
    from app.core.db import SessionLocal
    from app.models import AcceptanceRun, Adapter
    from app.services.acceptance_service import AcceptanceRunner

    queued = admin.post("/api/stations/ST-05/adapter/acceptance", {})
    assert queued.status_code == 201, queued.text
    with SessionLocal() as db:
        db.get(Adapter, "ST-05").config_version += 1
        db.commit()
    with SessionLocal() as db:
        assert AcceptanceRunner(db).run_due("ST-05") == 0
        run = db.get(AcceptanceRun, queued.json()["id"])
        assert run.state == "cancelled" and "作废" in run.error


def test_leftover_acceptance_commands_put_the_station_back_to_physical(admin, credential_root, reset_runtime, station):
    """验收留下了没结论的 ACC- 指令（设备可能还在动）：工位改回欠动作级，哪怕报告其余项目都通过。"""
    from app.adapters.acceptance import AcceptanceRecord
    from app.core.db import SessionLocal
    from app.models import Adapter
    from app.services.acceptance_service import AcceptanceRunner

    station_id = station
    with gateway_sim(credential_root, task_seconds=0.3) as (_, _, port):
        _to_gateway(admin, station_id, port, credential_root)
        _station_pass(station_id)
        assert _runs(admin, station_id)["gate"]["required"] == ""
        with SessionLocal() as db:
            record = AcceptanceRecord.of(db.get(Adapter, station_id))
            cleared = AcceptanceRunner(db)._settle_gate(record, "physical", True, True, "RUN-X", ["ACC-X-hold-target"],
                                                        "ORG-001")
        assert not cleared
        gate = _runs(admin, station_id)["gate"]
        assert gate["required"] == "physical", gate
        audit = admin.get("/api/audit", params={"target": station_id}).json()
        rows = audit["items"] if isinstance(audit, dict) else audit
        assert any(row["action"] == "接入验收改回欠动作级" for row in rows)


def test_signed_waiver_for_what_the_checklist_cannot_prove(admin, credential_root, reset_runtime, monkeypatch, station):
    """不支持状态查询、要现场摆位的设备：现场核对后签名放行；放行记录存档，签的是这一版配置。"""
    station_id = station
    with gateway_sim(credential_root, task_seconds=0.3) as (device, _, port):
        reported = device.identity
        monkeypatch.setattr(device, "identity", lambda: {**reported(), "simulator": False})
        switched = _to_gateway(admin, station_id, port, credential_root)
        version = switched["config_version"]

        def waive(reason="设备不支持状态查询；现场负责人 张工 已手动走完一次循环并核对了回报", object_version=version):
            return admin.post(f"/api/stations/{station_id}/adapter/acceptance/waive", {
                "reason": reason,
                "signature_id": admin.sign("签名放行接入验收", target=station_id, object_version=object_version),
            })

        busy = waive()
        assert busy.status_code == 409 and busy.json()["detail"]["code"] == "acceptance_busy", "排队中的验收先出结论"
        _station_pass(station_id)
        assert _runs(admin, station_id)["gate"]["required"] == "physical"
        assert waive(reason="好").status_code == 422
        stale = waive(object_version=version - 1)
        assert stale.status_code in {400, 403}, "签名要针对当前配置版本"
        waived = waive()
        assert waived.status_code == 201, waived.text
        run = waived.json()
        assert run["trigger"] == "waiver" and run["state"] == "done" and run["ok"]
        listed = _runs(admin, station_id)
        assert listed["gate"]["required"] == "" and listed["gate"]["accepted_run_id"] == run["id"]
        report = admin.get(f"/api/acceptance-runs/{run['id']}").json()["report_md"]
        assert "签名放行" in report and "张工" in report
        again = waive()
        assert again.status_code == 409 and again.json()["detail"]["code"] == "acceptance_not_required"


def test_note_and_timeout_changes_keep_the_device_connected(admin, credential_root, reset_runtime, station):
    """只改说明与超时：不改变连谁、怎么判结论——不断开设备（保持 / 终止要能立刻下发），也不新欠验收。"""
    station_id = station
    with gateway_sim(credential_root) as (_, _, port):
        _to_gateway(admin, station_id, port, credential_root)
        _station_pass(station_id)
        assert _runs(admin, station_id)["gate"]["required"] == ""
        before = admin.get(f"/api/stations/{station_id}/adapter").json()
        assert before["connected"]
        tuned = _patch(admin, station_id, note="放宽超时", config={**before["config"], "request_timeout_sec": 7})
        assert tuned.status_code == 200, tuned.text
        after = tuned.json()
        assert after["connected"] and after["acceptance"]["required"] == "", after["acceptance"]
        assert after["config_version"] == before["config_version"] + 1
        assert all(run["state"] != "queued" for run in _runs(admin, station_id)["runs"])
        # 改连接目标就不是这一类：重新握手、补验收
        moved = _patch(admin, station_id, config={**after_config(admin, station_id), "expected_device_id": "OTHER"})
        assert moved.status_code == 200 and not moved.json()["connected"]
        assert moved.json()["acceptance"]["required"] == "readonly"


def after_config(admin, station_id: str) -> dict:
    return admin.get(f"/api/stations/{station_id}/adapter").json()["config"]


def test_interrupted_readonly_run_is_queued_again(admin, credential_root, reset_runtime, station):
    """执行器在只读级验收中途重启：只读级不让设备动作，留不下什么——不改欠的级别，按当前配置重新排一次。"""
    from app.core.clock import now
    from app.core.db import SessionLocal
    from app.models import AcceptanceRun
    from app.services.acceptance_service import AcceptanceRunner

    station_id = station
    with gateway_sim(credential_root) as (_, _, port):
        _to_gateway(admin, station_id, port, credential_root)
        queued = _runs(admin, station_id)["runs"][0]
        with SessionLocal() as db:
            db.query(AcceptanceRun).filter(AcceptanceRun.id == queued["id"]).update(
                {"state": "running", "started_at": now()}, synchronize_session=False,
            )
            db.commit()
            assert AcceptanceRunner(db).interrupt_orphans() == 1
        listed = _runs(admin, station_id)
        assert listed["runs"][1]["id"] == queued["id"] and listed["runs"][1]["state"] == "error"
        assert listed["runs"][0]["state"] == "queued" and listed["runs"][0]["trigger"] == "restart"
        assert listed["gate"]["required"] == "physical", "第一次接成真实设备欠的动作级照旧"


def test_acceptance_defaults_come_from_the_template_or_the_adapter_config(admin, credential_root, reset_runtime, station):
    """转运这类参数（起止位置）极限里没有：申请验收时缺省取设备接入模板或适配器配置里的验收缺省，照样按极限核对。"""
    station_id = station
    with gateway_sim(credential_root, task_seconds=0.3) as (_, _, port):
        config, token = gateway_config(port, credential_root)
        switched = _patch(admin, station_id, kind="real", driver="http_json_v1", protocol="HTTPS JSON",
                          config={**config, "acceptance": {"capability": "cap.vacuum_dry",
                                                           "params": {"temp": 100, "vacuum": 1, "program": "VD-100"}}},
                          credential_ref=token)
        assert switched.status_code == 200, switched.text
        _station_pass(station_id)
        defaults = _runs(admin, station_id)["defaults"]
        assert defaults == {"capability": "cap.vacuum_dry", "params": {"temp": 100, "vacuum": 1, "program": "VD-100"},
                            "source": "config"}
        requested = _physical(admin, station_id)
        assert requested.status_code == 201, requested.text
        assert requested.json()["params"] == {"temp": 100, "vacuum": 1, "program": "VD-100"}
        assert admin.post(f"/api/acceptance-runs/{requested.json()['id']}/cancel").status_code == 200

        # 缺省参数落在极限外：申请时就拒绝，不拿它去动设备（验收缺省是自由字段，设备在动也能改）
        outside = _patch(admin, station_id, config={**config, "acceptance": {"params": {"temp": 500, "vacuum": 1}}})
        assert outside.status_code == 200, outside.text
        _station_pass(station_id)
        refused = _physical(admin, station_id)
        assert refused.status_code == 422 and "超出" in refused.json()["detail"]["message"], refused.text
