"""设备上还有可能在动作的指令时，适配器配置只放行不影响「连谁、怎么判结论」的修改。

换驱动、换连接目标之后，新的驱动实例查不回在途指令：设备侧去重的驱动查不到它，批次判故障、转人工核查；
走作业台账的映射驱动会拿旧作业去读新地址的状态，读到空闲就判完成。所以这时要拦住。
"""
from app.core.clock import now


def _acting_command(operator, state: str = "running", delivery: str = "delivered", outcome: str = ""):
    """下发一个批次，把它的第一条设备指令改成「设备正在做」的样子（内置模拟适配器会立刻完成，等不到在途）。"""
    from app.core.db import SessionLocal
    from app.models import Command

    created = operator.post("/api/batches", {"plan_id": "EP-205-01"})
    assert created.status_code == 201, created.text
    batch_id = created.json()["id"]
    assert operator.post(f"/api/batches/{batch_id}/schedule", {}).status_code == 200
    dispatched = operator.post(
        f"/api/batches/{batch_id}/dispatch",
        {"manual_review": True, "signature_id": operator.sign("批准执行", target=batch_id)},
    )
    assert dispatched.status_code == 200, dispatched.text
    with SessionLocal() as db:
        command = db.query(Command).filter(Command.batch_id == batch_id).order_by(Command.created_at).first()
        command.state, command.delivery_state, command.outcome = state, delivery, outcome
        command.started_at = now()
        db.commit()
        return batch_id, command.id, command.station_id


def _patch(admin, station_id: str, **changes):
    adapter = admin.get(f"/api/stations/{station_id}/adapter").json()
    return admin.patch(f"/api/stations/{station_id}/adapter", {
        **changes, "row_version": adapter["row_version"],
        "signature_id": admin.sign("设备集成配置变更批准", target=station_id, object_version=adapter["row_version"]),
    })


def _settle(command_id: str, state: str = "done") -> None:
    from app.core.db import SessionLocal
    from app.models import Command

    with SessionLocal() as db:
        db.get(Command, command_id).state = state
        db.commit()


def test_driver_and_target_changes_wait_for_acting_commands(admin, operator, reset_runtime):
    _, command_id, station_id = _acting_command(operator)
    original = admin.get(f"/api/stations/{station_id}/adapter").json()
    try:
        swapped = _patch(admin, station_id, kind="real", driver="http_json_v1")
        assert swapped.status_code == 409, swapped.text
        detail = swapped.json()["detail"]
        assert detail["code"] == "adapter_busy"
        assert any(command_id in row["label"] for row in detail["blocked"]), detail
        assert "模式" in detail["fields"] and "驱动" in detail["fields"]

        moved = _patch(admin, station_id, config={**original["config"], "host": "10.0.0.99"})
        assert moved.status_code == 409 and moved.json()["detail"]["fields"] == ["连接配置 host"], moved.text
        unmapped = _patch(admin, station_id, supports_query=False)
        assert unmapped.status_code == 409 and "按指令查询支持" in unmapped.json()["detail"]["fields"]

        # 说明、超时照常可改；界面保存时把没改的字段一起发回来，不算改
        tuned = _patch(
            admin, station_id, note="换班：网络慢，放宽超时", kind=original["kind"], driver=original["driver"],
            protocol=original["protocol"], credential_ref=original["credential_ref"],
            config={**original["config"], "request_timeout_sec": 30},
        )
        assert tuned.status_code == 200, tuned.text
        assert tuned.json()["config_version"] == original["config_version"] + 1

        # 指令有了结论之后放行
        _settle(command_id)
        moved = _patch(admin, station_id, config={**original["config"], "host": "10.0.0.99"})
        assert moved.status_code == 200, moved.text
    finally:
        _settle(command_id)
        _patch(admin, station_id, config=original["config"], note=original["note"])


def test_held_and_unknown_commands_count_as_acting(admin, operator, reset_runtime):
    from app.core.db import SessionLocal
    from app.models import Command

    _, command_id, station_id = _acting_command(operator, state="held")
    original = admin.get(f"/api/stations/{station_id}/adapter").json()
    try:
        assert _patch(admin, station_id, driver="sila2_v1", kind="real").status_code == 409

        # 结果未知、可能已送达：现场核查给出结论前一直算在动作
        with SessionLocal() as db:
            command = db.get(Command, command_id)
            command.state, command.delivery_state = "unknown", "maybe_sent"
            db.commit()
        blocked = _patch(admin, station_id, driver="sila2_v1", kind="real")
        assert blocked.status_code == 409, blocked.text
        assert any(command_id in row["label"] and "结果未知" in row["label"] for row in blocked.json()["detail"]["blocked"])

        # 设备明确回报失败（已停下）不算
        _settle(command_id, "failed")
        moved = _patch(admin, station_id, config={**original["config"], "host": "10.0.0.98"})
        assert moved.status_code == 200, moved.text
    finally:
        _settle(command_id, "failed")
        _patch(admin, station_id, config=original["config"])


def test_pilot_switch_refuses_stations_with_acting_commands(admin, operator, reset_runtime, capsys, monkeypatch):
    import importlib.util
    from pathlib import Path
    import sys

    root = Path(__file__).resolve().parents[3]
    spec = importlib.util.spec_from_file_location("configure_pilot_adapters", root / "scripts" / "configure-pilot-adapters.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _, command_id, station_id = _acting_command(operator)
    before = admin.get(f"/api/stations/{station_id}/adapter").json()
    try:
        monkeypatch.setattr(module.settings, "adapter_allowed_hosts", "10.20.1.0/24")
        monkeypatch.setattr(sys, "argv", [
            "configure-pilot-adapters.py", "apply", "--station", f"{station_id}=sila2_v1@10.20.1.5:50052:SIM-X",
            "--backup-dir", str(root / "api" / "test_files"),
        ])
        assert module.main() == 2
        assert command_id in capsys.readouterr().err
        after = admin.get(f"/api/stations/{station_id}/adapter").json()
        assert after["config_version"] == before["config_version"] and after["driver"] == before["driver"]
    finally:
        _settle(command_id)
