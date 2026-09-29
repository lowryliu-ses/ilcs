"""Modbus 点表映射驱动（`modbus_map_v1`）。

面向「设备（PLC、温控仪表、串口转以太网网关后的 RTU 设备）有自己的寄存器表，没有 ILCS 任务寄存器」的场景。
作业逻辑见 `point_map.py`；这里把点名解释成寄存器：

```json
"points": {
  "state":     {"table": "holding", "address": 1, "type": "uint16"},
  "sp_temp":   {"table": "holding", "address": 100, "type": "float32", "word_order": "big"},
  "pv_temp":   {"table": "input", "address": 20, "type": "int16", "scale": 0.1},
  "cmd_start": {"table": "coil", "address": 0, "type": "bool"},
  "remote":    {"table": "holding", "address": 3, "type": "bool", "bit": 0},
  "serial":    {"table": "holding", "address": 300, "type": "ascii", "length": 16}
}
```

地址是协议里的 0 基地址（手册上写 40001 的，这里填 0）。表：holding / input / coil / discrete，只有 holding 与
coil 可写；布尔从保持寄存器取某一位只读不写（读—改—写不是原子的）。32 位数值缺省高字在前，可设
`word_order: little`。异常应答说明写入没被采纳 → 明确失败；连不上、超时 → 结果未知。
"""
from __future__ import annotations

import struct
import threading

from ..core.config import settings
from .base import AdapterError, AdapterIndeterminate, AdapterUnreachable
from .point_map import PointMapAdapter

DRIVER = "modbus_map_v1"
TABLES = {"holding", "input", "coil", "discrete"}
WIDTH = {"uint16": 1, "int16": 1, "uint32": 2, "int32": 2, "float32": 2, "bool": 1}


