"""设备适配器契约。

一个适配器要回答的问题是固定的：
- 支持哪些能力和参数（`contract.capabilities`）；
- 提交命令（`submit`）并确认接受；
- 查询状态（`query`）——真实设备不支持可靠查询时要如实声明；
- 是否支持保持、终止、设备端去重；
- 回执里必须带原 command_id、设备时间戳和质量标记。

`AdapterUnreachable` 与「动作失败」是两件事：网络超时说明结果未知，
不能直接当失败去重试。调用方据此把命令留在 maybe_sent，等对账或人工核查。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


class AdapterError(Exception):
    """设备明确拒绝或明确失败。结论是确定的，可以按恢复规则处理。"""


class AdapterUnreachable(Exception):
    """网络或设备无响应。结果未知——不能直接认定失败可重试。"""


class AdapterIndeterminate(AdapterUnreachable):
    """设备侧有响应，但无法据此确认结果：重复投递冲突、限流、回执格式不合规。

    这些情况下设备可能已经在动作，所以按「结果未知」处理，而不是明确失败。
    """


@dataclass(frozen=True)
class AdapterContract:
    kind: str  # simulation | real
    protocol: str
    version: str = ""
    capabilities: tuple[str, ...] = ()
    supports_hold: bool = True
    supports_abort: bool = True
    supports_query: bool = True
    supports_dedup: bool = True
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "protocol": self.protocol,
            "version": self.version,
            "capabilities": list(self.capabilities),
            "supports_hold": self.supports_hold,
            "supports_abort": self.supports_abort,
            "supports_query": self.supports_query,
            "supports_dedup": self.supports_dedup,
            "note": self.note,
        }


@dataclass(frozen=True)
class CommandRequest:
    command_id: str
    station_id: str
    capability: str
    params: dict
    type: str  # dispatch | resume | retry | hold | abort
    batch_id: str
    step_index: int
    step_id: str = ""
    # 保持 / 终止针对的在途动作指令；设备侧据此确认要停的是哪一个动作
    target_command_id: str = ""
    # 步骤引用的设备方法：{id, code, version, name, program}；驱动按 program 选设备端程序
    method: dict = field(default_factory=dict)


@dataclass(frozen=True)
class CommandResult:
    command_id: str
    state: str  # accepted | running | done | failed | unknown
    device_ts: datetime | None = None
    quality: str = "good"
    delivered: dict = field(default_factory=dict)
    telemetry: tuple[tuple[str, float, float | None], ...] = ()
    error: str = ""
    origin: str = "simulation"

    def as_dict(self) -> dict:
        return {
            "command_id": self.command_id,
            "state": self.state,
            "device_ts": self.device_ts.isoformat(timespec="seconds") if self.device_ts else None,
            "quality": self.quality,
            "delivered": self.delivered,
            "error": self.error,
            "origin": self.origin,
        }


class DeviceAdapter(Protocol):
    contract: AdapterContract

    def healthcheck(self) -> dict:
        """验证驱动配置与设备连通性；失败必须抛异常，不能返回假在线。"""

    def submit(self, request: CommandRequest) -> CommandResult:
        """提交并确认接受。抛 AdapterUnreachable 表示结果未知。"""

    def query(self, command_id: str) -> CommandResult | None:
        """按原 command_id 查询设备侧状态。不支持可靠查询时返回 None。"""

    def hold(self, request: CommandRequest) -> CommandResult: ...

    def abort(self, request: CommandRequest) -> CommandResult: ...
