"""驱动宿主测试：在本进程里拉起外部 PLC 模拟设备（走真实 Modbus TCP / OPC UA），起宿主，用 SiLA 客户端带令牌调用。

运行：`api/.venv/bin/python -m pytest devices/host/tests`（用 ILCS 的虚拟环境，依赖是同一套）。
"""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import socket
import sys
import threading

import pytest

DEVICES = Path(__file__).resolve().parents[2]
for path in (DEVICES, DEVICES / "host"):  # simulators 包以 devices/ 为根，ilcs_host 以 devices/host 为根
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

TOKEN = "t" * 40
PLC_STATES = {"0": "idle", "1": "running", "2": "held", "3": "done", "4": "failed"}
PLC_ERRORS = {"17": "过程报警", "23": "执行中断", "90": "安全回路未闭合", "91": "不在远程模式", "92": "上一作业未复位"}


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@contextmanager
def silent_port():
    """只收连接、从不答话的端口：设备进程卡死、容器被暂停时就是这样（内核替它建连接，请求没有回音）。"""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(64)
        yield listener.getsockname()[1]

class FreezableProxy:
    """转发到本机某个端口的 TCP 代理。`freeze()` 之后一个字节都不再转发，连接还挂着：拔了网线、交换机断了就是这样
    （没有 RST，客户端只能等超时）。"""

    def __init__(self, target_port: int):
        self.target_port = target_port
        self.frozen = threading.Event()
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(16)
        self.port = self.listener.getsockname()[1]
        self.sockets: list[socket.socket] = []
        threading.Thread(target=self._accept, daemon=True).start()

    def freeze(self) -> None:
        self.frozen.set()

    def _accept(self) -> None:
        while True:
            try:
                inbound, _ = self.listener.accept()
            except OSError:
                return
            outbound = socket.create_connection(("127.0.0.1", self.target_port))
            self.sockets += [inbound, outbound]
            for source, target in ((inbound, outbound), (outbound, inbound)):
                threading.Thread(target=self._pump, args=(source, target), daemon=True).start()

    def _pump(self, source: socket.socket, target: socket.socket) -> None:
        while True:
            try:
                data = source.recv(65536)
            except OSError:
                return
            if not data:
                return
            if not self.frozen.is_set():
                try:
                    target.sendall(data)
                except OSError:
                    return

    def close(self) -> None:
        for sock in [self.listener, *self.sockets]:
            try:
                sock.close()
            except OSError:
                pass


@contextmanager
def freezable_proxy(target_port: int):
    proxy = FreezableProxy(target_port)
    try:
        yield proxy
    finally:
        proxy.close()



def modbus_points(setpoints=("thickness", "temp")) -> dict:
    points = {
        "heartbeat": {"table": "holding", "address": 0, "type": "uint16"},
        "state": {"table": "holding", "address": 1, "type": "uint16"},
        "error": {"table": "holding", "address": 2, "type": "uint16"},
        "remote": {"table": "holding", "address": 3, "type": "bool", "bit": 0},
        "safety": {"table": "holding", "address": 3, "type": "bool", "bit": 1},
        "job_id": {"table": "holding", "address": 100, "type": "ascii", "length": 40},
        "job_latched": {"table": "holding", "address": 120, "type": "ascii", "length": 40},
        "vendor": {"table": "holding", "address": 200, "type": "ascii", "length": 32},
        "model": {"table": "holding", "address": 216, "type": "ascii", "length": 32},
        "serial": {"table": "holding", "address": 232, "type": "ascii", "length": 32},
        "firmware": {"table": "holding", "address": 248, "type": "ascii", "length": 16},
        **{name: {"table": "coil", "address": index, "type": "bool"}
           for index, name in enumerate(("cmd_start", "cmd_hold", "cmd_resume", "cmd_abort", "cmd_ack"))},
    }
    for index, name in enumerate(setpoints):
        points[f"sp_{name}"] = {"table": "holding", "address": 10 + 2 * index, "type": "float32"}
        points[f"pv_{name}"] = {"table": "holding", "address": 50 + 2 * index, "type": "float32"}
    return points


def plc_mapping(points: dict, setpoints=("thickness", "temp"), capability="cap.coat") -> dict:
    pulse = {"value": True, "pulse_ms": 150}
    return {
        "points": points,
        "identity": {"device_id": "serial", "model": "model", "vendor": "vendor", "firmware": "firmware"},
        "ready": {"point": "remote", "ok": [True]}, "interlock": {"point": "safety", "ok": [True]},
        "heartbeat": {"point": "heartbeat", "stale_sec": 5},
        "job_id": {"write": "job_id", "echo": "job_latched"},
        "capabilities": {capability: {
            "write": {name: f"sp_{name}" for name in setpoints},
            "start": {"point": "cmd_start", "value": True, "pulse_ms": 150},
            "actuals": {name: f"pv_{name}" for name in setpoints},
        }},
        "status": {"point": "state", "states": PLC_STATES},
        "error": {"point": "error", "codes": PLC_ERRORS},
        "start_refused": {"codes": ["90", "91", "92"]},
        "hold": {"point": "cmd_hold", **pulse}, "resume": {"point": "cmd_resume", **pulse},
        "abort": {"point": "cmd_abort", **pulse}, "acknowledge": {"point": "cmd_ack", **pulse},
    }


@contextmanager
def plc_sim(protocol: str = "modbus", task_seconds: float = 0.4, device_id: str = "SIM-PLC-T"):
    from simulators.common.device import SimulatedDevice
    from simulators.plc_device.server import SimulatorRunner, parse

    port = free_port()
    argv = ["--protocol", protocol, "--device-id", device_id, "--address", "127.0.0.1", "--port", str(port),
            "--machine", "Coater", "--setpoints", "thickness,temp", "--host-name", "127.0.0.1", "--insecure"]
    runner = SimulatorRunner(parse(argv), SimulatedDevice(device_id, "generic", task_seconds=task_seconds))
    runner.start()
    try:
        yield runner.program, port
    finally:
        runner.stop()


def write_site(root: Path, devices: dict[str, dict], *, tokens: bool = True, **host) -> Path:
    """一个现场目录：host.json + devices/<设备>.json；状态、凭据都放在 root 下。"""
    (root / "devices").mkdir(parents=True, exist_ok=True)
    settings = {"name": "test", "environment": "development", "address": "127.0.0.1",
                "allowed_hosts": "127.0.0.1,localhost", "state_dir": "state", "credential_root": "secrets", **host}
    if tokens:
        (root / "tokens.txt").write_text(f"# ILCS\n{TOKEN}\n", encoding="utf-8")
        settings["tokens_file"] = "tokens.txt"
    (root / "host.json").write_text(json.dumps(settings, ensure_ascii=False), encoding="utf-8")
    for key, device in devices.items():
        (root / "devices" / f"{key}.json").write_text(json.dumps(device, ensure_ascii=False), encoding="utf-8")
    return root


@contextmanager
def running_host(site_dir: Path):
    from ilcs_host.plugins import PLUGINS
    from ilcs_host.server import prepare, start, stop
    from ilcs_host.site import load_site

    site = load_site(site_dir, set(PLUGINS))
    servers = start(site, prepare(site))
    try:
        yield site
    finally:
        stop(servers)


def client(port: int):
    from sila2.client import SilaClient

    return SilaClient("127.0.0.1", port, insecure=True)


def token(sila_client, value: str = TOKEN) -> list:
    return [sila_client.AuthorizationService.AccessToken(value)]


@pytest.fixture()
def plc():
    with plc_sim() as running:
        yield running
