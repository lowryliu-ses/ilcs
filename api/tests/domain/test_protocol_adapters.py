"""`modbus_tcp_v1` / `opcua_v1` / `http_json_v1` 驱动 × 各自的外部模拟设备：真实走协议，不打桩。

三个协议共用一套设备行为，所以每个协议都验同一组结论：身份与去重、明确拒绝时设备没动、
回执丢失与应答迟到判结果未知（设备只动作一次、对账能按原指令号查到）、离线后恢复。
"""
import time

import pytest

from sim_harness import (
    MATERIALS, gateway_config, gateway_sim, modbus_config, modbus_sim, opcua_config, opcua_sim, record, request,
)


def _wait_done(adapter, command_id: str, device, seconds: float = 5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        device.tick()
        result = adapter.query(command_id)
        if result is not None and result.state in {"done", "failed"}:
            return result
        time.sleep(0.1)
    raise AssertionError("任务没有在时限内完成")


@pytest.fixture()
def credential_root(tmp_path, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "adapter_credential_root", str(tmp_path))
    return tmp_path


# ---------- Modbus TCP ----------

def _modbus(port: int, **config):
    from app.adapters.modbus_tcp import ModbusTcpAdapter

    return ModbusTcpAdapter(record("Modbus TCP", modbus_config(port, **config)))


def test_modbus_identity_submit_query_and_dedup():
    with modbus_sim() as (device, _, port):
        adapter = _modbus(port, expected_device_id="SIM-MB-T", material_map={"vacuum": MATERIALS["electrolyte"]})
        health = adapter.healthcheck()
        assert (health["device_id"], health["simulator"], health["accepts_commands"]) == ("SIM-MB-T", True, True)

        accepted = adapter.submit(request("CMD-M1"))
        assert accepted.state == "accepted" and accepted.origin == "real:modbus_tcp_v1"
        assert adapter.submit(request("CMD-M1")).command_id == "CMD-M1"
        assert device.executions["CMD-M1"] == 1, "同一指令号重复触发，设备只动作一次"
        assert device.tasks["CMD-M1"].params == {"p3": 120.0, "p4": 1.0}, "参数按配置的槽位写进寄存器"

        done = _wait_done(adapter, "CMD-M1", device)
        assert done.state == "done"
        assert abs(done.delivered["temp"] - 120) < 1 and done.delivered["temp"] != 120, "槽位实测值按参数名回报"
        assert {point[0] for point in done.telemetry} == {"temp", "vacuum"}
        assert done.delivered["materials"][0]["material"] == "电解液 LP57"
        assert adapter.query("CMD-NEVER-SEEN") is None


def test_modbus_refuses_what_registers_cannot_express_before_touching_the_device():
    from app.adapters import AdapterError

    with modbus_sim() as (device, _, port):
        adapter = _modbus(port)
        with pytest.raises(AdapterError, match="结构化参数"):
            adapter.submit(request("CMD-W", params={"wells": {"A1": {"temp": 1}}}))
        with pytest.raises(AdapterError, match="没有映射到寄存器槽位"):
            adapter.submit(request("CMD-P", params={"speed": 3}))
        with pytest.raises(AdapterError, match="能力码"):
            adapter.submit(request("CMD-C", capability="cap.coat"))
        assert not device.executions, "编码不了的指令根本不写触发"


def test_modbus_rejections_are_explicit_failures():
    from app.adapters import AdapterError

    with modbus_sim() as (device, _, port):
        adapter = _modbus(port)
        device.set_fault("interlock")
        time.sleep(0.3)  # 身份区按 PLC 扫描周期刷新
        assert adapter.healthcheck()["interlock"] is True
        with pytest.raises(AdapterError, match="Interlocked"):
            adapter.submit(request("CMD-I"))
        device.set_fault("none")
        with pytest.raises(AdapterError, match="InvalidParameters"):
            adapter.submit(request("CMD-BAD", params={"temp": -5}))
        assert "CMD-I" not in device.executions and "CMD-BAD" not in device.executions


def test_modbus_lost_ack_is_result_unknown_and_next_command_still_goes_through():
    """回执丢失后触发寄存器已经是新序号：换一个驱动实例（执行器重启）也必须从更大的序号接着编，
    否则设备看不到触发变化，下一条指令永远不被处理。"""
    from app.adapters import AdapterError, AdapterUnreachable

    with modbus_sim() as (device, _, port):
        adapter = _modbus(port)
        device.set_fault("lost_receipt")
        with pytest.raises(AdapterUnreachable) as lost:
            adapter.submit(request("CMD-LOST"))
        assert not isinstance(lost.value, AdapterError)
        assert device.executions["CMD-LOST"] == 1, "应答没写，但设备已经在动"
        device.set_fault("none")
        assert adapter.query("CMD-LOST").state in {"accepted", "running", "done"}, "对账能按原指令号查到"

        restarted = _modbus(port)
        assert restarted.submit(request("CMD-NEXT")).state == "accepted"

        device.set_fault("slow_submit", 2)
        with pytest.raises(AdapterUnreachable, match="结果未知"):
            restarted.submit(request("CMD-SLOW"))
        device.set_fault("none")


def test_modbus_hold_abort_and_partial_execution():
    with modbus_sim(task_seconds=30) as (device, _, port):
        adapter = _modbus(port)
        adapter.submit(request("CMD-H"))
        assert adapter.hold(request("CMD-HOLD", "hold", target="CMD-H")).state == "done"
        assert device.tasks["CMD-H"].state == "held"
        assert adapter.abort(request("CMD-ABORT", "abort", target="CMD-H")).state == "done"
        aborted = adapter.query("CMD-H")
        assert aborted.state == "failed" and "终止" in aborted.error


def test_modbus_device_owned_registers_are_read_only():
    from pymodbus.client import ModbusTcpClient

    with modbus_sim() as (_, _, port):
        client = ModbusTcpClient("127.0.0.1", port=port, timeout=1, retries=0)
        client.connect()
        try:
            assert client.write_registers(200, [7]).isError(), "应答块只能由设备写"
            assert not client.write_registers(100, [0]).isError(), "指令邮箱允许主站写"
        finally:
            client.close()


def test_modbus_offline_then_recovers_and_stalled_heartbeat_is_lost():
    from app.adapters import AdapterUnreachable

    with modbus_sim() as (_, runner, port):
        adapter = _modbus(port, heartbeat_stale_sec=0.3)
        adapter.submit(request("CMD-O"))
        runner.go_offline(1)
        time.sleep(0.3)
        with pytest.raises(AdapterUnreachable):
            adapter.query("CMD-O")
        deadline = time.monotonic() + 5
        found = None
        while found is None and time.monotonic() < deadline:
            try:
                found = adapter.query("CMD-O")
            except AdapterUnreachable:
                time.sleep(0.2)
        assert found is not None, "恢复后仍能按原指令号查到离线前的任务"

        # 寄存器能读但心跳计数不再变化：PLC 程序停了，按失联处理
        runner.modbus.refresh = lambda: None
        watcher = _modbus(port, heartbeat_stale_sec=0.3)
        watcher.healthcheck()
        time.sleep(0.4)
        with pytest.raises(AdapterUnreachable, match="心跳"):
            watcher.healthcheck()


# ---------- OPC UA ----------

def _opcua(port: int, cert_dir=None, **config):
    from app.adapters.opcua import OpcUaAdapter

    settings, credential = opcua_config(port, cert_dir, **config)
    return OpcUaAdapter(record("OPC UA", settings, credential))


def test_opcua_encrypted_session_submit_query_and_dedup(credential_root):
    with opcua_sim(credential_root, profile="liquid_handler", material_map=MATERIALS) as (device, _, port):
        adapter = _opcua(port, credential_root, expected_device_id="SIM-UA-T")
        health = adapter.healthcheck()
        assert health["security"] == "Basic256Sha256/SignAndEncrypt" and health["simulator"] is True

        wells = {"wells": {"A1": {"electrolyte": 60}, "A2": {"electrolyte": 50}}}
        assert adapter.submit(request("CMD-U1", params=wells)).origin == "real:opcua_v1"
        adapter.submit(request("CMD-U1", params=wells))
        assert device.executions["CMD-U1"] == 1
        done = _wait_done(adapter, "CMD-U1", device)
        assert set(done.delivered["wells"]) == {"A1", "A2"}, "JSON 参数原样到达，孔位矩阵照常执行"
        assert abs(done.delivered["materials"][0]["quantity"] - 0.110) < 0.002
        assert adapter.query("CMD-NEVER-SEEN") is None


def test_opcua_refuses_unpinned_or_untrusted_peers(credential_root, tmp_path_factory):
    from app.adapters import AdapterError, AdapterUnreachable
    from app.core.config import settings
    from simulators.common.certs import ensure_certificate, self_signed_certificate

    with opcua_sim(credential_root) as (_, _, port):
        from app.adapters.opcua import OpcUaAdapter

        config, credential = opcua_config(port, credential_root)
        with pytest.raises(AdapterError, match="server_certificate"):
            OpcUaAdapter(record("OPC UA", {**config, "server_certificate": ""}, credential))
        # 另一张客户端证书（不在模拟设备的信任名单里）：握手被拒
        stranger = credential_root / "stranger"
        ensure_certificate(stranger, "client", lambda: self_signed_certificate(
            "ilcs", "ILCS", application_uri="urn:ilcs:client", client=True))
        (stranger / "client.json").write_text('{"certificate": "client.crt", "private_key": "client.key"}')
        with pytest.raises(AdapterUnreachable):
            OpcUaAdapter(record("OPC UA", config, f"file://{stranger}/client.json")).healthcheck()

        settings_env = settings.environment
        try:
            settings.environment = "production"
            with pytest.raises(AdapterError, match="不加密"):
                _opcua(port)
        finally:
            settings.environment = settings_env


def test_opcua_rejections_unknown_results_and_recovery():
    from app.adapters import AdapterError, AdapterIndeterminate, AdapterUnreachable

    with opcua_sim() as (device, runner, port):
        adapter = _opcua(port)
        device.set_fault("busy")
        with pytest.raises(AdapterError, match="DeviceBusy"):
            adapter.submit(request("CMD-B"))
        device.set_fault("lost_receipt")
        with pytest.raises(AdapterIndeterminate):
            adapter.submit(request("CMD-LOST"))
        device.set_fault("none")
        assert device.executions["CMD-LOST"] == 1 and "CMD-B" not in device.executions
        assert adapter.query("CMD-LOST") is not None, "对账能按原指令号查到"

        device.set_fault("slow_submit", 3)
        with pytest.raises(AdapterUnreachable):
            adapter.submit(request("CMD-SLOW"))
        device.set_fault("none")

        adapter.submit(request("CMD-O"))
        runner.go_offline(1)
        time.sleep(0.5)
        with pytest.raises(AdapterUnreachable):
            adapter.query("CMD-O")
        deadline = time.monotonic() + 8
        found = None
        while found is None and time.monotonic() < deadline:
            try:
                found = adapter.query("CMD-O")
            except AdapterUnreachable:
                time.sleep(0.3)
        assert found is not None, "服务器重启后重建会话，仍能查到离线前的任务"


# ---------- HTTPS JSON 网关 ----------

def _gateway(port: int, cert_dir, credential=None, **config):
    from app.adapters.http_json import HttpJsonAdapter

    settings, token = gateway_config(port, cert_dir, **config)
    return HttpJsonAdapter(record("HTTPS JSON", settings, token if credential is None else credential))


def test_gateway_tls_token_submit_query_and_dedup(credential_root):
    with gateway_sim(credential_root) as (device, _, port):
        adapter = _gateway(port, credential_root)
        health = adapter.healthcheck()
        assert (health["device_id"], health["simulator"], health["interlock"]) == ("SIM-GW-T", True, False)
        assert adapter.submit(request("CMD-G1")).state == "accepted"
        adapter.submit(request("CMD-G1"))
        assert device.executions["CMD-G1"] == 1
        assert _wait_done(adapter, "CMD-G1", device).state == "done"
        assert adapter.query("CMD-NEVER-SEEN") is None


def test_gateway_rejections_wrong_token_and_lost_receipt(credential_root):
    from app.adapters import AdapterError, AdapterUnreachable

    with gateway_sim(credential_root) as (device, _, port):
        wrong = credential_root / "wrong.token"
        wrong.write_text("not-the-token")
        with pytest.raises(AdapterError, match="401"):
            _gateway(port, credential_root, credential=f"file://{wrong}").healthcheck()

        adapter = _gateway(port, credential_root)
        device.set_fault("interlock")
        with pytest.raises(AdapterError, match="423"):
            adapter.submit(request("CMD-I"))
        device.set_fault("lost_receipt")
        with pytest.raises(AdapterUnreachable) as lost:
            adapter.submit(request("CMD-LOST"))
        assert not isinstance(lost.value, AdapterError), "连接被断开：结果未知，不是明确失败"
        device.set_fault("none")
        assert device.executions["CMD-LOST"] == 1 and "CMD-I" not in device.executions
        assert adapter.query("CMD-LOST") is not None
