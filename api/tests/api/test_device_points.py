"""设备点位读写走真实 API：只读写点位的工位（tasks: false）只欠只读级验收；读点在 API 里做；手动写点要签名、写明原因，
执行器先读、写、再回读，前后值与签名留痕，出了结论的记录不许改。PLC 是外部模拟设备，经驱动宿主（modbus_map 插件，
真实走 Modbus TCP）用 SiLA 2 接。"""
import time

import pytest

STATION = "ST-PT-01"


def _patch(admin, station_id: str, **changes):
    adapter = admin.get(f"/api/stations/{station_id}/adapter").json()
    return admin.patch(f"/api/stations/{station_id}/adapter", {
        **changes, "row_version": adapter["row_version"],
        "signature_id": admin.sign("设备集成配置变更批准", target=station_id, object_version=adapter["row_version"]),
    })


def _pass(station_id: str = STATION) -> dict:
    from app.core.db import SessionLocal
    from app.services.execution_service import ExecutorLoop

    with SessionLocal() as db:
        return ExecutorLoop(db).station_pass(station_id)


def _write(admin, point: str, value, reason: str = "联调：手动调设定值", signature: str | None = None):
    signature = signature if signature is not None else admin.sign("手动写入设备点位", target=STATION)
    return admin.post(f"/api/stations/{STATION}/adapter/points/{point}/write",
                      {"value": value, "reason": reason, "signature_id": signature})


@pytest.fixture()
def point_station(admin, reset_runtime, tmp_path, monkeypatch):
    """一台只读写点位的 Modbus PLC，经驱动宿主接：设定值 sp_temp 可手动写（0–200 ℃），其余只读。"""
    from app.adapters.registry import reset_cache
    from app.core.config import settings
    from app.core.db import SessionLocal
    from app.models import Station
    from sim_harness import HOST_TOKEN, driver_host, free_port, plc_modbus_points, plc_sim

    monkeypatch.setattr(settings, "adapter_allowed_hosts", "127.0.0.1")
    monkeypatch.setattr(settings, "adapter_credential_root", str(tmp_path))
    (tmp_path / "host.token").write_text(HOST_TOKEN, encoding="utf-8")
    reset_cache()
    with SessionLocal() as db:
        exists = db.get(Station, STATION) is not None
    if not exists:
        created = admin.post("/api/stations", {
            "id": STATION, "name": "点位读写联调 PLC", "protocol": "sim", "limits": {},
            "adapter_kind": "simulation", "adapter_driver": "simulation",
            "signature_id": admin.sign("工程变更批准", target=STATION),
        })
        assert created.status_code == 201, created.text
    else:
        assert admin.post(f"/api/stations/{STATION}/retire", {"retired": False}).status_code == 200
    with plc_sim("modbus", setpoints=("temp",)) as (program, _, plc_port):
        points = plc_modbus_points(("temp",))
        points["sp_temp"] = {**points["sp_temp"], "writable": True, "min": 0, "max": 200, "unit": "℃"}
        device = {"plugin": "modbus_map", "port": free_port(), "simulator": True,
                  "supports": {"hold": False, "abort": False, "query": True, "dedup": True},
                  "config": {"host": "127.0.0.1", "port": plc_port, "unit_id": 1, "request_timeout_sec": 1,
                             "points": points, "identity": {"device_id": "serial", "model": "model"}}}
        with driver_host(tmp_path / "site", {"PLC-PT": device}):
            config = {"host": "127.0.0.1", "port": device["port"], "insecure": True, "tasks": False,
                      "expected_device_id": "SIM-PLC-T", "request_timeout_sec": 3}
            saved = _patch(admin, STATION, kind="real", driver="sila2_v1", protocol="SiLA 2（驱动宿主）", config=config,
                           credential_ref=f"file://{tmp_path / 'host.token'}", supports_hold=False, supports_abort=False)
            assert saved.status_code == 200, saved.text
            yield program, config, saved.json()
    _patch(admin, STATION, kind="simulation", driver="simulation", protocol="sim", config={}, credential_ref="")
    assert admin.post(f"/api/stations/{STATION}/retire", {"retired": True}).status_code == 200
    reset_cache()


