"""Modbus TCP 设备驱动（`modbus_tcp_v1`）。

设备（PLC 程序或协议网关）实现 `contracts/modbus/TaskRegisters.json` 的任务寄存器契约：ILCS 把指令写进
指令邮箱、再写触发序号，设备处理后写应答块、最后写应答序号。查询走同样的「写选择 → 等序号」握手。
驱动只做寄存器编解码，参数名 ↔ 参数槽位、能力 ↔ 能力码的映射来自适配器配置，不猜厂商约定。

错误分类沿用系统的三分法：
- 应答码 1–4（参数非法 / 联锁 / 忙 / 不支持）、Modbus 异常应答（写入被拒）：设备明确没动 → `AdapterError`；
- 连接失败、写了触发但等不到应答序号：结果未知 → `AdapterUnreachable`；
- 应答码 9、应答里的指令号对不上、状态码越界：设备有响应但无法确认 → `AdapterIndeterminate`。

写触发之后绝不重写触发：Modbus 本身没有请求去重，重写一次就可能多动作一次。
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from pathlib import Path
import struct
import threading
import time

from ..core.config import settings
from .base import (
    AdapterContract, AdapterError, AdapterIndeterminate, AdapterUnreachable, CommandRequest,
    CommandResult,
)
from .contract import parse_receipt

DRIVER = "modbus_tcp_v1"
LAYOUT = json.loads(
    (Path(__file__).resolve().parents[3] / "contracts" / "modbus" / "TaskRegisters.json").read_text(encoding="utf-8")
)
BLOCKS = LAYOUT["blocks"]
SLOTS = int(LAYOUT["slots"])
TASK_TYPES = LAYOUT["task_types"]
ACK_CODES = {int(code): name for code, name in LAYOUT["ack_codes"].items()}
STATES = {int(code): name for code, name in LAYOUT["states"].items()}
QUALITIES = {int(code): name for code, name in LAYOUT["qualities"].items()}
ERRORS = {int(code): text for code, text in LAYOUT["error_codes"].items()}
FLAGS = LAYOUT["flags"]
MAX_READ = 120  # 功能码 03 单次最多 125 个寄存器


# ---------- 寄存器编解码（契约规定大端字节序、大端字序） ----------

def _width(spec: list) -> int:
    kind = spec[1]
    if kind == "ascii":
        return spec[2] // 2
    if kind == "f32":
        return 2 * (spec[2] if len(spec) > 2 else 1)
    return {"u16": 1, "u64": 4}[kind]


def decode(block: str, registers: list[int]) -> dict:
    values = {}
    for name, spec in BLOCKS[block]["fields"].items():
        offset, kind = spec[0], spec[1]
        words = registers[offset:offset + _width(spec)]
        raw = b"".join(int(word).to_bytes(2, "big") for word in words)
        if kind == "ascii":
            values[name] = raw.rstrip(b"\0").decode("ascii", errors="replace")
        elif kind == "u16":
            values[name] = words[0]
        elif kind == "u64":
            values[name] = int.from_bytes(raw, "big")
        elif len(spec) > 2:
            values[name] = list(struct.unpack(f">{spec[2]}f", raw))
        else:
            values[name] = struct.unpack(">f", raw)[0]
    return values


def encode(block: str, values: dict) -> list[int]:
    """整块编码；未给出的字段写 0。"""
    registers = [0] * BLOCKS[block]["length"]
    for name, spec in BLOCKS[block]["fields"].items():
        if name not in values:
            continue
        offset, kind, value = spec[0], spec[1], values[name]
        if kind == "ascii":
            raw = str(value).encode("ascii")
            if len(raw) > spec[2]:
                raise ValueError(f"{name} 超过 {spec[2]} 个 ASCII 字符")
            raw = raw.ljust(spec[2], b"\0")
        elif kind == "u16":
            raw = int(value).to_bytes(2, "big")
        elif kind == "u64":
            raw = int(value).to_bytes(8, "big")
        elif len(spec) > 2:
            raw = struct.pack(f">{spec[2]}f", *(list(value) + [0.0] * spec[2])[:spec[2]])
        else:
            raw = struct.pack(">f", float(value))
        words = [int.from_bytes(raw[i:i + 2], "big") for i in range(0, len(raw), 2)]
        registers[offset:offset + len(words)] = words
    return registers


def _iso(milliseconds: int) -> str:
    return datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc).isoformat(timespec="milliseconds")


class ModbusTcpAdapter:
    def __init__(self, record):
        self.station_id = record.station_id
        self.config = dict(record.config or {})
        self.host = str(self.config.get("host") or "")
        try:
            self.port = int(self.config.get("port") or 502)
            self.unit_id = int(self.config.get("unit_id", 1))
            self.base = int(self.config.get("base_address") or 0)
        except (TypeError, ValueError) as exc:
            raise AdapterError("modbus_tcp_v1 的 port / unit_id / base_address 必须是整数") from exc
        if not self.host or not (0 < self.port < 65536) or not (0 <= self.unit_id <= 247):
            raise AdapterError("modbus_tcp_v1 必须配置 host、port（1–65535）与 unit_id（0–247）")
        if self.host.lower() not in settings.adapter_allowed_host_set:
            raise AdapterError(f"Modbus 设备主机 {self.host} 不在 ILCS_ADAPTER_ALLOWED_HOSTS 白名单")
        self.connect_timeout = self._positive("connect_timeout_sec", 3.0)
        self.request_timeout = self._positive("request_timeout_sec", 10.0)
        self.poll_interval = self._positive("poll_interval_ms", 50.0) / 1000
        self.heartbeat_stale = self._positive("heartbeat_stale_sec", 30.0)
        self.expected_device_id = str(self.config.get("expected_device_id") or "")
        self.capability_codes = self._code_map("capabilities", 1, 65535)
        self.param_slots = self._code_map("params", 1, SLOTS)
        if len(set(self.param_slots.values())) != len(self.param_slots):
            raise AdapterError("params 里两个参数映射到了同一个槽位")
        self.slot_names = {slot: name for name, slot in self.param_slots.items()}
        self.material_map = self.config.get("material_map") or {}
        if not isinstance(self.material_map, dict):
            raise AdapterError("material_map 必须是 {参数: {material, unit, factor}}")
        self._client = None
        self._lock = threading.Lock()
        self._sequences: dict[str, int] = {}
        self._heartbeat: tuple[int, float] | None = None
        self.contract = AdapterContract(
            kind="real", protocol=record.protocol or "Modbus TCP", version=record.version or "1.0",
            supports_hold=bool(record.supports_hold), supports_abort=bool(record.supports_abort),
            supports_query=bool(record.supports_query), supports_dedup=bool(record.supports_dedup),
            note=record.note or "Modbus TCP 任务寄存器设备",
        )

    def _positive(self, key: str, default: float) -> float:
        try:
            value = float(self.config.get(key, default))
        except (TypeError, ValueError) as exc:
            raise AdapterError(f"{key} 必须是正数") from exc
        if value <= 0 or value > 3600 * (1000 if key.endswith("_ms") else 1):
            raise AdapterError(f"{key} 超出允许范围")
        return value

    def _code_map(self, key: str, low: int, high: int) -> dict[str, int]:
        raw = self.config.get(key) or {}
        if not isinstance(raw, dict):
            raise AdapterError(f"{key} 必须是 {{名称: 编号}}")
        result = {}
        for name, code in raw.items():
            if isinstance(code, bool) or not isinstance(code, int) or not (low <= code <= high):
                raise AdapterError(f"{key}.{name} 必须是 {low}–{high} 的整数")
            result[str(name)] = code
        return result

    # ---------- 连接与寄存器读写 ----------

    def _connect(self):
        if self._client is not None and self._client.connected:
            return self._client
        from pymodbus.client import ModbusTcpClient

        # retries=0：库内重试会把「写触发」重发一遍，设备侧就可能再动作一次。
        # timeout 同时约束连接与单次读写；整次握手的等待由 request_timeout 约束
        client = ModbusTcpClient(self.host, port=self.port, timeout=self.connect_timeout, retries=0)
        try:
            connected = client.connect()
        except Exception as exc:
            raise AdapterUnreachable(f"Modbus 设备不可达：{exc.__class__.__name__}") from exc
        if not connected:
            client.close()
            raise AdapterUnreachable(f"Modbus 设备不可达：{self.host}:{self.port}")
        self._client = client
        return client

    def _drop(self) -> None:
        if self._client is not None:
            self._client.close()
        self._client = None

    def close(self) -> None:
        """配置换版本或缓存清空时由注册表调用。"""
        with self._lock:
            self._drop()

    def _read(self, block: str) -> dict:
        from pymodbus.exceptions import ModbusException

        spec = BLOCKS[block]
        registers: list[int] = []
        client = self._connect()
        try:
            for start in range(0, spec["length"], MAX_READ):
                count = min(MAX_READ, spec["length"] - start)
                response = client.read_holding_registers(self.base + spec["address"] + start, count=count, slave=self.unit_id)
                if response.isError():
                    raise AdapterIndeterminate(f"设备拒绝读取 {block} 寄存器：{response}")
                registers.extend(response.registers)
        except AdapterIndeterminate:
            raise
        except (ModbusException, OSError) as exc:
            self._drop()
            raise AdapterUnreachable(f"读取 {block} 寄存器失败：{exc.__class__.__name__}") from exc
        return decode(block, registers)

    def _write(self, block: str, values: dict, *, only: str = "") -> None:
        """写整块（不含触发字段），或只写 only 指定的单个字段。异常应答说明写入没被采纳。"""
        from pymodbus.exceptions import ModbusException

        spec = BLOCKS[block]
        registers = encode(block, values)
        if only:
            offset, width = spec["fields"][only][0], _width(spec["fields"][only])
        else:
            offset, width = 0, spec["fields"][spec["trigger"]][0]
        client = self._connect()
        try:
            response = client.write_registers(
                self.base + spec["address"] + offset, registers[offset:offset + width], slave=self.unit_id,
            )
        except (ModbusException, OSError) as exc:
            self._drop()
            raise AdapterUnreachable(f"写入 {block} 寄存器无结论：{exc.__class__.__name__}") from exc
        if response.isError():
            raise AdapterError(f"设备拒绝写入 {block} 寄存器：{response}")

    def _next_sequence(self, request_block: str, reply_block: str) -> int:
        if request_block not in self._sequences:
            # 从设备当前的触发与应答序号里较大的一个接着编：上一次回执丢失时触发寄存器已经是新值，
            # 再写同一个值设备看不到变化，指令就永远不会被处理
            trigger = BLOCKS[request_block]["trigger"]
            self._sequences[request_block] = max(
                int(self._read(reply_block)["seq"]), int(self._read(request_block)[trigger]),
            )
        self._sequences[request_block] = self._sequences[request_block] % 65535 + 1
        return self._sequences[request_block]

    def _handshake(self, request_block: str, reply_block: str, values: dict, command_id: str) -> dict:
        """写邮箱 → 写触发 → 等应答序号。写了触发之后的任何失败都是结果未知。"""
        sequence = self._next_sequence(request_block, reply_block)
        self._write(request_block, values)
        trigger = BLOCKS[request_block]["trigger"]
        self._write(request_block, {trigger: sequence}, only=trigger)
        deadline = time.monotonic() + self.request_timeout
        while True:
            try:
                reply = self._read(reply_block)
            except AdapterIndeterminate:
                raise
            except AdapterUnreachable as exc:
                raise AdapterUnreachable(f"已写触发，读应答失败，结果未知：{exc}") from exc
            if reply["seq"] == sequence:
                break
            if time.monotonic() >= deadline:
                raise AdapterUnreachable(f"已写触发，{self.request_timeout:g} s 内没有等到设备应答，结果未知")
            time.sleep(self.poll_interval)
        if reply["command_id"] != command_id:
            raise AdapterIndeterminate(
                f"设备应答的指令号 {reply['command_id'] or '缺失'} 与 {command_id} 不一致"
            )
        return reply

    # ---------- 契约 ----------

    def identity(self) -> dict:
        with self._lock:
            return self._read("identity")

    def healthcheck(self) -> dict:
        identity = self.identity()
        flags = int(identity["flags"])
        simulator = bool(flags >> FLAGS["simulator"] & 1)
        if simulator and settings.environment == "production":
            raise AdapterError("该 Modbus 设备自报为模拟器；正式环境不接入模拟设备")
        if identity["contract_version"] != LAYOUT["version"]:
            raise AdapterError(
                f"设备任务寄存器契约版本 {identity['contract_version']}，驱动要求 {LAYOUT['version']}"
            )
        actual = identity["device_id"]
        if self.expected_device_id and actual != self.expected_device_id:
            raise AdapterError(f"设备身份不匹配：期望 {self.expected_device_id}，实际 {actual or '缺失'}")
        # PLC 程序停了寄存器照样能读：心跳计数长时间不变就当失联
        moment = time.monotonic()
        beat = int(identity["heartbeat"])
        if self._heartbeat is None or self._heartbeat[0] != beat:
            self._heartbeat = (beat, moment)
        elif moment - self._heartbeat[1] > self.heartbeat_stale:
            raise AdapterUnreachable(f"设备心跳计数 {self.heartbeat_stale:g} s 没有变化，PLC 程序可能已停止")
        return {
            "reachable": True, "driver": DRIVER, "protocol": self.contract.protocol,
            "device_id": actual, "model": identity["model"], "simulator": simulator,
            "interlock": bool(flags >> FLAGS["interlock"] & 1),
            "accepts_commands": bool(flags >> FLAGS["accepts_commands"] & 1),
            "channels": identity["channels"],
        }

    def _params(self, request: CommandRequest) -> tuple[int, list[float]]:
        mask, values = 0, [0.0] * SLOTS
        for name, value in (request.params or {}).items():
            if isinstance(value, dict):
                raise AdapterError(
                    f"Modbus 任务寄存器只接受数值参数，不支持 {name} 这类结构化参数（如孔位矩阵）；"
                    f"请改用 OPC UA / SiLA 2 设备或网关"
                )
            slot = self.param_slots.get(name)
            if slot is None:
                raise AdapterError(f"参数 {name} 没有映射到寄存器槽位（适配器配置 params）")
            try:
                number = float(value)
            except (TypeError, ValueError) as exc:
                raise AdapterError(f"参数 {name} = {value!r} 不是数值") from exc
            if not math.isfinite(number):
                raise AdapterError(f"参数 {name} 不是有限数值")
            mask |= 1 << (slot - 1)
            values[slot - 1] = number
        return mask, values

    def _receipt(self, reply: dict, command_id: str, delivered: dict | None = None,
                 telemetry: list | None = None, error: str = "") -> CommandResult:
        state = STATES.get(int(reply["state"]))
        quality = QUALITIES.get(int(reply["quality"]))
        if state is None or state == "not_found" or quality is None:
            raise AdapterIndeterminate(f"设备应答状态码 {reply['state']} / 质量码 {reply['quality']} 不在契约内")
        return parse_receipt({
            "command_id": command_id, "state": state, "quality": quality,
            "device_ts": _iso(int(reply["device_time_ms"])), "delivered": delivered or {},
            "telemetry": telemetry or [], "error": error,
        }, command_id, f"real:{DRIVER}")

    def _command(self, request: CommandRequest, capability_code: int, mask: int, params: list[float]) -> CommandResult:
        with self._lock:
            try:
                values = {
                    "command_id": request.command_id, "task_type": TASK_TYPES[request.type],
                    "capability_code": capability_code, "target_command_id": request.target_command_id,
                    "param_mask": mask, "params": params,
                }
                encode("command", values)
            except (KeyError, ValueError) as exc:
                raise AdapterError(f"指令无法编码为任务寄存器：{exc}") from exc
            reply = self._handshake("command", "ack", values, request.command_id)
        code = int(reply["code"])
        outcome = ACK_CODES.get(code)
        if outcome == "accepted":
            return self._receipt(reply, request.command_id)
        if outcome in {"InvalidParameters", "Interlocked", "DeviceBusy", "NotSupported"}:
            raise AdapterError(f"设备拒绝（{outcome}）")
        raise AdapterIndeterminate(f"设备应答码 {code}（{outcome or '未约定'}），无法确认是否已动作")

    def submit(self, request: CommandRequest) -> CommandResult:
        capability_code = self.capability_codes.get(request.capability)
        if capability_code is None:
            raise AdapterError(f"能力 {request.capability} 没有映射到能力码（适配器配置 capabilities）")
        mask, params = self._params(request)
        return self._command(request, capability_code, mask, params)

    def query(self, command_id: str) -> CommandResult | None:
        if not self.contract.supports_query:
            return None
        with self._lock:
            reply = self._handshake("query", "result", {"command_id": command_id}, command_id)
        if STATES.get(int(reply["state"])) == "not_found":
            return None
        delivered: dict = {}
        telemetry = []
        for slot, value in enumerate(reply["actuals"], start=1):
            if not reply["actual_mask"] >> (slot - 1) & 1:
                continue
            name = self.slot_names.get(slot, f"slot_{slot}")
            delivered[name] = round(value, 6)
            telemetry.append({"metric": name, "value": round(value, 6), "setpoint": None})
        materials = [
            {"material": mapping["material"], "unit": mapping.get("unit", ""),
             "quantity": round(delivered[name] * float(mapping.get("factor", 1)), 6)}
            for name, mapping in self.material_map.items()
            if name in delivered and isinstance(mapping, dict) and mapping.get("material")
        ]
        if materials:
            delivered["materials"] = materials
        if reply["channel"]:
            delivered["channel"] = reply["channel"]
        error = ERRORS.get(int(reply["error_code"]), f"设备错误码 {reply['error_code']}")
        state = STATES.get(int(reply["state"]))
        return self._receipt(reply, command_id, delivered, telemetry if state == "done" else [], error)

    def hold(self, request: CommandRequest) -> CommandResult:
        if not self.contract.supports_hold:
            raise AdapterError("设备声明不支持保持")
        return self._command(request, 0, 0, [0.0] * SLOTS)

    def abort(self, request: CommandRequest) -> CommandResult:
        if not self.contract.supports_abort:
            raise AdapterError("设备声明不支持终止")
        return self._command(request, 0, 0, [0.0] * SLOTS)
