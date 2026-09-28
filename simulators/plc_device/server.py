"""ILCS PLC 模拟设备：厂家自有点表的 PLC，按 `--protocol opcua|modbus` 暴露，系统侧用 `opcua_map_v1` /
`modbus_map_v1` 驱动接入。它不实现任何 ILCS 任务契约——这正是点表映射驱动要对接的情形。

PLC 程序（两种协议相同）：设定值 SP_<名>、程序号 RecipeNo、工序 Operation、指令号 JobId 由上位写入；
上位给 CmdStart 一个上升沿就开始作业（把 JobId 锁存到 JobLatched），CmdHold / CmdResume / CmdAbort / CmdAck
同样按上升沿动作。State：0 空闲、1 运行、2 保持、3 完成、4 故障（ErrorCode：17 过程报警、23 执行中断、
31 程序号不存在、90 安全回路未闭合、91 不在远程模式、92 上一作业未复位）。完成 / 故障后要 CmdAck 复位才能再启动。
Heartbeat 每个扫描周期加一；RemoteMode、SafetyOk 反映就绪与联锁；实测值 PV_<名>。

OPC UA：命名空间 `urn:ilcs:sim:plc`，节点 ID `ns=<序号>;s=<机器名>.<点名>`（如 `Coater.State`），另有方法
`<机器名>.StartJob(RecipeNo)`。安全与其他 OPC UA 模拟设备相同（`--cert-dir`，`--insecure` 只开 None 策略）。

Modbus（0 基地址）：线圈 0–4 = CmdStart / CmdHold / CmdResume / CmdAbort / CmdAck；保持寄存器
0 Heartbeat、1 State、2 ErrorCode、3 标志位（bit0 RemoteMode、bit1 SafetyOk）、4 RecipeNo、5 Operation、
10+2i SP_i（float32，高字在前）、50+2i PV_i、100–119 JobId、120–139 JobLatched（各 40 字符 ASCII）、
200–215 Vendor、216–231 Model、232–247 SerialNo、248–255 Firmware；900–919 模拟器故障命令（ASCII「模式 参数」）。

    python simulators/plc_device/server.py --protocol opcua --machine Coater --setpoints thickness,temp --port 4841
    python simulators/plc_device/server.py --protocol modbus --machine Mixer --setpoints mass,volume,rate,temp,rpm,vacuum --port 5021
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
import struct
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:  # 直接运行（容器）时也能找到 simulators 包
    sys.path.insert(0, str(ROOT))

from simulators.common.device import DeviceRejected, ReceiptLost, SimulatedDevice  # noqa: E402
from simulators.common.runtime import build_device, configure_logging, device_arguments, serve_forever  # noqa: E402

MARK = "ILCS-SIMULATOR"
NAMESPACE = "urn:ilcs:sim:plc"
COMMANDS = ("CmdStart", "CmdHold", "CmdResume", "CmdAbort", "CmdAck")
ERRORS = {"fail": 17, "partial": 23}
log = logging.getLogger("ilcs.plc-sim")


class PlcProgram:
    """PLC 内存 + 扫描逻辑。协议层每个扫描周期先把上位写入的点同步进来，扫描完再把输出同步出去。"""

    def __init__(self, device: SimulatedDevice, setpoints: list[str], recipes: set[int] | None, machine: str,
                 runner=None):
        self.device = device
        self.setpoints = setpoints
        self.recipes = recipes
        self.machine = machine
        self.runner = runner
        self.memory: dict = {
            "Vendor": MARK, "Model": device.model, "SerialNo": device.device_id, "Firmware": device.firmware,
            "State": 0, "ErrorCode": 0, "Heartbeat": 0, "RemoteMode": True, "SafetyOk": True,
            "RecipeNo": 0, "Operation": 0, "JobId": "", "JobLatched": "", "SimFault": "",
            **{command: False for command in COMMANDS},
            **{f"SP_{name}": 0.0 for name in setpoints}, **{f"PV_{name}": 0.0 for name in setpoints},
        }
        self.inputs = {"RecipeNo", "Operation", "JobId", "SimFault", *COMMANDS, *(f"SP_{n}" for n in setpoints)}
        self.previous = {command: False for command in COMMANDS}
        self.current = ""
        self.counter = 0
        self.last_fault = ""
        self.lock = threading.RLock()

    def _task(self):
        return self.device.tasks.get(self.current) if self.current else None

    def start(self, recipe: int | None = None) -> None:
        with self.lock:
            if recipe is not None:
                self.memory["RecipeNo"] = recipe
            state = self.memory["State"]
            if state in {1, 2}:
                return  # 正在运行：忽略
            if state in {3, 4}:
                self.memory["ErrorCode"] = 92
                return
            if not self.memory["SafetyOk"] or self.device.interlock:
                self.memory["ErrorCode"] = 90
                return
            if self.device.fault == "busy":
                self.memory["ErrorCode"] = 91
                return
            if self.recipes is not None and int(self.memory["RecipeNo"]) not in self.recipes:
                self.memory["State"], self.memory["ErrorCode"] = 4, 31
                return
            self.counter += 1
            job = f"JOB-{self.counter}"
            params = {name: float(self.memory[f"SP_{name}"]) for name in self.setpoints}
            try:
                self.device.submit(job, "dispatch", f"op{int(self.memory['Operation'])}", params,
                                   {"batch_id": "plc", "step_id": job})
            except DeviceRejected:
                self.memory["State"], self.memory["ErrorCode"] = 4, 31
                return
            except ReceiptLost:
                pass  # 点表设备没有「回执」：动作照常开始
            self.current = job
            self.memory["JobLatched"] = self.memory["JobId"]
            self.memory["ErrorCode"] = 0

    def scan(self) -> None:
        with self.lock:
            fault = str(self.memory.get("SimFault") or "").strip()
            if fault and fault != self.last_fault:
                self.last_fault = fault
                self._apply_fault(fault)
            edges = {command for command in COMMANDS if self.memory[command] and not self.previous[command]}
            self.previous = {command: bool(self.memory[command]) for command in COMMANDS}
            if "CmdStart" in edges:
                self.start()
            task = self._task()
            if "CmdHold" in edges and task is not None and task.state in {"accepted", "running"}:
                self.device.hold(f"HOLD-{self.counter}", self.current)
            if "CmdResume" in edges and task is not None and task.state == "held":
                self.device.submit(f"RESUME-{self.counter}", "resume", task.capability, {},
                                   {"batch_id": "plc", "step_id": self.current})
            if "CmdAbort" in edges and self.current:
                self.device.abort(f"ABORT-{self.counter}", self.current)
            self.device.tick()
            task = self._task()
            if "CmdAck" in edges and self.memory["State"] in {3, 4}:
                self.current, task = "", None
                self.memory["State"], self.memory["ErrorCode"] = 0, 0
            self._outputs(task)

    def _outputs(self, task) -> None:
        memory = self.memory
        memory["Heartbeat"] = (memory["Heartbeat"] + 1) % 65536
        memory["RemoteMode"] = self.device.fault != "busy"
        memory["SafetyOk"] = not self.device.interlock
        if task is None:
            if memory["State"] not in {3, 4}:
                memory["State"] = 0
            return
        state = task.state
        if state in {"accepted", "running"}:
            memory["State"] = 1
        elif state == "held":
            memory["State"] = 2
        elif state == "done":
            memory["State"] = 3
        elif state == "failed":
            memory["State"], memory["ErrorCode"] = 4, ERRORS.get(task.fault_at_submit, 17)
        else:  # aborted
            memory["State"] = 0
            self.current = ""
        measured = {point["metric"]: point["value"] for point in task.telemetry or []}
        progress = min(1.0, task.elapsed_s / max(task.duration_s, 1e-6))
        for name in self.setpoints:
            target = float(task.params.get(name, 0.0))
            memory[f"PV_{name}"] = float(measured.get(name, target * (progress if state != "done" else 1.0)))

    def _apply_fault(self, text: str) -> None:
        mode, _, parameter = text.partition(" ")
        seconds = float(parameter or 0)
        if mode == "offline" and self.runner is not None:
            self.runner.go_offline(seconds or 5)
            return
        try:
            self.device.set_fault(mode, seconds)
        except ValueError:
            log.warning("未知故障模式 %s", mode)

    def state(self) -> dict:
        return {**self.device.state(), "plc": {k: v for k, v in self.memory.items() if not k.startswith("Sim")}}


# ---------- OPC UA ----------

class OpcUaPlc:
    def __init__(self, args: argparse.Namespace, program: PlcProgram):
        self.args = args
        self.program = program
        self.endpoint = f"opc.tcp://{args.address}:{args.port}/plc/"
        self.application_uri = f"urn:ilcs:simulator:{args.device_id}"
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.server = None
        self.nodes: dict = {}
        self.last_inputs: dict = {}
        self.security = None
        if not args.insecure:
            from simulators.common.opcua import ServerSecurity

            self.security = ServerSecurity(args.cert_dir, args.device_id, args.host_name, self.application_uri,
                                           "ILCS PLC Simulator")

    def _call(self, coroutine, timeout: float = 30):
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(timeout)

    async def _build(self):
        from asyncua import Server, ua

        server = Server()
        await server.init()
        server.set_endpoint(self.endpoint)
        server.set_server_name(f"ILCS PLC 模拟设备 {self.args.device_id}")
        await server.set_application_uri(self.application_uri)
        if self.security is None:
            server.set_security_policy([ua.SecurityPolicyType.NoSecurity])
        else:
            await self.security.apply(server, allow_writes=True)
        index = await server.register_namespace(NAMESPACE)
        machine = self.args.machine
        folder = await server.nodes.objects.add_object(ua.NodeId(machine, index), ua.QualifiedName(machine, index))
        types = {bool: ua.VariantType.Boolean, int: ua.VariantType.Int16, float: ua.VariantType.Float,
                 str: ua.VariantType.String}
        self.nodes = {}
        for name, value in self.program.memory.items():
            variant = types[type(value)]
            if name == "Heartbeat":
                variant = ua.VariantType.UInt16
            node = await folder.add_variable(ua.NodeId(f"{machine}.{name}", index), ua.QualifiedName(name, index),
                                             value, variant)
            if name in self.program.inputs:
                await node.set_writable()
                self.last_inputs[name] = value  # 初值算「已看到」：第一次扫描前 PLC 自己改的值不会被冲掉
            self.nodes[name] = (node, variant)

        def start_job(parent, recipe):
            self.program.start(int(recipe.Value))
            return []

        await folder.add_method(ua.NodeId(f"{machine}.StartJob", index), ua.QualifiedName("StartJob", index),
                                start_job, [ua.VariantType.Int16], [])
        await server.start()
        return server

    async def _sync(self):
        from asyncua import ua

        memory = self.program.memory
        # 只有客户端改过的输入点才覆盖 PLC 内存：PLC 自己改的（方法调用写的程序号）不能被旧值冲掉
        for name in self.program.inputs:
            node, _ = self.nodes[name]
            value = await node.read_value()
            if self.last_inputs.get(name) != value:
                memory[name] = value
                self.last_inputs[name] = value
        self.program.scan()
        for name, (node, variant) in self.nodes.items():
            value = memory[name]
            if name in self.program.inputs:
                if self.last_inputs.get(name) == value:
                    continue
                self.last_inputs[name] = value
            await node.write_value(ua.DataValue(ua.Variant(value, variant)))

    def scan(self) -> None:
        if self.server is None:
            self.program.scan()  # 离线期间 PLC 照常扫描
            return
        try:
            self._call(self._sync(), timeout=5)
        except Exception:  # 离线切换中
            pass

    def start(self) -> None:
        self.last_inputs = {}
        self.server = self._call(self._build())
        log.info("PLC 模拟设备（OPC UA）%s 已启动：%s", self.args.device_id, self.endpoint)

    def stop(self) -> None:
        server, self.server = self.server, None
        if server is not None:
            self._call(server.stop())


# ---------- Modbus ----------

class ModbusPlc:
    HOLDING = {"Heartbeat": 0, "State": 1, "ErrorCode": 2, "Flags": 3, "RecipeNo": 4, "Operation": 5}
    STRINGS = {"JobId": (100, 40), "JobLatched": (120, 40), "Vendor": (200, 32), "Model": (216, 32),
               "SerialNo": (232, 32), "Firmware": (248, 16), "SimFault": (900, 40)}
    SIZE = 1000

    def __init__(self, args: argparse.Namespace, program: PlcProgram):
        from pymodbus.datastore import ModbusSequentialDataBlock, ModbusServerContext, ModbusSlaveContext

        self.args = args
        self.program = program
        self.holding = ModbusSequentialDataBlock(1, [0] * self.SIZE)  # 从站上下文会把地址 +1
        self.coils = ModbusSequentialDataBlock(1, [False] * 16)
        writable = {4, 5, *range(10, 50), *range(100, 120), *range(900, 920)}

        class Guarded(ModbusSlaveContext):
            def validate(self, fc_as_hex, address, count=1):
                if fc_as_hex in (6, 16, 22, 23) and not all(a in writable for a in range(address, address + count)):
                    return False
                if fc_as_hex in (5, 15) and address + count > len(COMMANDS):
                    return False
                return super().validate(fc_as_hex, address, count)

        self.context = ModbusServerContext(slaves=Guarded(hr=self.holding, co=self.coils), single=True)
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.server = None
        self._write_outputs()

    def _words(self, value: float) -> list[int]:
        raw = struct.pack(">f", float(value))
        return [int.from_bytes(raw[:2], "big"), int.from_bytes(raw[2:], "big")]

    def _float(self, words: list[int]) -> float:
        return struct.unpack(">f", b"".join(int(w).to_bytes(2, "big") for w in words))[0]

    def _string(self, address: int, length: int) -> str:
        words = self.holding.getValues(address + 1, (length + 1) // 2)
        return b"".join(int(w).to_bytes(2, "big") for w in words).rstrip(b"\0").decode("ascii", errors="replace")

    def _put_string(self, address: int, length: int, text: str) -> None:
        raw = text.encode("ascii", errors="replace")[:length].ljust(length + length % 2, b"\0")
        self.holding.setValues(address + 1, [int.from_bytes(raw[i:i + 2], "big") for i in range(0, len(raw), 2)])

    def _read_inputs(self) -> None:
        memory = self.program.memory
        coils = self.coils.getValues(1, len(COMMANDS))
        for command, value in zip(COMMANDS, coils):
            memory[command] = bool(value)
        memory["RecipeNo"] = self.holding.getValues(self.HOLDING["RecipeNo"] + 1, 1)[0]
        memory["Operation"] = self.holding.getValues(self.HOLDING["Operation"] + 1, 1)[0]
        for index, name in enumerate(self.program.setpoints):
            memory[f"SP_{name}"] = self._float(self.holding.getValues(10 + 2 * index + 1, 2))
        memory["JobId"] = self._string(*self.STRINGS["JobId"])
        memory["SimFault"] = self._string(*self.STRINGS["SimFault"])

    def _write_outputs(self) -> None:
        memory = self.program.memory
        for name in ("Heartbeat", "State", "ErrorCode"):
            self.holding.setValues(self.HOLDING[name] + 1, [int(memory[name]) & 0xFFFF])
        flags = (1 if memory["RemoteMode"] else 0) | (2 if memory["SafetyOk"] else 0)
        self.holding.setValues(self.HOLDING["Flags"] + 1, [flags])
        for index, name in enumerate(self.program.setpoints):
            self.holding.setValues(50 + 2 * index + 1, self._words(memory[f"PV_{name}"]))
        for name in ("JobLatched", "Vendor", "Model", "SerialNo", "Firmware"):
            self._put_string(*self.STRINGS[name], str(memory[name]))

    def scan(self) -> None:
        self._read_inputs()
        self.program.scan()
        self._write_outputs()

    async def _listen(self):
        from pymodbus.server import ModbusTcpServer

        server = ModbusTcpServer(self.context, address=(self.args.address, self.args.port))
        await server.serve_forever(background=True)
        return server

    def start(self) -> None:
        self.server = asyncio.run_coroutine_threadsafe(self._listen(), self.loop).result(10)
        log.info("PLC 模拟设备（Modbus TCP）%s 已启动：%s:%s", self.args.device_id, self.args.address, self.args.port)

    def stop(self) -> None:
        server, self.server = self.server, None
        if server is not None:
            asyncio.run_coroutine_threadsafe(server.shutdown(), self.loop).result(10)


class SimulatorRunner:
    def __init__(self, args: argparse.Namespace, device: SimulatedDevice):
        self.args = args
        setpoints = [name.strip() for name in args.setpoints.split(",") if name.strip()]
        recipes = {int(item) for item in args.recipes.split(",") if item.strip()} if args.recipes else None
        self.program = PlcProgram(device, setpoints, recipes, args.machine, self)
        self.io = OpcUaPlc(args, self.program) if args.protocol == "opcua" else ModbusPlc(args, self.program)
        self.closed = threading.Event()
        threading.Thread(target=self._scan, daemon=True).start()

    def _scan(self) -> None:
        while not self.closed.wait(self.args.scan_seconds):
            try:
                self.io.scan()
            except Exception:
                log.exception("PLC 扫描失败")

    def start(self) -> None:
        self.io.start()

    def stop(self) -> None:
        self.closed.set()
        self.io.stop()

    def go_offline(self, seconds: float) -> None:
        def cycle():
            time.sleep(0.2)
            self.io.stop()
            log.info("模拟离线 %.0f s", seconds)
            time.sleep(seconds)
            self.io.start()

        threading.Thread(target=cycle, daemon=True).start()


def parse(argv=None) -> argparse.Namespace:
    env = os.environ.get
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    device_arguments(parser, default_port=4841)
    parser.add_argument("--protocol", default=env("SIM_PROTOCOL", "opcua"), choices=["opcua", "modbus"])
    parser.add_argument("--machine", default=env("SIM_MACHINE", "Line"))
    parser.add_argument("--setpoints", default=env("SIM_SETPOINTS", "temp"), help="设定值名，逗号分隔")
    parser.add_argument("--recipes", default=env("SIM_RECIPES", ""), help="允许的程序号，逗号分隔；缺省不限")
    parser.add_argument("--scan-seconds", type=float, default=float(env("SIM_SCAN_SECONDS", "0.05")))
    parser.add_argument("--cert-dir", default=env("SIM_CERT_DIR", "./opcua-certs"))
    parser.add_argument("--insecure", action="store_true", default=env("SIM_INSECURE", "0") == "1")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    configure_logging()
    logging.getLogger("asyncua").setLevel(logging.WARNING)
    args = parse(argv)
    device = build_device(args)
    runner = SimulatorRunner(args, device)
    runner.start()
    serve_forever(device, args.tick_seconds, runner.stop)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
