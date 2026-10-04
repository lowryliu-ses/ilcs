"""`http_json_v1` 驱动 × HTTPS 网关模拟设备（ilcs-devices/simulators/http_gateway）：真实走 TLS 与令牌，不打桩。

Modbus 任务寄存器、OPC UA TaskExecution 两个任务契约驱动已经移出 ILCS（驱动宿主的 modbus_task / opcua_task 插件，
测试在 ilcs-devices/host/tests/test_plugin_task_contract.py）。
"""
import time

import pytest

from sim_harness import gateway_config, gateway_sim, record, request


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


def _gateway(port: int, cert_dir, credential=None, **config):
    from app.adapters.drivers.http_json import HttpJsonAdapter

    settings, token = gateway_config(port, cert_dir, **config)
    return HttpJsonAdapter(record("HTTPS JSON", settings, token if credential is None else credential))


def test_gateway_tls_token_submit_query_and_dedup(credential_root):
    with gateway_sim(credential_root) as (device, _, port):
        adapter = _gateway(port, credential_root)
        health = adapter.healthcheck()
        assert (health["device_id"], health["simulator"], health["interlock"]) == ("SIM-GW-T", True, False)
        assert adapter.submit(request("CMD-G1")).state == "running", "网关（SDK）在设备确认开始后回 running"
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
