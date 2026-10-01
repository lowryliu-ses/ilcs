"""BTS 的调用面：驱动（driver/device.py）只依赖这里列出的几个方法。

真实实现是 `driver/bts.py`（经开源的 aurora-neware 走 BTS 8.0 的 TCP XML 接口），模拟实现是
`simulator/fake_bts.py`。两边返回同样的字段，驱动代码只有一份。

通道号（pipeline）写成 BTS 的「设备号-子设备号-通道号」，如 `21-1-3`。
"""
from __future__ import annotations

from typing import Any, Protocol


class BtsRefused(Exception):
    """BTS 明确拒绝或请求根本没发出去（通道不存在、工步文件不在、BTS 回 false）：通道没有动作。"""


class BtsOffline(BtsRefused):
    """连不上 BTS，启动命令没发出去：通道没有动作，过一会儿可以再试。"""


class Bts(Protocol):
    def info(self) -> dict[str, Any]:
        """{"pipelines": [BTS 上现有的通道号], "version": BTS 版本, "server": 连的是哪台, "simulator": 模拟设备才有,
        "interlock": 模拟设备注入联锁时才有}"""

    def channels(self, pipelines: list[str]) -> dict[str, dict[str, Any]]:
        """按通道读最新状态：{通道号: {"workstatus": working | pause | finish | stop | protect | …, "barcode",
        "cycle", "step", "step_type", "voltage", "current", "capacity", "energy", "log_code"}}。读不到的量是 None。"""

    def start(self, pipeline: str, barcode: str, step_file: str, save_dir: str) -> None:
        """在通道上按工步文件启动一个测试，条码写 `barcode`（启动没拿到应答时按它找回作业）。
        明确拒绝抛 `BtsRefused`；其他异常（超时、断线）表示不知道通道动没动。"""

    def stop(self, pipeline: str) -> None:
        """停止通道上的测试。明确拒绝抛 `BtsRefused`。"""
