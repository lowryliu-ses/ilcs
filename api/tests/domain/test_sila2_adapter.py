"""`sila2_v1` 驱动 × 外部 SiLA 2 模拟设备：真实走 gRPC，不打桩。"""
import socket
import threading
import time
from types import SimpleNamespace

import pytest

from sim_harness import needs_devices  # SiLA 2 模拟设备在设备仓库（simulators/sila_device）；导入时把它放进 sys.path

pytestmark = needs_devices


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
    from app.adapters.drivers.sila2 import Sila2Adapter

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


def test_context_carries_station_and_material(simulator):
    """TaskExecution 1.1：上下文和 http_json_v1 的请求体一样带工位，投料步骤再带物料。"""
    from dataclasses import replace

    device, _, port = simulator
    adapter = _adapter(port)
    material = {"name": "电解液 LP57", "unit": "mL", "param": "electrolyte"}
    adapter.submit(replace(_request("CMD-CTX"), material=material))
    context = device.tasks["CMD-CTX"].context
    assert context["station_id"] == "ST-SIM" and context["material"] == material
    _wait_done(adapter, "CMD-CTX")
    adapter.submit(_request("CMD-PLAIN"))
    assert "material" not in device.tasks["CMD-PLAIN"].context, "不投料的步骤不带物料"


def test_simulator_reports_task_support(simulator):
    """模拟设备实现 TaskExecution 1.1 新增的 TaskSupport：任意能力、四种控制都支持、方法目录与身份一致。"""
    from sila2.client import SilaClient

    _, _, port = simulator
    support = SilaClient("127.0.0.1", port, insecure=True).TaskExecution.TaskSupport.get()
    assert [row.Capability for row in support.Capabilities] == ["*"]
    assert [program.Program for program in support.Capabilities[0].Programs] == ["*"]
    assert support.SupportsHold and support.SupportsAbort and support.SupportsQuery and support.SupportsDedup
    assert support.Handoff == "sync"


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
    from app.adapters.drivers.sila2 import Sila2Adapter
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


@pytest.fixture()
def blackhole():
    """接了 TCP 连接却一个字节都不回的「设备服务」（卡死的网关、端口被别的程序占着）。"""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(16)
    server.settimeout(0.2)
    held, stop = [], threading.Event()

    def accept():
        while not stop.is_set():
            try:
                held.append(server.accept()[0])
            except OSError:
                continue

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    try:
        yield server.getsockname()[1]
    finally:
        stop.set()
        thread.join(2)
        for connection in held:
            connection.close()  # 挂着的握手随之失败、线程结束
        server.close()


def test_unresponsive_service_fails_by_deadline_without_blocking_other_stations(simulator, blackhole):
    """接了连接却不应答：握手（读特性清单）按「连接超时 + 请求超时」判连不上，不再等 gRPC 自己放弃（约 20 s）；
    全局锁只管编译，这次握手挂着时别的工位照常首次连接；同一台再连时接着等原来那次握手，不另起线程。"""
    from app.adapters import AdapterUnreachable
    from app.adapters.drivers.sila2 import Sila2Adapter

    _, _, port = simulator
    stuck = Sila2Adapter(SimpleNamespace(
        station_id="ST-HOLE", protocol="SiLA 2", version="1.0", note="",
        config={"host": "127.0.0.1", "port": blackhole, "insecure": True,
                "connect_timeout_sec": 0.2, "request_timeout_sec": 0.3},
        supports_hold=True, supports_abort=True, supports_query=True, supports_dedup=True,
    ))
    started = time.monotonic()
    with pytest.raises(AdapterUnreachable, match="握手"):
        stuck.healthcheck()
    assert time.monotonic() - started < 2

    started = time.monotonic()
    assert _adapter(port).healthcheck()["device_id"] == "SIM-LH-T"
    assert time.monotonic() - started < 5, "别的工位的首次连接不能排在卡住的握手后面"

    with pytest.raises(AdapterUnreachable, match="握手"):
        stuck.healthcheck()
    assert len([t for t in threading.enumerate() if t.name == "sila2-handshake-ST-HOLE"]) == 1


def test_parallel_first_connections_compile_safely(simulator):
    """几个工位同时首次连接：sila2 现场编译特性的那一步串行（并发编译会偶发 KeyError、被误判成连不上），其余并行。"""
    _, _, port = simulator
    adapters = [_adapter(port) for _ in range(6)]
    outcomes: list = [None] * len(adapters)

    def connect(index):
        try:
            outcomes[index] = adapters[index].healthcheck()["device_id"]
        except Exception as exc:  # noqa: BLE001
            outcomes[index] = exc

    threads = [threading.Thread(target=connect, args=(index,)) for index in range(len(adapters))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert outcomes == ["SIM-LH-T"] * len(adapters)
