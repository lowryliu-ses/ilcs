"""批次活性：运行中的批次必须总有可推进的对象。

可推进的对象是开着的步骤实例（待办、执行中、等待）、还会被投递或正在设备上的指令，以及待处理的推进事件。
被保持挂起的设备步骤只有在同一载具上另一个设备步骤正在用板时才算「在等」：批次在运行、板也空着，
它却一直挂着，就没有任何事件会再把它开出来。

没有可推进对象的运行中批次会一直停在「运行中」，界面上看不出异常。执行器的活性检查与回归用例的
不变量断言共用这里的判定。
"""
from __future__ import annotations

from typing import Any, Iterable

from .steps import labware_role
from .workflow import OPEN_STATES, PENDING

DEVICE = "device"
# 还会被投递（排队中）或可能仍在设备上的指令；结果未知的指令会让批次转入故障，不在运行中出现
LIVE_COMMAND_STATES = ("accepted", "running", "held")


def _queued(command: Any) -> bool:
    return command.state == "sent" and command.delivery_state == "queued"


def _plate_busy(row: Any, rows: list[Any]) -> bool:
    role = labware_role(row.step_snapshot or {})
    return any(
        other is not row and other.kind == DEVICE and other.state in {"ready", "running"}
        and labware_role(other.step_snapshot or {}) == role
        for other in rows
    )


def stall_reason(batch_state: str, runs: Iterable[Any], commands: Iterable[Any], pending_events: int) -> str:
    """运行中的批次为什么推进不下去；推进得下去返回空串。

    `runs` 是批次全部步骤实例，`commands` 是批次全部指令，`pending_events` 是还没处理完的推进事件数。
    """
    if batch_state != "running" or pending_events:
        return ""
    if any(_queued(command) or command.state in LIVE_COMMAND_STATES for command in commands):
        return ""
    open_rows = [row for row in runs if row.state in OPEN_STATES]
    parked = []
    for row in open_rows:
        if row.state != PENDING or row.kind != DEVICE:
            # 待办、执行中、等待中的节点在等人、等设备或等时间
            return ""
        if _plate_busy(row, open_rows):
            return ""
        parked.append(row)
    if parked:
        steps = "、".join(str(row.step_index + 1) for row in sorted(parked, key=lambda r: r.step_index))
        return f"第 {steps} 步设备步骤挂起等待，但批次在运行、载具空闲，没有任何事件会再开出它"
    return "没有开着的步骤、在途指令或待处理事件"
