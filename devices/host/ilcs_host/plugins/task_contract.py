"""按 ILCS 任务契约编程的设备：Modbus 任务寄存器（插件 `modbus_task`，`devices/contracts/modbus/TaskRegisters.json`）、
OPC UA TaskExecution 节点（插件 `opcua_task`，`devices/contracts/opcua/TaskExecution.json`）。

和映射插件不一样，这类设备自己记指令：去重、按指令号查询、保持 / 终止都在设备侧，插件只做编解码。所以驱动宿主
不给它们记作业台账，它们也没有点表：
- 提交时连不上（`AdapterUnreachable`）一律回「结果未知」：分不清是连接没建起来，还是触发已经写下了——之后按指令号
  问设备，设备说没收到才算没下发；
- 设备状态：契约里没有「运行中」这一项，`Status.State` 报 unknown；在跑哪条指令按指令号问设备；
- 失联时延：Modbus 设备的心跳计数多久不变算停了（`heartbeat_stale_sec`），探测按它判。
"""
from __future__ import annotations

import threading

from .base import AdapterError, CommandRequest, CommandResult
from .modbus_task import ModbusTcpAdapter
from .opcua_task import OpcUaAdapter


class _LedgerOnTheDevice:
    """契约设备自己记指令，宿主这边没有台账：`current` 没有在途作业，`find` 一律当作「可能已经提交」。"""

    def current(self) -> None:
        return None

    def find(self, command_id: str) -> dict:
        return {"id": command_id, "state": "unknown"}


class TaskContractDevice:
    driver_class: type = object
    tasks = True
    handoff = "sync"  # 契约规定提交时就答接不接

    def __init__(self, record, journal_key: str = ""):
        self.driver = self.driver_class(record)
        self.config = dict(record.config or {})
        self.journal = _LedgerOnTheDevice()
        self._lock = threading.RLock()
        self.heartbeat_stale = float(getattr(self.driver, "heartbeat_stale", 0) or 0)

    # ---------- 身份与状态 ----------

    def identity(self) -> dict:
        health = self.driver.healthcheck()
        return {key: health[key] for key in ("device_id", "model", "vendor", "serial", "firmware", "simulator",
                                              "interlock", "accepts_commands") if key in health}

    def device_state(self) -> str:
        return "unknown"

    # ---------- 点位：契约设备没有点表 ----------

    def point_specs(self) -> dict:
        return {}

    def point_catalog(self) -> list:
        return []

    def read_points(self, names=None) -> list:
        raise AdapterError("按 ILCS 任务契约接的设备没有点表", code="UnknownPoint")

    def write_point_manually(self, name: str, value) -> dict:
        raise AdapterError("按 ILCS 任务契约接的设备没有点表", code="UnknownPoint")

    # ---------- 能力目录 ----------

    def accepted_params(self, spec) -> set[str] | None:
        return None

    def parameters_schema(self, capability: str) -> dict:
        """能力参数：Modbus 契约按配置的参数槽位（都是数），OPC UA 契约由设备自己核对。"""
        slots = self.config.get("params") if isinstance(self.config.get("params"), dict) else {}
        if not slots:
            return {"type": "object"}
        return {"type": "object", "properties": {name: {"type": "number"} for name in sorted(slots)},
                "additionalProperties": False}

    # ---------- 指令 ----------

    def submit(self, request: CommandRequest) -> CommandResult:
        return self.driver.submit(request)

    def query(self, command_id: str) -> CommandResult | None:
        return self.driver.query(command_id)

    def hold(self, request: CommandRequest) -> CommandResult:
        return self.driver.hold(request)

    def abort(self, request: CommandRequest) -> CommandResult:
        return self.driver.abort(request)

    def close(self) -> None:
        close = getattr(self.driver, "close", None)
        if close is not None:
            close()


class ModbusTaskDevice(TaskContractDevice):
    driver_class = ModbusTcpAdapter


class OpcUaTaskDevice(TaskContractDevice):
    driver_class = OpcUaAdapter
