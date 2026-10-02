"""设备模块要实现的接口。

设备开发者只写「怎么让设备动、怎么读状态、怎么停」；ILCS 网关契约里容易写错的部分——按指令号去重与回放、
先落盘再动设备、按指令号查询、回执丢了宁可不回、身份与方法目录——由 SDK（`gateway.py`）负责。

异常的含义是契约的一部分，别混用：
- `Rejected`：设备明确拒绝、没有动作（参数非法、不支持、联锁、忙）。网关回 422 / 423，ILCS 判明确失败；
- `ReceiptLost`：设备已经动作，但应答要丢掉——只给模拟设备模拟「回执丢失」用；
- 其他任何异常（SDK 超时、断线、厂家库报错）：不知道设备动没动。SDK 按结果未知处理，**绝不重发**。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 拒绝类别 → (契约里的错误名, HTTP 状态码)
REJECTIONS = {
    "invalid": ("InvalidParameters", 422), "unsupported": ("NotSupported", 422),
    "interlocked": ("Interlocked", 423), "busy": ("DeviceBusy", 423),
}
STATES = ("running", "held", "done", "failed")


class Rejected(Exception):
    """设备明确拒绝、没有动作。`kind` 取 invalid / unsupported / interlocked / busy。"""

    def __init__(self, kind: str, message: str):
        if kind not in REJECTIONS:
            raise ValueError(f"拒绝类别只能是 {', '.join(REJECTIONS)}")
        super().__init__(message)
        self.kind = kind
        self.message = message


class ReceiptLost(Exception):
    """设备已经动作、应答却要丢掉：只给模拟设备模拟回执丢失。`handle` 是设备已经开始的作业号。"""

    def __init__(self, handle: str, message: str = "设备已经动作，应答在返回途中丢失"):
        super().__init__(message)
        self.handle = handle


@dataclass
class Job:
    """一条 ILCS 动作指令。`handle` 是设备自己的作业号（`start` 返回的），没有就用指令号。"""

    command_id: str
    capability: str
    params: dict[str, Any]
    type: str = "dispatch"
    batch_id: str = ""
    step_id: str = ""
    step_index: int = 0
    # 步骤引用的设备方法：{id, code, version, name, program}；按 program 选设备上的程序
    method: dict[str, Any] = field(default_factory=dict)
    handle: str = ""
    # 这一步投的料：{name, unit, param}（物料名、单位、用量取哪个参数）；不投料的步骤为空
    material: dict[str, Any] = field(default_factory=dict)

    @property
    def program(self) -> str:
        return str((self.method or {}).get("program") or "")


@dataclass
class Status:
    """设备侧作业的状态。`actuals` 是实测值（写进回执的 delivered），`telemetry` 是 [{metric, value, setpoint}]。"""

    state: str
    actuals: dict[str, Any] = field(default_factory=dict)
    telemetry: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""

    def __post_init__(self) -> None:
        if self.state not in STATES:
            raise ValueError(f"设备状态只能是 {' / '.join(STATES)}，不是 {self.state!r}")


class Device:
    """设备模块实现这几个方法。一台设备一个实例；SDK 保证同一时刻只有一个线程在调它。"""

    def identity(self) -> dict[str, Any]:
        """设备身份：device_id、model、vendor、firmware，可选 serial、methods（[{program, name, capability}]）、
        interlock（联锁触发）、accepts_commands（接不接指令）、simulator（模拟设备必须报 True）。读不到就抛异常。"""
        raise NotImplementedError

    def start(self, job: Job) -> str:
        """让设备开始这个作业，返回设备自己的作业号（没有就返回 job.command_id）。"""
        raise NotImplementedError

    def status(self, job: Job) -> Status:
        """按 job.handle 读设备侧作业状态。"""
        raise NotImplementedError

    def hold(self, job: Job) -> None:
        raise Rejected("unsupported", "设备不支持保持")

    def resume(self, job: Job) -> None:
        raise Rejected("unsupported", "设备不支持恢复")

    def abort(self, job: Job) -> None:
        raise Rejected("unsupported", "设备不支持终止")

    def lookup(self, job: Job) -> str | None:
        """启动命令没拿到应答时，按指令号在设备侧找回作业号（设备支持时实现，例如任务备注里带了指令号）。"""
        return None

    def fault_target(self) -> Any | None:
        """模拟设备：返回一个带 `set_fault(mode, parameter)` 与 `fault_state()` 的对象（统一控制口用）。真实设备返回 None。"""
        return None
