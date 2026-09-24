"""`sila2_v1` 驱动 × 外部 SiLA 2 模拟设备：真实走 gRPC，不打桩。"""
import socket
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture()
def simulator():
    from simulators.common.device import SimulatedDevice
    from simulators.sila_device.server import SimulatorRunner, parse

    port = _free_port()
    args = parse([
        "--device-id", "SIM-LH-T", "--profile", "liquid_handler", "--address", "127.0.0.1",
        "--port", str(port), "--insecure", "--task-seconds", "0.3",
    ])
    device = SimulatedDevice(
        args.device_id, args.profile, task_seconds=args.task_seconds,
        material_map={"electrolyte": {"material": "电解液 LP57", "unit": "mL", "factor": 0.001}},
    )
    runner = SimulatorRunner(args, device)
    runner.start()
    try:
        yield device, runner, port
    finally:
        runner.stop()


def _adapter(port: int, **config):
    from app.adapters.sila2 import Sila2Adapter

    record = SimpleNamespace(
        station_id="ST-SIM", protocol="SiLA 2", version="1.0", note="",
        config={"host": "127.0.0.1", "port": port, "insecure": True, "request_timeout_sec": 1, **config},
        supports_hold=True, supports_abort=True, supports_query=True, supports_dedup=True,
    )
    return Sila2Adapter(record)


def _request(command_id: str, type_: str = "dispatch", target: str = "", params=None):
    from app.adapters import CommandRequest

    return CommandRequest(
        command_id=command_id, station_id="ST-SIM", capability="cap.assemble",
        params=params if params is not None else {"wells": {"A1": {"electrolyte": 60}, "A2": {"electrolyte": 50}}},
        type=type_, batch_id="B-SIM", step_index=2, step_id="s03", target_command_id=target,
    )


