"""驱动宿主插件测试用：在本进程里拉起各协议的外部模拟设备，并构造指向它们的设备登记（从 ILCS 的 api/tests/sim_harness.py
搬来，插件从 ILCS 搬到驱动宿主时测试跟着搬）。

模拟设备走真实网络协议（Modbus TCP / OPC UA / HTTP / TCP 文本命令），插件不打桩——测的就是插件与设备之间那一段。
"""
from __future__ import annotations

from contextlib import contextmanager
import socket
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

DEVICES = Path(__file__).resolve().parents[2]  # simulators 包以 devices/ 为根，ilcs_host 以 devices/host 为根
for path in (DEVICES, DEVICES / "host"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

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



def record(protocol: str, config: dict, credential_ref: str = "", **flags):
    values = {"supports_hold": True, "supports_abort": True, "supports_query": True, "supports_dedup": True}
    values.update(flags)
    return SimpleNamespace(
        station_id="ST-SIM", protocol=protocol, version="1.0", note="", config=config,
        credential_ref=credential_ref, **values,
    )


def request(command_id: str, type_: str = "dispatch", target: str = "", params=None, capability="cap.vacuum_dry",
            program: str = ""):
    from ilcs_host.plugins.base import CommandRequest

    return CommandRequest(
        command_id=command_id, station_id="ST-SIM", capability=capability,
        params=params if params is not None else {"temp": 120, "vacuum": 1},
        type=type_, batch_id="B-SIM", step_index=0, step_id="s01", target_command_id=target,
        method={"program": program} if program else {},
    )


@contextmanager
def control_port(target, token: str = ""):
    """模拟设备统一控制口（devices/simulators/common/control.py），接入验收的故障注入与动作计数走它。"""
    from simulators.common.control import ControlServer

    server = ControlServer(target, "127.0.0.1", 0, token).start()
    try:
        yield server.port
    finally:
        server.stop()


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


# ---------- 没有 ILCS 任务契约的设备：串口 / TCP 命令 ----------

# 真空干燥箱温控仪表（devices/simulators/line_device --dialect oven）的命令映射
OVEN_MAP = {
    "identity": {"send": "*IDN?", "pattern": "^(?P<vendor>[^,]*),(?P<model>[^,]*),(?P<device_id>[^,]*),(?P<firmware>.*)$"},
    "ready": {"send": "REM?", "pattern": "^(?P<value>\\w+)$", "ok": ["REMOTE"]},
    "interlock": {"send": "DOOR?", "pattern": "^(?P<value>\\w+)$", "ok": ["CLOSED"]},
    "error_pattern": "^ERR",
    "capabilities": {"cap.vacuum_dry": {"program": "VD-120", "start": [
        {"send": "PROG {program}", "expect": "^OK$"},
        {"send": "SP {temp:.1f}", "expect": "^OK$"},
        {"send": "VAC {vacuum:.2f}", "expect": "^OK$"},
        {"send": "RUN", "expect": "^OK$"},
    ]}},
    "status": {"send": "STAT?", "pattern": "^(?P<state>[A-Z]+)(,(?P<detail>.*))?$",
               "states": {"IDLE": "idle", "RUN": "running", "HOLD": "held", "DONE": "done", "ALARM": "failed"}},
    "error_codes": {"E05": "干燥过程报警：温度偏差超限", "E07": "真空泵停机：干燥中断"},
    "actuals": [{"send": "PV?", "pattern": "^(?P<temp>[-\\d.]+),(?P<vacuum>[-\\d.]+)$"}],
    "hold": [{"send": "HOLD", "expect": "^OK$"}],
    "resume": [{"send": "CONT", "expect": "^OK$"}],
    "abort": [{"send": "STOP", "expect": "^OK$"}],
    "acknowledge": [{"send": "ACK", "expect": "^OK$"}],
}

# UR 仪表盘服务（devices/simulators/line_device --dialect ur）
UR_MAP = {
    "write_terminator": "\n", "read_terminator": "\n",
    "greeting": "^Connected: Universal Robots Dashboard Server",
    "identity": [
        {"send": "get serial number", "pattern": "^(?P<serial>\\S+)$"},
        {"send": "get robot model", "pattern": "^(?P<model>.+)$"},
        {"send": "PolyscopeVersion", "pattern": "^URSoftware (?P<firmware>.+)$"},
    ],
    "ready": {"send": "robotmode", "pattern": "^Robotmode: (?P<value>\\w+)$", "ok": ["RUNNING"]},
    "interlock": {"send": "safetystatus", "pattern": "^Safetystatus: (?P<value>\\w+)$", "ok": ["NORMAL", "REDUCED"]},
    "error_pattern": "^(Failed to execute|File not found|Error|could not understand)",
    "capabilities": {"cap.robot_load": {"program": "load_glovebox", "start": [
        {"send": "load /programs/{program}.urp", "expect": "^Loading program"},
        {"send": "play", "expect": "^Starting program"},
    ]}},
    "status": {"send": "programState", "pattern": "^(?P<state>PLAYING|PAUSED|STOPPED)",
               "states": {"PLAYING": "running", "PAUSED": "held", "STOPPED": "idle"}},
    "hold": [{"send": "pause", "expect": "^Pausing program"}],
    "resume": [{"send": "play", "expect": "^Starting program"}],
    "abort": [{"send": "stop", "expect": "^Stopped"}],
}


@contextmanager
def line_sim(dialect: str = "oven", device_id: str = "SIM-OVEN-T", **device):
    from simulators.line_device.server import SimulatorRunner, parse

    port = free_port()
    args = parse(["--dialect", dialect, "--device-id", device_id, "--address", "127.0.0.1", "--port", str(port)])
    runner = SimulatorRunner(args, _device(device_id, **device))
    runner.start()
    try:
        yield runner.device, runner, port
    finally:
        runner.stop()


def line_config(port: int, dialect: str = "oven", **extra) -> dict:
    mapping = OVEN_MAP if dialect == "oven" else UR_MAP
    return {"transport": {"kind": "tcp", "host": "127.0.0.1", "port": port}, "request_timeout_sec": 1,
            "connect_timeout_sec": 1, "probe_interval_sec": 0.5, **mapping, **extra}


# ---------- PLC 点表（devices/simulators/plc_device）：OPC UA 节点映射 / Modbus 点表映射 ----------

PLC_STATES = {"0": "idle", "1": "running", "2": "held", "3": "done", "4": "failed"}
PLC_ERRORS = {"17": "过程报警", "23": "执行中断", "31": "程序号不存在", "90": "安全回路未闭合", "91": "不在远程模式",
              "92": "上一作业未复位"}


def _plc_mapping(points: dict, setpoints: tuple, capability: str, recipes: dict | None) -> dict:
    spec = {
        "write": {name: f"sp_{name}" for name in setpoints},
        "start": {"point": "cmd_start", "value": True, "pulse_ms": 150},
        "actuals": {name: f"pv_{name}" for name in setpoints},
    }
    if recipes is not None:
        spec["recipe"] = {"point": "recipe", "map": recipes}
    pulse = {"value": True, "pulse_ms": 150}
    return {
        "points": points,
        "identity": {"device_id": "serial", "model": "model", "vendor": "vendor", "firmware": "firmware"},
        "ready": {"point": "remote", "ok": [True]}, "interlock": {"point": "safety", "ok": [True]},
        "heartbeat": {"point": "heartbeat", "stale_sec": 5},
        "job_id": {"write": "job_id", "echo": "job_latched"},
        "capabilities": {capability: spec},
        "status": {"point": "state", "states": PLC_STATES},
        "error": {"point": "error", "codes": PLC_ERRORS},
        "start_refused": {"codes": ["90", "91", "92"]},
        "hold": {"point": "cmd_hold", **pulse}, "resume": {"point": "cmd_resume", **pulse},
        "abort": {"point": "cmd_abort", **pulse}, "acknowledge": {"point": "cmd_ack", **pulse},
    }


def plc_opcua_points(machine: str, setpoints: tuple) -> dict:
    prefix = f"nsu=urn:ilcs:sim:plc;s={machine}."
    names = {"state": "State", "error": "ErrorCode", "heartbeat": "Heartbeat", "remote": "RemoteMode",
             "safety": "SafetyOk", "serial": "SerialNo", "model": "Model", "vendor": "Vendor", "firmware": "Firmware",
             "cmd_start": "CmdStart", "cmd_hold": "CmdHold", "cmd_resume": "CmdResume", "cmd_abort": "CmdAbort",
             "cmd_ack": "CmdAck", "recipe": "RecipeNo", "operation": "Operation", "job_id": "JobId",
             "job_latched": "JobLatched"}
    points = {key: prefix + value for key, value in names.items()}
    for name in setpoints:
        points[f"sp_{name}"] = f"{prefix}SP_{name}"
        points[f"pv_{name}"] = f"{prefix}PV_{name}"
    return points


def plc_modbus_points(setpoints: tuple) -> dict:
    points = {
        "heartbeat": {"table": "holding", "address": 0, "type": "uint16"},
        "state": {"table": "holding", "address": 1, "type": "uint16"},
        "error": {"table": "holding", "address": 2, "type": "uint16"},
        "remote": {"table": "holding", "address": 3, "type": "bool", "bit": 0},
        "safety": {"table": "holding", "address": 3, "type": "bool", "bit": 1},
        "recipe": {"table": "holding", "address": 4, "type": "uint16"},
        "operation": {"table": "holding", "address": 5, "type": "uint16"},
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


@contextmanager
def plc_sim(protocol: str = "opcua", cert_dir: Path | None = None, device_id: str = "SIM-PLC-T",
            machine: str = "Coater", setpoints: tuple = ("thickness", "temp"), recipes: str = "", **device):
    from simulators.plc_device.server import SimulatorRunner, parse

    port = free_port()
    argv = ["--protocol", protocol, "--device-id", device_id, "--address", "127.0.0.1", "--port", str(port),
            "--machine", machine, "--setpoints", ",".join(setpoints), "--host-name", "127.0.0.1"]
    if recipes:
        argv += ["--recipes", recipes]
    argv += ["--cert-dir", str(cert_dir)] if cert_dir is not None else ["--insecure"]
    runner = SimulatorRunner(parse(argv), _device(device_id, **device))
    runner.start()
    try:
        yield runner.program, runner, port
    finally:
        runner.stop()


def plc_config(protocol: str, port: int, cert_dir: Path | None = None, device_id: str = "SIM-PLC-T",
               machine: str = "Coater", setpoints: tuple = ("thickness", "temp"), capability: str = "cap.coat",
               recipes: dict | None = None, **extra) -> tuple[dict, str]:
    if protocol == "opcua":
        config = {"endpoint": f"opc.tcp://127.0.0.1:{port}/plc/", "request_timeout_sec": 2, "connect_timeout_sec": 1,
                  **_plc_mapping(plc_opcua_points(machine, setpoints), setpoints, capability, recipes)}
        if cert_dir is None:
            return {**config, "security_policy": "None", **extra}, ""
        return ({**config, "server_certificate": str(cert_dir / f"{device_id}.crt"), **extra},
                f"file://{cert_dir}/ilcs-client.json")
    config = {"host": "127.0.0.1", "port": port, "unit_id": 1, "request_timeout_sec": 1,
              **_plc_mapping(plc_modbus_points(setpoints), setpoints, capability, recipes)}
    return {**config, **extra}, ""


# ---------- REST 接口映射（devices/simulators/fleet：MiR 风格的 AGV 车队接口） ----------

def fleet_mapping(positions: dict | None = None) -> dict:
    return {
        "headers": {"Accept-Language": "en_US"},
        "identity": {"method": "GET", "path": "/status",
                     "fields": {"device_id": "robot_name", "serial": "serial_number", "model": "model",
                                "firmware": "software_version"},
                     "interlock": {"field": "state_text", "values": ["EmergencyStop", "Error"]}},
        "capabilities": {"cap.transfer": {
            "method": "POST", "path": "/mission_queue", "handle": "id",
            "body": {"mission_id": "{mission}", "message": "ILCS {command_id}",
                     "parameters": [{"id": "From", "value": "{from_position}"}, {"id": "To", "value": "{to_position}"}]},
            "defaults": {"mission": "mission-ilcs-transfer"}}},
        "status": {"method": "GET", "path": "/mission_queue/{handle}", "field": "state", "error_field": "message_result",
                   "states": {"Pending": "accepted", "Executing": "running", "Paused": "held", "Done": "done",
                              "Aborted": "failed"}},
        "lookup": {"method": "GET", "path": "/mission_queue", "detail_path": "/mission_queue/{id}", "id_field": "id",
                   "match_field": "message", "match": "ILCS {command_id}", "recent": 20},
        "hold": {"method": "PUT", "path": "/status", "body": {"state_id": 4}},
        "resume": {"method": "PUT", "path": "/status", "body": {"state_id": 3}},
        "abort": {"method": "DELETE", "path": "/mission_queue/{handle}"},
        "positions": positions or {},
    }


@contextmanager
def fleet_sim(cert_dir: Path, robots: str = "AGV-01,AGV-02", task_seconds: float = 0.4):
    from simulators.fleet.server import SimulatorRunner, parse

    port = free_port()
    runner = SimulatorRunner(parse(["--robots", robots, "--address", "127.0.0.1", "--port", str(port),
                                    "--task-seconds", str(task_seconds), "--cert-dir", str(cert_dir)]))
    runner.start()
    try:
        yield runner.fleet, runner, port
    finally:
        runner.close()


def fleet_config(port: int, cert_dir: Path, robot: str = "AGV-01", positions: dict | None = None,
                 **extra) -> tuple[dict, str]:
    return ({"base_url": f"http://127.0.0.1:{port}/robots/{robot}/api/v2.0.0", "allow_insecure_http": True,
             "request_timeout_sec": 2, "connect_timeout_sec": 1, "probe_interval_sec": 0.5,
             **fleet_mapping(positions), **extra},
            f"file://{cert_dir}/fleet.json")


def transfer(command_id: str, source: str = "HOTEL-01/S01", target: str = "ST-05/N1", type_: str = "transfer"):
    from ilcs_host.plugins.base import CommandRequest

    return CommandRequest(
        command_id=command_id, station_id="ST-SIM", capability="cap.transfer", type=type_,
        params={"labware_id": "LW-1", "barcode": "TRAY-C02", "labware_type": "TRAY-8",
                "from": {"location_id": source, "station_id": "", "kind": "hotel"},
                "to": {"location_id": target, "station_id": target.split("/")[0], "kind": "station"}},
        batch_id="B-SIM", step_index=0, step_id="s01",
    )