class ModbusMapAdapter(PointMapAdapter):
    DRIVER = DRIVER
    PROTOCOL = "Modbus 点表映射"
    NOTE = "Modbus 点表映射（设备自有寄存器表）"

    def __init__(self, record, journal_key: str = ""):
        super().__init__(record, journal_key)
        self.host = str(self.config.get("host") or "")
        try:
            self.port = int(self.config.get("port") or 502)
            self.unit_id = int(self.config.get("unit_id", 1))
        except (TypeError, ValueError) as exc:
            raise AdapterError("modbus_map_v1 的 port / unit_id 必须是整数") from exc
        if not self.host or not (0 < self.port < 65536) or not (0 <= self.unit_id <= 247):
            raise AdapterError("modbus_map_v1 必须配置 host、port（1–65535）与 unit_id（0–247）")
        if not settings.adapter_host_allowed(self.host):
            raise AdapterError(f"Modbus 设备主机 {self.host} 不在 ILCS_ADAPTER_ALLOWED_HOSTS 白名单")
        self.timeout = self._seconds("request_timeout_sec", 3.0, maximum=60)
        self._client = None
        self._io = threading.Lock()
        for name, point in self.points.items():
            self._validate(name, point)

    @staticmethod
    def _validate(name: str, point) -> None:
        if not isinstance(point, dict):
            raise AdapterError(f"点 {name} 必须是 {{table, address, type}}")
        table, kind = point.get("table", "holding"), point.get("type", "uint16")
        if table not in TABLES:
            raise AdapterError(f"点 {name} 的 table 只能是 {' / '.join(sorted(TABLES))}")
        if kind not in WIDTH and kind != "ascii":
            raise AdapterError(f"点 {name} 的 type 只能是 {' / '.join(sorted(WIDTH))} / ascii")
        address = point.get("address")
        if isinstance(address, bool) or not isinstance(address, int) or not 0 <= address <= 65535:
            raise AdapterError(f"点 {name} 的 address 必须是 0–65535 的整数（0 基地址）")
        if table in {"coil", "discrete"} and kind != "bool":
            raise AdapterError(f"点 {name} 在 {table} 表里只能是 bool")
        if kind == "ascii" and not (isinstance(point.get("length"), int) and 0 < point["length"] <= 240):
            raise AdapterError(f"点 {name} 是 ascii，必须给出字符数 length（≤240）")
        if point.get("word_order", "big") not in {"big", "little"}:
            raise AdapterError(f"点 {name} 的 word_order 只能是 big 或 little")

    def close(self) -> None:
        with self._io:
            self._drop()

    # ---------- 连接 ----------

    def _connect(self):
        if self._client is not None and self._client.connected:
            return self._client
        from pymodbus.client import ModbusTcpClient

        # retries=0：库内重试会把启动信号重发一遍
        client = ModbusTcpClient(self.host, port=self.port, timeout=self.timeout, retries=0)
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

    def _call(self, action: str, method: str, *args, **kwargs):
        from pymodbus.exceptions import ModbusException

        with self._io:
            client = self._connect()
            try:
                response = getattr(client, method)(*args, slave=self.unit_id, **kwargs)
            except (ModbusException, OSError) as exc:
                self._drop()
                raise AdapterUnreachable(f"{action}没有结论：{exc.__class__.__name__}") from exc
        if response.isError():
            raise AdapterError(f"{action}被设备拒绝：{response}")
        return response

    # ---------- 编解码 ----------

    @staticmethod
    def _decode(point: dict, words: list[int]):
        kind = point.get("type", "uint16")
        if point.get("word_order", "big") == "little" and kind in {"uint32", "int32", "float32"}:
            words = list(reversed(words))
        raw = b"".join(int(word).to_bytes(2, "big") for word in words)
        if kind == "uint16":
            return words[0]
        if kind == "int16":
            return struct.unpack(">h", raw)[0]
        if kind == "uint32":
            return struct.unpack(">I", raw)[0]
        if kind == "int32":
            return struct.unpack(">i", raw)[0]
        if kind == "float32":
            return struct.unpack(">f", raw)[0]
        if kind == "bool":
            return bool(words[0] >> int(point.get("bit", 0)) & 1)
        return raw.rstrip(b"\0").decode("ascii", errors="replace")

    @staticmethod
    def _encode(point: dict, value) -> list[int]:
        kind = point.get("type", "uint16")
        try:
            if kind == "uint16":
                raw = struct.pack(">H", int(round(float(value))))
            elif kind == "int16":
                raw = struct.pack(">h", int(round(float(value))))
            elif kind == "uint32":
                raw = struct.pack(">I", int(round(float(value))))
            elif kind == "int32":
                raw = struct.pack(">i", int(round(float(value))))
            elif kind == "float32":
                raw = struct.pack(">f", float(value))
            elif kind == "ascii":
                encoded = str(value).encode("ascii")
                if len(encoded) > point["length"]:
                    raise AdapterError(f"值 {value!r} 超过 {point['length']} 个 ASCII 字符")
                raw = encoded.ljust(point["length"] + point["length"] % 2, b"\0")
            else:
                raise AdapterError("保持寄存器里的位只读：写布尔请用线圈（coil）")
        except (struct.error, ValueError, TypeError, UnicodeEncodeError) as exc:
            raise AdapterError(f"值 {value!r} 不能编码成 {kind}：{exc}") from exc
        words = [int.from_bytes(raw[index:index + 2], "big") for index in range(0, len(raw), 2)]
        if point.get("word_order", "big") == "little" and kind in {"uint32", "int32", "float32"}:
            words = list(reversed(words))
        return words

    # ---------- I/O ----------

    def read_point(self, name: str):
        point = self.points[name]
        table, kind, address = point.get("table", "holding"), point.get("type", "uint16"), point["address"]
        if table in {"coil", "discrete"}:
            method = "read_coils" if table == "coil" else "read_discrete_inputs"
            try:
                response = self._call(f"读 {name} ", method, address, count=1)
            except AdapterError as exc:
                raise AdapterIndeterminate(str(exc)) from exc
            return bool(response.bits[0])
        count = (point["length"] + 1) // 2 if kind == "ascii" else WIDTH[kind]
        method = "read_holding_registers" if table == "holding" else "read_input_registers"
        try:
            response = self._call(f"读 {name} ", method, address, count=count)
        except AdapterError as exc:
            raise AdapterIndeterminate(str(exc)) from exc
        return self._decode(point, list(response.registers))

    def write_point(self, name: str, value) -> None:
        point = self.points[name]
        table, address = point.get("table", "holding"), point["address"]
        if table == "coil":
            self._call(f"写 {name} ", "write_coil", address, bool(value))
            return
        if table != "holding":
            raise AdapterError(f"点 {name} 在 {table} 表里，只读")
        words = self._encode(point, value)
        if len(words) == 1:
            self._call(f"写 {name} ", "write_register", address, words[0])
        else:
            self._call(f"写 {name} ", "write_registers", address, words)