def _wait_done(adapter, command_id: str, seconds: float = 5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = adapter.query(command_id)
        if result is not None and result.state in {"done", "failed"}:
            return result
        time.sleep(0.1)
    raise AssertionError("任务没有在时限内完成")


def test_identity_submit_query_and_dedup(simulator):
    device, _, port = simulator
    adapter = _adapter(port, expected_device_id="SIM-LH-T")
    health = adapter.healthcheck()
    assert health["device_id"] == "SIM-LH-T" and health["simulator"] is True

    accepted = adapter.submit(_request("CMD-1"))
    assert accepted.state == "accepted" and accepted.origin == "real:sila2_v1"
    again = adapter.submit(_request("CMD-1"))
    assert again.command_id == "CMD-1"
    assert device.executions["CMD-1"] == 1, "同一指令号只动作一次"

    done = _wait_done(adapter, "CMD-1")
    materials = done.delivered["materials"]
    assert materials[0]["material"] == "电解液 LP57"
    assert abs(materials[0]["quantity"] - 0.110) < 0.002, "按孔位实际加入量折算物料消耗"
    assert set(done.delivered["wells"]) == {"A1", "A2"}
    assert adapter.query("CMD-NEVER-SEEN") is None


def test_hold_and_abort_target_the_in_flight_task(simulator):
    device, _, port = simulator
    adapter = _adapter(port)
    device.task_seconds = 30
    adapter.submit(_request("CMD-H"))
    assert adapter.hold(_request("CMD-HOLD", "hold", target="CMD-H")).state == "done"
    assert device.tasks["CMD-H"].state == "held"
    assert adapter.abort(_request("CMD-ABORT", "abort", target="CMD-H")).state == "done"
    assert device.tasks["CMD-H"].state == "aborted"


def test_rejections_are_explicit_failures(simulator):
    from app.adapters import AdapterError

    device, _, port = simulator
    adapter = _adapter(port)
    device.set_fault("interlock")
    with pytest.raises(AdapterError, match="Interlocked"):
        adapter.submit(_request("CMD-I"))
    device.set_fault("none")
    with pytest.raises(AdapterError, match="InvalidParameters"):
        adapter.submit(_request("CMD-BAD", params={"wells": {"A1": {"electrolyte": -5}}}))
    assert "CMD-I" not in device.executions and "CMD-BAD" not in device.executions, "被拒绝的指令设备没有动作"


def test_lost_receipt_and_slow_ack_are_result_unknown(simulator):
    from app.adapters import AdapterError, AdapterUnreachable

    device, _, port = simulator
    adapter = _adapter(port)
    device.set_fault("lost_receipt")
    with pytest.raises(AdapterUnreachable) as lost:
        adapter.submit(_request("CMD-LOST"))
    assert not isinstance(lost.value, AdapterError)
    assert device.executions["CMD-LOST"] == 1, "回执丢了，但设备已经在动"
    device.set_fault("none")
    assert adapter.query("CMD-LOST").state in {"accepted", "running", "done"}, "对账能按原指令号查到"

    device.set_fault("slow_submit", 2)
    with pytest.raises(AdapterUnreachable):
        adapter.submit(_request("CMD-SLOW"))
    device.set_fault("none")


def test_offline_device_is_unreachable_then_recovers(simulator):
    from app.adapters import AdapterUnreachable

    device, runner, port = simulator
    adapter = _adapter(port, connect_timeout_sec=0.5)
    adapter.submit(_request("CMD-O"))
    runner.go_offline(1.5)
    time.sleep(0.5)
    with pytest.raises(AdapterUnreachable):
        adapter.query("CMD-O")
    # 执行器每一轮都会重试；gRPC 断线后有重连退避，按轮询的方式等它恢复
    deadline = time.monotonic() + 8
    found = None
    while time.monotonic() < deadline and found is None:
        try:
            found = adapter.query("CMD-O")
        except AdapterUnreachable:
            time.sleep(0.3)
    assert found is not None, "恢复后仍能按原指令号查到离线前的任务"


def test_production_refuses_simulated_sila_device(simulator, monkeypatch):
    from app.adapters import AdapterError
    from app.core.config import settings

    _, _, port = simulator
    adapter = _adapter(port)
    monkeypatch.setattr(settings, "environment", "production")
    with pytest.raises(AdapterError, match="模拟器"):
        adapter.healthcheck()


def test_tls_with_generated_certificate_and_ca_file(tmp_path, monkeypatch):
    """模拟设备生成自签证书；驱动用 ca_file 加密连接，缺 CA 则连不上。"""
    from app.adapters import AdapterUnreachable
    from app.adapters.sila2 import Sila2Adapter
    from app.core.config import settings
    from simulators.common.device import SimulatedDevice
    from simulators.sila_device.server import SimulatorRunner, parse

    monkeypatch.setattr(settings, "adapter_credential_root", str(tmp_path))
    port = _free_port()
    # 按主机名连接：证书必须把主机名写进 DNS SAN（容器里用服务名连接就是这种情况）
    args = parse([
        "--device-id", "SIM-TLS", "--address", "0.0.0.0", "--port", str(port),
        "--host-name", "localhost", "--cert-dir", str(tmp_path),
    ])
    runner = SimulatorRunner(args, SimulatedDevice("SIM-TLS"))
    runner.start()
    try:
        def adapter(**config):
            return Sila2Adapter(SimpleNamespace(
                station_id="ST-TLS", protocol="SiLA 2", version="1.0", note="",
                config={"host": "localhost", "port": port, "request_timeout_sec": 2, **config},
                supports_hold=True, supports_abort=True, supports_query=True, supports_dedup=True,
            ))

        health = adapter(ca_file=str(tmp_path / "SIM-TLS.crt"), expected_device_id="SIM-TLS").healthcheck()
        assert health["device_id"] == "SIM-TLS"
        with pytest.raises(AdapterUnreachable):
            adapter(connect_timeout_sec=1).healthcheck()
    finally:
        runner.stop()
