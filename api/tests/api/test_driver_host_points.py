"""经 SiLA 2 接驱动宿主上的 PLC：ILCS 不再自己连设备。

- 点位面板与签名手动写：权限、签名、排队、回读与审计照旧；
- 驱动配置摘要闸门：驱动项目里改了映射、只重启了驱动宿主，ILCS 自己的配置一个字没变，也要停下来重新验收，
  验收通过时批准新的那份。
"""
from contextlib import contextmanager
import json
import time

import pytest

STATION = "ST-PT-SILA"


def _patch(admin, **changes):
    adapter = admin.get(f"/api/stations/{STATION}/adapter").json()
    return admin.patch(f"/api/stations/{STATION}/adapter", {
        **changes, "row_version": adapter["row_version"],
        "signature_id": admin.sign("设备集成配置变更批准", target=STATION, object_version=adapter["row_version"]),
    })


def _pass() -> dict:
    from app.core.db import SessionLocal
    from app.services.execution_service import ExecutorLoop

    with SessionLocal() as db:
        return ExecutorLoop(db).station_pass(STATION)


def _force_probe() -> None:
    """下一轮执行器就探测（探测按周期，测试不等）。"""
    from app.core.db import SessionLocal
    from app.models import Adapter

    with SessionLocal() as db:
        db.get(Adapter, STATION).connected = False
        db.commit()


@contextmanager
def sila_station(admin, tmp_path, monkeypatch, device_port: int):
    """工位 STATION 用 sila2_v1 接驱动宿主上的设备（只读写点位，tasks: false）；退出时改回内置模拟并停用。"""
    from app.adapters.registry import reset_cache
    from app.core.config import settings
    from app.core.db import SessionLocal
    from app.models import Station
    from sim_harness import HOST_TOKEN

    monkeypatch.setattr(settings, "adapter_allowed_hosts", "127.0.0.1")
    monkeypatch.setattr(settings, "adapter_credential_root", str(tmp_path))
    (tmp_path / "host.token").write_text(HOST_TOKEN, encoding="utf-8")
    reset_cache()
    with SessionLocal() as db:
        exists = db.get(Station, STATION) is not None
    if not exists:
        created = admin.post("/api/stations", {
            "id": STATION, "name": "经驱动宿主接的 PLC", "protocol": "sim", "limits": {},
            "adapter_kind": "simulation", "adapter_driver": "simulation",
            "signature_id": admin.sign("工程变更批准", target=STATION),
        })
        assert created.status_code == 201, created.text
    else:
        assert admin.post(f"/api/stations/{STATION}/retire", {"retired": False}).status_code == 200
    config = {"host": "127.0.0.1", "port": device_port, "insecure": True, "tasks": False,
              "expected_device_id": "SIM-PLC-T", "request_timeout_sec": 3}
    saved = _patch(admin, kind="real", driver="sila2_v1", protocol="SiLA 2", config=config,
                   credential_ref=f"file://{tmp_path / 'host.token'}", supports_hold=False, supports_abort=False)
    assert saved.status_code == 200, saved.text
    try:
        yield saved.json()
    finally:
        _patch(admin, kind="simulation", driver="simulation", protocol="sim", config={}, credential_ref="")
        assert admin.post(f"/api/stations/{STATION}/retire", {"retired": True}).status_code == 200
        reset_cache()


def test_points_panel_and_signed_write_go_through_the_driver_host(admin, reset_runtime, tmp_path, monkeypatch):
    from sim_harness import driver_host, host_plc_device, plc_sim

    with plc_sim("modbus") as (program, _, plc_port):
        device = host_plc_device(plc_port, tasks=False)
        with driver_host(tmp_path / "site", {"PLC-P": device}), \
                sila_station(admin, tmp_path, monkeypatch, device["port"]) as saved:
            assert (saved["tasks"], saved["points"]) == (False, True)
            assert saved["acceptance"]["required"] == "readonly", "只读写点位不接指令：只欠只读级验收"
            _pass()
            gate = admin.get(f"/api/stations/{STATION}/adapter/acceptance").json()["gate"]
            assert gate["required"] == "", gate

            read = admin.get(f"/api/stations/{STATION}/adapter/points")
            assert read.status_code == 200, read.text
            rows = {row["name"]: row for row in read.json()["points"]}
            assert rows["serial"]["value"] == "SIM-PLC-T" and rows["sp_temp"]["writable"] and rows["sp_temp"]["unit"] == "℃"
            assert rows["cmd_start"]["writable"] is False

            refused = admin.post(f"/api/stations/{STATION}/adapter/points/sp_temp/write",
                                 {"value": 500, "reason": "越界", "signature_id": admin.sign("手动写入设备点位", target=STATION)})
            assert refused.status_code == 422 and refused.json()["detail"]["code"] == "point_write_refused", refused.text

            queued = admin.post(f"/api/stations/{STATION}/adapter/points/sp_temp/write",
                                {"value": 42.5, "reason": "联调：经驱动宿主写设定值",
                                 "signature_id": admin.sign("手动写入设备点位", target=STATION)})
            assert queued.status_code == 201, queued.text
            assert _pass()["written"] == 1
            done = admin.get(f"/api/stations/{STATION}/adapter/point-writes").json()[0]
            assert (done["state"], done["matches"]) == ("done", True), done
            assert abs(done["after"] - 42.5) < 1e-3 and done["before"] is not None
            deadline = time.monotonic() + 2
            while abs(program.memory["SP_temp"] - 42.5) > 1e-3 and time.monotonic() < deadline:
                time.sleep(0.05)
            assert abs(program.memory["SP_temp"] - 42.5) < 1e-3, "PLC 里的设定值真的变了"
            assert sum(program.device.executions.values()) == 0, "手动写点不启动作业"


