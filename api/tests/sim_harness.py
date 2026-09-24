"""测试用：在本进程里拉起各协议的外部模拟设备，并构造指向它们的适配器登记。

模拟设备走真实网络协议（Modbus TCP / OPC UA / HTTPS），驱动不打桩——测的就是驱动与设备之间那一段。
"""
from __future__ import annotations

from contextlib import contextmanager
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 模拟 PLC 不认参数名：能力码与参数槽位由适配器配置给出（与试点切换脚本按工位限值编号的方式一致）
MODBUS_MAP = {
    "capabilities": {"cap.test": 1, "cap.vacuum_dry": 2, "cap.weigh": 3},
    "params": {"mass": 1, "rate": 2, "temp": 3, "vacuum": 4, "vmax": 5},
}
MATERIALS = {"electrolyte": {"material": "电解液 LP57", "unit": "mL", "factor": 0.001}}


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def record(protocol: str, config: dict, credential_ref: str = "", **flags):
    values = {"supports_hold": True, "supports_abort": True, "supports_query": True, "supports_dedup": True}
    values.update(flags)
    return SimpleNamespace(
        station_id="ST-SIM", protocol=protocol, version="1.0", note="", config=config,
        credential_ref=credential_ref, **values,
    )


def request(command_id: str, type_: str = "dispatch", target: str = "", params=None, capability="cap.vacuum_dry"):
    from app.adapters import CommandRequest

    return CommandRequest(
        command_id=command_id, station_id="ST-SIM", capability=capability,
        params=params if params is not None else {"temp": 120, "vacuum": 1},
        type=type_, batch_id="B-SIM", step_index=0, step_id="s01", target_command_id=target,
    )


def _device(device_id: str, profile: str = "generic", task_seconds: float = 0.3, **kwargs):
    from simulators.common.device import SimulatedDevice

    return SimulatedDevice(device_id, profile, task_seconds=task_seconds, **kwargs)


@contextmanager
def modbus_sim(device_id: str = "SIM-MB-T", **device):
    from simulators.modbus_device.server import SimulatorRunner, parse

    port = free_port()
    args = parse(["--device-id", device_id, "--address", "127.0.0.1", "--port", str(port)])
    runner = SimulatorRunner(args, _device(device_id, **device))
    runner.start()
    try:
        yield runner.modbus.device, runner, port
    finally:
        runner.stop()


def modbus_config(port: int, **extra) -> dict:
    return {"host": "127.0.0.1", "port": port, "request_timeout_sec": 1, "connect_timeout_sec": 1,
            "probe_interval_sec": 0.5, **MODBUS_MAP, **extra}


@contextmanager
def opcua_sim(cert_dir: Path | None = None, device_id: str = "SIM-UA-T", **device):
    from simulators.opcua_device.server import SimulatorRunner, parse

    port = free_port()
    argv = ["--device-id", device_id, "--address", "127.0.0.1", "--port", str(port), "--host-name", "127.0.0.1"]
    argv += ["--cert-dir", str(cert_dir)] if cert_dir is not None else ["--insecure"]
    runner = SimulatorRunner(parse(argv), _device(device_id, **device))
    runner.start()
    try:
        yield runner.device, runner, port
    finally:
        runner.stop()


def opcua_config(port: int, cert_dir: Path | None = None, device_id: str = "SIM-UA-T", **extra) -> tuple[dict, str]:
    config = {"endpoint": f"opc.tcp://127.0.0.1:{port}/ilcs/", "request_timeout_sec": 2, "connect_timeout_sec": 1,
              "probe_interval_sec": 0.5, **extra}
    if cert_dir is None:
        return {**config, "security_policy": "None"}, ""
    return {**config, "server_certificate": str(cert_dir / f"{device_id}.crt")}, f"file://{cert_dir}/ilcs-client.json"


@contextmanager
def gateway_sim(cert_dir: Path, device_id: str = "SIM-GW-T", **device):
    from simulators.http_gateway.server import SimulatorRunner, parse

    port = free_port()
    args = parse(["--device-id", device_id, "--address", "127.0.0.1", "--port", str(port),
                  "--host-name", "localhost", "--cert-dir", str(cert_dir)])
    runner = SimulatorRunner(args, _device(device_id, **device))
    runner.start()
    try:
        yield runner.device, runner, port
    finally:
        runner.stop()


def gateway_config(port: int, cert_dir: Path, device_id: str = "SIM-GW-T", **extra) -> tuple[dict, str]:
    return (
        {"base_url": f"https://localhost:{port}/api/v1", "ca_file": str(cert_dir / f"{device_id}.crt"),
         "request_timeout_sec": 2, "connect_timeout_sec": 1, "heartbeat_mode": "probe", "probe_interval_sec": 0.5,
         "expected_device_id": device_id, **extra},
        f"file://{cert_dir}/{device_id}.token",
    )
