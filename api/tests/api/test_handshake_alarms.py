"""保存连接配置（或新登记）之后，适配器先离线、等第一次握手：这段时间不是失联，不报警；过了宽限还没握上手才报。
握过手之后再掉线照旧立刻报；改配置之前就在失联的，报警不因为保存配置而复位。执行门照样按离线挡住下发。
"""
from datetime import timedelta
from types import SimpleNamespace

STATION = "ST-05"


def _save_config(admin, base_url: str) -> dict:
    """改连接地址：改变连谁，保存后要重新握手。"""
    adapter = admin.get(f"/api/stations/{STATION}/adapter").json()
    saved = admin.patch(f"/api/stations/{STATION}/adapter", {
        "config": {**(adapter["config"] or {}), "base_url": base_url}, "row_version": adapter["row_version"],
        "signature_id": admin.sign("设备集成配置变更批准", target=STATION, object_version=adapter["row_version"]),
    })
    assert saved.status_code == 200, saved.text
    assert saved.json()["connected"] is False, "改了连谁：要重新握手"
    return saved.json()


def _alarms(operator) -> list[dict]:
    """这台工位还没关闭的失联报警。"""
    return [
        alarm for alarm in operator.get("/api/alarms").json()
        if alarm["source_id"] == STATION and alarm["condition_key"] == f"station:{STATION}:disconnected"
        and alarm["state"] != "closed"
    ]


def _waiting_since(seconds_ago: float) -> None:
    from app.core.clock import now
    from app.core.db import SessionLocal
    from app.models import Adapter

    with SessionLocal() as db:
        db.get(Adapter, STATION).awaiting_handshake_since = now() - timedelta(seconds=seconds_ago)
        db.commit()


def _heartbeat(device, connected: bool) -> None:
    sent = device.post(f"/api/runtime/stations/{STATION}/heartbeat", {"connected": connected})
    assert sent.status_code == 200, sent.text


def test_saving_a_config_and_handshaking_raises_no_alarm(admin, operator, device, reset_runtime, executor):
    _save_config(admin, "https://gw-c.lab.local")
    executor()
    executor()
    assert _alarms(operator) == [], "在等第一次握手，不是失联"
    assert STATION in operator.get("/api/gate").json()["blocked_stations"], "执行门照样按离线挡住下发"

    _heartbeat(device, True)
    executor()
    assert _alarms(operator) == []
    adapter = admin.get(f"/api/stations/{STATION}/adapter").json()
    assert adapter["connected"] is True and STATION not in operator.get("/api/gate").json()["blocked_stations"]


def test_a_config_that_never_handshakes_alarms_after_the_grace(admin, operator, device, reset_runtime, executor):
    from app.core.config import settings

    _save_config(admin, "https://gw-wrong.lab.local")
    executor()
    assert _alarms(operator) == []

    _waiting_since(settings.heartbeat_stale_sec + 1)  # 内置模拟不由执行器探测：按心跳超时给宽限
    executor()
    executor()
    raised = _alarms(operator)
    assert len(raised) == 1 and raised[0]["condition_active"], "过了宽限还没握上手：照常报失联，只报一次"
    assert "还没握上手" in raised[0]["message"], raised[0]["message"]

    _heartbeat(device, True)
    executor()
    assert not [alarm for alarm in _alarms(operator) if alarm["condition_active"]], "握上手了，条件复位"


def test_dropping_after_the_handshake_alarms_at_once(admin, operator, device, reset_runtime, executor):
    _save_config(admin, "https://gw-d.lab.local")
    _heartbeat(device, True)
    _heartbeat(device, False)  # 心跳到达的这一刻就判报警，不等宽限、不等执行器
    raised = _alarms(operator)
    assert len(raised) == 1 and raised[0]["condition_active"]
    assert "还没握上手" not in raised[0]["message"]


def test_saving_a_config_does_not_clear_an_existing_disconnect(admin, operator, device, reset_runtime, executor):
    _heartbeat(device, False)
    assert len(_alarms(operator)) == 1

    _save_config(admin, "https://gw-e.lab.local")  # 设备还是连不上：保存配置不算恢复
    executor()
    still = _alarms(operator)
    assert len(still) == 1 and still[0]["condition_active"]

    _heartbeat(device, True)
    executor()
    assert not [alarm for alarm in _alarms(operator) if alarm["condition_active"]]


def test_probed_devices_get_three_probe_cycles_and_a_probe_ends_the_wait(reset_runtime, monkeypatch):
    """执行器主动探测的设备：宽限是 3 个探测周期（至少 1 min）；探测成功就不再算在等握手。"""
    from app.core.clock import now
    from app.core.db import SessionLocal
    from app.models import Adapter
    from app.services import execution_service
    from app.services.execution_service import ExecutorLoop
    from app.services.monitoring_service import DeviceMonitor, handshake_grace

    def probed(seconds_ago: float, interval: float | None = None):
        config = {} if interval is None else {"probe_interval_sec": interval}
        return SimpleNamespace(kind="real", driver="modbus_map_v1", config=config, connected=False,
                               awaiting_handshake_since=now() - timedelta(seconds=seconds_ago))

    assert handshake_grace(probed(0)) == 60, "缺省 10 s 探测一次：至少给 1 min"
    assert handshake_grace(probed(0, interval=30)) == 90
    assert DeviceMonitor.awaiting_handshake(probed(30)) is True
    assert DeviceMonitor.awaiting_handshake(probed(61)) is False

    class Healthy:
        def healthcheck(self):
            return {"device_id": "PLC-1", "interlock": False, "accepts_commands": True}

    monkeypatch.setattr(execution_service, "adapter_for", lambda record: Healthy())
    with SessionLocal() as db:
        adapter = db.get(Adapter, STATION)
        saved = (adapter.kind, adapter.driver, adapter.config)
        adapter.kind, adapter.driver, adapter.config = "real", "modbus_map_v1", {"host": "127.0.0.1", "port": 502}
        adapter.connected, adapter.awaiting_handshake_since = False, now()
        db.commit()
        try:
            assert ExecutorLoop(db).probe_devices(STATION) == 1
            db.commit()
            db.refresh(adapter)
            assert adapter.connected is True and adapter.awaiting_handshake_since is None
        finally:
            adapter.kind, adapter.driver, adapter.config = saved
            db.commit()