def test_driver_config_change_on_the_host_reopens_the_acceptance_gate(admin, reset_runtime, tmp_path, monkeypatch):
    from app.core.db import SessionLocal
    from app.models import Alarm
    from sim_harness import host_plc_device, plc_sim, run_host, write_host_site

    site_dir = tmp_path / "site"
    with plc_sim("modbus") as (_, _, plc_port):
        device = host_plc_device(plc_port, tasks=False)
        write_host_site(site_dir, {"PLC-P": device})
        with sila_station(admin, tmp_path, monkeypatch, device["port"]):
            with run_host(site_dir) as site:
                _pass()  # 首次接入的只读级验收：通过时批准设备服务报的这一份驱动配置
                first = admin.get(f"/api/stations/{STATION}/adapter").json()
                assert first["acceptance"]["required"] == "" and first["driver_changed"] is False
                assert first["approved_driver"]["config_digest"] == site.devices[0].digest
                version = first["config_version"]

            # 在驱动项目里改一处映射（sp_temp 的上限），只重启驱动宿主
            device["config"]["points"]["sp_temp"]["max"] = 250
            write_host_site(site_dir, {"PLC-P": device})
            with run_host(site_dir) as site:
                changed_digest = site.devices[0].digest
                assert changed_digest != first["approved_driver"]["config_digest"]
                _force_probe()
                _pass()  # 探测读到新摘要：照配置变更处理——版本加一、欠验收、报警，排一次只读级验收
                gated = admin.get(f"/api/stations/{STATION}/adapter").json()
                assert gated["config_version"] == version + 1, "驱动配置变了也算配置变更：版本加一"
                assert gated["acceptance"]["required"] == "readonly" and gated["driver_changed"] is True
                runs = admin.get(f"/api/stations/{STATION}/adapter/acceptance").json()["runs"]
                assert (runs[0]["trigger"], runs[0]["state"]) == ("driver_change", "queued")

                _pass()  # 执行器跑那次只读级验收：通过时批准新的那份驱动配置，放开闸门
                accepted = admin.get(f"/api/stations/{STATION}/adapter").json()
                runs = admin.get(f"/api/stations/{STATION}/adapter/acceptance").json()["runs"]
                assert runs[0]["ok"] and runs[0]["driver_info"]["config_digest"] == changed_digest
                assert accepted["approved_driver"]["config_digest"] == changed_digest, "验收通过时批准新的那份"
                assert accepted["acceptance"]["required"] == "" and accepted["driver_changed"] is False
                with SessionLocal() as db:
                    alarm = db.query(Alarm).filter(Alarm.condition_key == f"station:{STATION}:driver_changed").one()
                    assert alarm.origin == "system" and "停派工" in alarm.message
                    alarm_id = alarm.id
                _force_probe()
                _pass()  # 再探测一次：摘要与已批准的一致，报警条件复位（确认与关闭留给人）
                with SessionLocal() as db:
                    assert db.get(Alarm, alarm_id).condition_active is False

                # 同一份配置不重复处理：再探测几次，版本不再加
                _force_probe()
                _pass()
                assert admin.get(f"/api/stations/{STATION}/adapter").json()["config_version"] == version + 1


def test_driver_change_under_a_running_command_sends_it_to_manual_review(operator, reset_runtime, executor):
    """驱动配置在指令执行期间变了：新配置可能按另一套状态码判结论，这条指令结果未知，转人工核查。换了插件要动作级验收。"""
    from app.core.clock import now
    from app.core.db import SessionLocal
    from app.models import Adapter, Command
    from app.services.execution_service import ExecutorLoop

    created = operator.post("/api/batches", {"plan_id": "EP-205-01"})
    batch_id = created.json()["id"]
    assert operator.post(f"/api/batches/{batch_id}/schedule", {}).status_code == 200
    assert operator.post(f"/api/batches/{batch_id}/dispatch", {
        "manual_review": True, "signature_id": operator.sign("批准执行", target=batch_id)}).status_code == 200
    executor()
    with SessionLocal() as db:
        command = db.query(Command).filter(Command.batch_id == batch_id).first()
        command.state, command.delivery_state = "running", "delivered"  # 设备上正在做这一步
        station_id, command_id = command.station_id, command.id
        adapter = db.get(Adapter, station_id)
        version = adapter.config_version
        adapter.approved_driver = {"plugin": "modbus_map", "config_digest": "sha256:" + "a" * 64}
        adapter.driver_info = dict(adapter.approved_driver)
        db.commit()
    try:
        with SessionLocal() as db:
            loop = ExecutorLoop(db)
            record = db.get(Adapter, station_id)
            loop._track_driver(record, {"plugin": "opcua_map", "config_digest": "sha256:" + "b" * 64}, now())
            db.commit()
        with SessionLocal() as db:
            adapter = db.get(Adapter, station_id)
            assert adapter.config_version == version + 1
            assert adapter.acceptance_required == "physical", "换了插件：下发、判结论的代码都换了，要动作级验收"
            command = db.get(Command, command_id)
            assert command.state == "unknown" and command.delivery_state == "maybe_sent"
        detail = operator.get(f"/api/batches/{batch_id}").json()
        assert detail["state"] == "fault" and "驱动配置在指令执行期间变了" in detail["failure_reason"]
    finally:
        with SessionLocal() as db:
            adapter = db.get(Adapter, station_id)
            adapter.approved_driver, adapter.driver_info, adapter.acceptance_required = {}, {}, ""
            db.commit()
