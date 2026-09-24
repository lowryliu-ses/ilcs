"""ILCS Modbus TCP 模拟设备。

在系统外部独立运行，按 `contracts/modbus/TaskRegisters.json` 暴露任务寄存器，外加仅模拟器才有的故障
注入寄存器与状态诊断区。系统侧用 `modbus_tcp_v1` 驱动接入，和接一台 PLC 走同一条路。

    python simulators/modbus_device/server.py --device-id SIM-VAC-01 --port 5020

它模拟的是 PLC 的扫描行为：ILCS 写完触发寄存器后，设备在后台线程里处理指令，处理完先写应答块、
最后写应答序号。参数槽位没有名字（PLC 不知道 ILCS 的参数名），设备按槽位 p1…p16 回报实测值。
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
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

from pymodbus.datastore import ModbusSequentialDataBlock, ModbusServerContext, ModbusSlaveContext  # noqa: E402
from pymodbus.server import ModbusTcpServer  # noqa: E402

from simulators.common.device import FAULT_MODES, DeviceRejected, ReceiptLost, SimulatedDevice  # noqa: E402
from simulators.common.runtime import build_device, configure_logging, device_arguments, serve_forever  # noqa: E402

LAYOUT = json.loads((ROOT / "contracts" / "modbus" / "TaskRegisters.json").read_text(encoding="utf-8"))
BLOCKS = LAYOUT["blocks"]
SLOTS = int(LAYOUT["slots"])
TASK_TYPES = {code: name for name, code in LAYOUT["task_types"].items()}
ACK_CODES = {name: int(code) for code, name in LAYOUT["ack_codes"].items()}
STATES = {name: int(code) for code, name in LAYOUT["states"].items()}
QUALITIES = {name: int(code) for code, name in LAYOUT["qualities"].items()}
FLAGS = LAYOUT["flags"]
SIZE = max(block["address"] + block["length"] for block in BLOCKS.values())
# ILCS（Modbus 主站）只能写这些块；写设备侧的块回异常应答 02（非法数据地址），和 PLC 的只读区一样
WRITABLE = [(b["address"], b["address"] + b["length"]) for b in BLOCKS.values() if b["writer"] == "ilcs"]
log = logging.getLogger("ilcs.modbus-sim")


# ---------- 寄存器编解码（与驱动读同一份契约） ----------

def _width(spec: list) -> int:
    if spec[1] == "ascii":
        return spec[2] // 2
    if spec[1] == "f32":
        return 2 * (spec[2] if len(spec) > 2 else 1)
    return {"u16": 1, "u64": 4}[spec[1]]


def decode(block: str, registers: list[int]) -> dict:
    values = {}
    for name, spec in BLOCKS[block]["fields"].items():
        words = registers[spec[0]:spec[0] + _width(spec)]
        raw = b"".join(int(word).to_bytes(2, "big") for word in words)
        if spec[1] == "ascii":
            values[name] = raw.rstrip(b"\0").decode("ascii", errors="replace")
        elif spec[1] == "u16":
            values[name] = words[0]
        elif spec[1] == "u64":
            values[name] = int.from_bytes(raw, "big")
        elif len(spec) > 2:
            values[name] = list(struct.unpack(f">{spec[2]}f", raw))
        else:
            values[name] = struct.unpack(">f", raw)[0]
    return values


def encode_field(spec: list, value) -> list[int]:
    if spec[1] == "ascii":
        raw = str(value).encode("utf-8")[:spec[2]].ljust(spec[2], b"\0")
    elif spec[1] == "u16":
        raw = (int(value) & 0xFFFF).to_bytes(2, "big")
    elif spec[1] == "u64":
        raw = int(value).to_bytes(8, "big")
    elif len(spec) > 2:
        raw = struct.pack(f">{spec[2]}f", *(list(value) + [0.0] * spec[2])[:spec[2]])
    else:
        raw = struct.pack(">f", float(value))
    return [int.from_bytes(raw[i:i + 2], "big") for i in range(0, len(raw), 2)]


def _milliseconds(iso: str) -> int:
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


class RegisterBank(ModbusSequentialDataBlock):
    """保持寄存器区。主站的写入经 setValues 进来并触发回调；设备自己写应答走 put()，不触发回调。

    pymodbus 的从站上下文会把协议地址 +1 再访问数据块，所以数据块从 1 起编址。
    """

    def __init__(self, on_write):
        super().__init__(1, [0] * SIZE)
        self._on_write = on_write

    def setValues(self, address, values):  # noqa: N802  pymodbus 的接口名
        values = values if isinstance(values, list) else [values]
        super().setValues(address, values)
        self._on_write(address - 1, len(values))

    def put(self, block: str, values: dict) -> None:
        """按字段写设备侧的块；带序号的块最后写序号，主站看到新序号时其余字段已经就位。"""
        spec = BLOCKS[block]
        sequence = spec.get("sequence")
        for name, value in values.items():
            if name != sequence:
                field = spec["fields"][name]
                super().setValues(spec["address"] + field[0] + 1, encode_field(field, value))
        if sequence in values:
            field = spec["fields"][sequence]
            super().setValues(spec["address"] + field[0] + 1, encode_field(field, values[sequence]))

    def get(self, block: str) -> dict:
        spec = BLOCKS[block]
        return decode(block, self.getValues(spec["address"] + 1, spec["length"]))


class WriteGuardedContext(ModbusSlaveContext):
    def validate(self, fc_as_hex, address, count=1):
        if fc_as_hex in (5, 6, 15, 16, 22, 23):
            if not any(low <= address and address + count <= high for low, high in WRITABLE):
                return False
        return super().validate(fc_as_hex, address, count)


class ModbusDevice:
    """寄存器 ↔ 设备模型。每个触发在独立线程里处理：应答迟到（slow_submit）不挡住查询。"""

    def __init__(self, device: SimulatedDevice, runner: "SimulatorRunner | None" = None):
        self.device = device
        self.runner = runner
        self.bank = RegisterBank(self._written)
        self.heartbeat = 0
        self.processed: dict[str, int] = {}
        self.lock = threading.Lock()
        self.refresh()

    # ---------- 主站写入 ----------

    def _written(self, address: int, count: int) -> None:
        for block, handler in (("command", self._command), ("query", self._query), ("simulator_control", self._control)):
            spec = BLOCKS[block]
            trigger = spec["address"] + spec["fields"][spec["trigger"]][0]
            if address <= trigger < address + count:
                sequence = self.bank.get(block)[spec["trigger"]]
                with self.lock:
                    if sequence == self.processed.get(block):
                        continue  # 触发值没变：不是新指令
                    self.processed[block] = sequence
                threading.Thread(target=self._guarded, args=(handler, sequence), daemon=True).start()

    def _guarded(self, handler, sequence: int) -> None:
        try:
            handler(sequence)
        except Exception:  # 模拟 PLC 程序出错：不写应答，主站按超时判结果未知
            log.exception("处理触发 %s 失败", sequence)
        self.refresh()

    def _command(self, sequence: int) -> None:
        values = self.bank.get("command")
        command_id, task_type = values["command_id"], TASK_TYPES.get(values["task_type"], "")
        try:
            if not task_type:
                raise DeviceRejected("NotSupported", f"任务类型码 {values['task_type']} 不在契约内")
            if task_type == "hold":
                receipt = self.device.hold(command_id, values["target_command_id"])
            elif task_type == "abort":
                receipt = self.device.abort(command_id, values["target_command_id"])
            else:
                params = {
                    f"p{slot}": values["params"][slot - 1]
                    for slot in range(1, SLOTS + 1) if values["param_mask"] >> (slot - 1) & 1
                }
                receipt = self.device.submit(
                    command_id, task_type, f"code:{values['capability_code']}", params, {},
                )
            code = ACK_CODES["accepted"]
        except DeviceRejected as error:
            code = ACK_CODES[error.identifier]
            receipt = {"state": "failed", "quality": "bad", "device_ts": self.device.now().isoformat()}
        except ReceiptLost:
            return  # 设备已经动作，应答永远不写
        self.bank.put("ack", {
            "code": code, "command_id": command_id, "state": STATES[receipt["state"]],
            "quality": QUALITIES[receipt["quality"]], "device_time_ms": _milliseconds(receipt["device_ts"]),
            "seq": sequence,
        })

    def _query(self, sequence: int) -> None:
        command_id = self.bank.get("query")["command_id"]
        receipt = self.device.query(command_id)
        actuals = [0.0] * SLOTS
        mask = 0
        measured = {point["metric"]: point["value"] for point in receipt.get("telemetry") or []}
        delivered = receipt.get("delivered") or {}
        if not measured and "fraction" in delivered:  # 没完成的任务：按执行比例回报已执行量
            measured = {k: v * delivered["fraction"] for k, v in (delivered.get("params") or {}).items()}
        for metric, value in measured.items():
            if metric.startswith("p") and metric[1:].isdigit() and 1 <= int(metric[1:]) <= SLOTS:
                slot = int(metric[1:])
                actuals[slot - 1] = float(value)
                mask |= 1 << (slot - 1)
        error_code = 0
        if receipt["state"] == "failed":
            phase, error = receipt.get("phase"), receipt.get("error") or ""
            error_code = 3 if phase == "aborted" else 2 if "部分" in error else 1
        self.bank.put("result", {
            "command_id": command_id, "state": STATES.get(receipt["state"], STATES["unknown"]),
            "quality": QUALITIES.get(receipt.get("quality"), QUALITIES["uncertain"]),
            "device_time_ms": _milliseconds(receipt["device_ts"]), "error_code": error_code,
            "channel": receipt.get("channel") or 0, "actual_mask": mask, "actuals": actuals, "seq": sequence,
        })

    def _control(self, sequence: int) -> None:
        values = self.bank.get("simulator_control")
        mode = FAULT_MODES[values["fault"]] if values["fault"] < len(FAULT_MODES) else "none"
        parameter = float(values["parameter"])
        if mode == "offline" and self.runner is not None:
            self.runner.go_offline(parameter or 5)
        else:
            self.device.set_fault(mode, parameter)

    # ---------- 设备侧刷新 ----------

    def refresh(self) -> None:
        identity = self.device.identity()
        flags = (1 << FLAGS["simulator"]) if identity["simulator"] else 0
        flags |= (1 << FLAGS["interlock"]) if identity["interlock"] else 0
        flags |= (1 << FLAGS["accepts_commands"]) if identity["accepts_commands"] else 0
        self.heartbeat = (self.heartbeat + 1) % 65536
        self.bank.put("identity", {
            "device_id": identity["device_id"], "model": identity["model"],
            "contract_version": LAYOUT["version"], "flags": flags, "heartbeat": self.heartbeat,
            "channels": identity["channels"], "device_time_ms": _milliseconds(identity["device_ts"]),
        })
        state = json.dumps(self.device.state()).encode("ascii")
        if len(state) > 2000:  # 诊断区放不下时只留计数，保证是合法 JSON
            state = json.dumps({"fault": self.device.fault, "executions": sum(self.device.executions.values()),
                                "truncated": True}).encode()
        self.bank.put("simulator_state", {"length": len(state), "json": state.decode("ascii")})


class SimulatorRunner:
    """pymodbus 服务跑在独立事件循环线程里；离线故障真的关掉监听与已有连接，N 秒后重新监听。

    另有一个「PLC 扫描」线程按 --scan-seconds 刷新身份区（心跳计数、联锁、时间）与诊断区，
    离线期间也不停——断的是网络，不是 PLC。
    """

    def __init__(self, args: argparse.Namespace, device: SimulatedDevice):
        self.args = args
        self.modbus = ModbusDevice(device, self)
        self.context = ModbusServerContext(slaves=WriteGuardedContext(hr=self.modbus.bank), single=True)
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.server: ModbusTcpServer | None = None
        self.lock = threading.Lock()
        self.closed = threading.Event()
        threading.Thread(target=self._scan, daemon=True).start()

    def _scan(self) -> None:
        while not self.closed.wait(self.args.scan_seconds):
            try:
                self.modbus.refresh()
            except Exception:  # 扫描出错不能让线程退出
                log.exception("刷新寄存器失败")

    def _call(self, coroutine, timeout: float = 10):
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(timeout)

    async def _listen(self) -> ModbusTcpServer:
        server = ModbusTcpServer(self.context, address=(self.args.address, self.args.port))
        await server.serve_forever(background=True)
        return server

    def start(self) -> None:
        with self.lock:
            self.server = self._call(self._listen())
        log.info("Modbus TCP 模拟设备 %s 已启动：%s:%s", self.args.device_id, self.args.address, self.args.port)

    def _close_server(self) -> None:
        with self.lock:
            if self.server is not None:
                self._call(self.server.shutdown())
            self.server = None

    def stop(self) -> None:
        self.closed.set()
        self._close_server()

    def go_offline(self, seconds: float) -> None:
        def cycle():
            self._close_server()
            log.info("模拟离线 %.0f s", seconds)
            time.sleep(seconds)
            self.start()

        threading.Thread(target=cycle, daemon=True).start()


def parse(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    device_arguments(parser, default_port=5020)
    parser.add_argument("--scan-seconds", type=float, default=float(os.environ.get("SIM_SCAN_SECONDS", "0.2")),
                        help="PLC 扫描周期：身份区与诊断区的刷新间隔")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    configure_logging()
    args = parse(argv)
    runner = SimulatorRunner(args, build_device(args))
    runner.start()
    serve_forever(runner.modbus.device, args.tick_seconds, runner.stop)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