def test_point_only_station_reads_and_writes_through_the_executor(admin, operator, point_station):
    program, _, saved = point_station
    assert saved["tasks"] is False and saved["points"] is True
    assert saved["acceptance"]["required"] == "readonly", "只读写点位不接指令：只欠只读级验收"
    _pass()
    gate = admin.get(f"/api/stations/{STATION}/adapter/acceptance").json()["gate"]
    assert gate["required"] == "", gate

    read = admin.get(f"/api/stations/{STATION}/adapter/points")
    assert read.status_code == 200, read.text
    rows = {row["name"]: row for row in read.json()["points"]}
    assert read.json()["tasks"] is False and rows["sp_temp"]["writable"] and rows["serial"]["value"]
    assert rows["cmd_start"]["writable"] is False

    # 权限、原因、签名、可写声明、范围：都在动设备之前挡住
    assert operator.post(f"/api/stations/{STATION}/adapter/points/sp_temp/write",
                         {"value": 50, "reason": "试试", "signature_id": "x"}).status_code == 403
    assert _write(admin, "sp_temp", 50, signature="not-a-signature").status_code == 400
    for point, value, code in (("pv_temp", 1, "point_write_refused"), ("sp_temp", 500, "point_write_refused"),
                               ("cmd_start", True, "point_write_refused")):
        refused = _write(admin, point, value)
        assert refused.status_code == 422 and refused.json()["detail"]["code"] == code, refused.text

    queued = _write(admin, "sp_temp", 42.5)
    assert queued.status_code == 201 and queued.json()["state"] == "queued", queued.text
    second = _write(admin, "sp_temp", 43)
    assert second.status_code == 409 and second.json()["detail"]["code"] == "point_write_pending"
    assert _pass()["written"] == 1

    rows = admin.get(f"/api/stations/{STATION}/adapter/point-writes").json()
    done = rows[0]
    assert (done["state"], done["value"], done["matches"]) == ("done", 42.5, True), done
    assert abs(done["after"] - 42.5) < 1e-3 and done["before"] is not None
    deadline = time.monotonic() + 2  # PLC 程序按扫描周期把寄存器同步进变量
    while abs(program.memory["SP_temp"] - 42.5) > 1e-3 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert abs(program.memory["SP_temp"] - 42.5) < 1e-3, "PLC 里的设定值真的变了"
    assert sum(program.device.executions.values()) == 0, "手动写点不启动作业"
    actions = [row["action"] for row in admin.get(f"/api/audit?target={STATION}&limit=20").json()]
    assert "申请写入设备点位" in actions and "写入设备点位" in actions, actions


def test_queued_writes_can_be_withdrawn_and_stale_ones_are_not_executed(admin, point_station):
    _, config, _ = point_station
    _pass()
    withdrawn = _write(admin, "sp_temp", 70).json()
    cancelled = admin.post(f"/api/point-writes/{withdrawn['id']}/cancel")
    assert cancelled.status_code == 200 and cancelled.json()["state"] == "cancelled", cancelled.text
    assert _pass()["written"] == 0

    stale = _write(admin, "sp_temp", 80).json()
    # 申请之后、执行之前配置变了：申请时看到的点定义可能不是现在这个，不写
    assert _patch(admin, STATION, config={**config, "request_timeout_sec": 2}).status_code == 200
    _pass()
    row = next(item for item in admin.get(f"/api/stations/{STATION}/adapter/point-writes").json()
               if item["id"] == stale["id"])
    assert row["state"] == "failed" and "重新申请" in row["error"], row


def test_finished_point_writes_cannot_be_changed_or_deleted(admin, point_station):
    from sqlalchemy import text

    from app.core.db import SessionLocal

    _pass()
    row = _write(admin, "sp_temp", 12).json()
    _pass()
    with SessionLocal() as db:
        with pytest.raises(Exception, match="禁止修改"):
            db.execute(text("UPDATE point_writes SET error = 'x' WHERE id = :id"), {"id": row["id"]})
        db.rollback()
        with pytest.raises(Exception, match="禁止删除"):
            db.execute(text("DELETE FROM point_writes WHERE id = :id"), {"id": row["id"]})
        db.rollback()


def test_drivers_without_a_point_table_say_so(admin):
    """内置模拟、HTTPS 网关没有点表：明确说明，不返回空表冒充读到了。"""
    response = admin.get("/api/stations/ST-05/adapter/points")
    assert response.status_code == 409 and response.json()["detail"]["code"] == "points_unavailable", response.text
