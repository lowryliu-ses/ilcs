"""电化学工作站的调用面：驱动（driver/device.py）只依赖这里的几样东西，换品牌只换后端。

一次测量分三步，正好对上 ILCS 契约里「明确没动 / 结果未知」的分界：

1. `prepare(plan)`：把测量交给仪器、**还不开始**（MethodSCRIPT：`l` 加载脚本，仪器做语法检查）。仪器拒收抛
   `BackendRejected`，链路不通抛 `BackendError`——两种都是仪器没动；
2. `begin()`：开始（MethodSCRIPT：`r`）。写都没写出去抛 `BackendError(sent=False)`：没动；写出去了没拿到确认抛
   `StartUnknown`：仪器可能已经在测，结果未知、不重发；
3. `stream(on_point)`：读测量输出直到结束，每个数据点回调一次，返回怎么结束的（`Finish`）；链路断了抛 `BackendError`。

`abort()` 让仪器停下（只发命令不等结果，`stream` 会看到结束）；`recover()` 在链路断过之后重连、停掉仪器上可能还在跑的
测量，确认仪器空闲。数据点用统一的键（SI 单位）：`e_V` 电位、`i_A` 电流、`t_s` 时间、`f_Hz` 频率、`z_re_ohm` /
`z_im_ohm` 阻抗实部 / 虚部（虚部按物理符号，容抗为负）。

已实现的后端：`driver/palmsens.py`（PalmSens MethodSCRIPT：EmStat Pico / EmStat4 / Nexus / Sensit）。别的品牌照这组方法
写一个（README「再接一个品牌」）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

TECHNIQUES = ("ocp", "lsv", "cv", "ca", "eis")
# 数据点状态位（MethodSCRIPT 元数据 1X 的含义；别的品牌映射到同样的位）
TIMING, OVERLOAD, UNDERLOAD, OVERLOAD_WARNING = 0x1, 0x2, 0x4, 0x8


class BackendError(Exception):
    """链路不通、仪器不应答。`sent`：出事时命令写出去了没有。"""

    def __init__(self, message: str, *, sent: bool = False):
        super().__init__(message)
        self.sent = sent


class BackendRejected(Exception):
    """仪器明确拒收（参数 / 脚本不对、不支持、忙）：仪器没动。`kind` 同 `ilcs_gateway.Rejected`。"""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


class StartUnknown(Exception):
    """开始命令写出去了，没拿到确认：仪器可能已经在测。"""


@dataclass(frozen=True)
class Ranging:
    """电流量程：起始量程（预计最大电流）与自动量程的上下限（相等 = 不自动换量程）；电位量程（测电位时，None = 仪器最大）。"""

    start_A: float
    min_A: float
    max_A: float
    potential_V: float | None = None


@dataclass(frozen=True)
class Plan:
    """一次测量：技术 + SI 单位的参数（键见 driver/techniques.py 的 SETTINGS）+ 量程 + 带宽。"""

    technique: str
    settings: dict[str, Any]
    ranging: Ranging
    bandwidth_Hz: float
    label: str = ""


@dataclass(frozen=True)
class Limits:
    """仪器的极限（按型号与 PGStat 模式）：电位范围、一次测量能跨多大的电位、EIS 最高频率与最大振幅、最大电流。"""

    e_min_V: float
    e_max_V: float
    window_V: float
    eis_max_hz: float | None = None
    eis_max_vrms: float | None = None
    i_max_A: float | None = None


@dataclass
class Point:
    """一个数据点。`segment`：哪一段（开路静置也算 ocp 段）；`scan`：CV 第几圈（0 起）；`status`：状态位。"""

    segment: str
    values: dict[str, float]
    scan: int = 0
    status: int = 0


@dataclass
class Finish:
    """测量怎么结束的：`done` 正常做完、`aborted` 被终止（仪器确认了）、`error` 仪器报错中止。
    `safe`：出错之后确认过电池已断开（cell off）。"""

    state: str
    error: str = ""
    safe: bool = True
    notes: list[str] = field(default_factory=list)


class Potentiostat(Protocol):
    def identity(self) -> dict[str, Any]:
        """{"serial", "model", "device_type", "firmware", "script_version", "simulator"}。读不到抛 `BackendError`。
        测量进行中不碰仪器，回上一次读到的。"""

    @property
    def ready(self) -> bool:
        """仪器上没有网关不知道的测量在跑（这个进程里和仪器同步过、现在不在断线重连）。网关重启前没出结论的作业，
        要等它为真才判失败。"""

    def limits(self, technique: str) -> Limits | None:
        """这台仪器做这个技术时的极限；认不出型号返回 None（只按网关配置的极限核对）。要先 `identity()` 过。"""

    def prepare(self, plan: Plan) -> None:
        ...

    def begin(self) -> None:
        ...

    def stream(self, on_point: Callable[[Point], None]) -> Finish:
        ...

    def abort(self) -> None:
        ...

    def recover(self) -> None:
        ...

    def close(self) -> None:
        ...
