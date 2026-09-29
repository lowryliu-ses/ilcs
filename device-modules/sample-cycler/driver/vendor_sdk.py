"""厂家 SDK 的调用面：驱动（driver/device.py）只依赖这里列出的几个方法。

样板是一台 8 通道充放电柜，厂家只给了 Windows SDK。真实的 SDK 通常是 DLL（经 pythonnet / ctypes 调用）
或厂家自己的 Python 包；模拟接口（simulator/fake_sdk.py）实现同一组方法。

接真机时要做的只有一件事：实现 `load_sdk()`，返回一个有下面这些方法的对象。别在这里改驱动的判断逻辑。
"""
from __future__ import annotations

import importlib
import os
from typing import Any, Protocol


class SdkError(Exception):
    """厂家 SDK 的明确错误：参数非法、程序不存在、通道不可用。设备没有动作。"""


class VendorSdk(Protocol):
    def info(self) -> dict[str, Any]:
        """{"serial", "model", "vendor", "firmware", "channels", "estop": 急停是否按下, "simulator": 模拟设备才有}"""

    def free_channels(self) -> list[int]:
        """此刻空闲的通道号。"""

    def start_program(self, channel: int, program: str, settings: dict[str, float], tag: str) -> str:
        """在通道上启动程序，返回设备的作业号。`tag` 写进设备侧的作业备注：启动没拿到应答时按它找回作业。"""

    def run_state(self, run_id: str) -> dict[str, Any]:
        """{"state": "RUNNING | PAUSED | FINISHED | ERROR | STOPPED", "cycles": int, "capacity_mAh": float,
        "voltage": float, "alarm": 故障说明}"""

    def pause(self, run_id: str) -> None: ...

    def resume(self, run_id: str) -> None: ...

    def stop(self, run_id: str) -> None: ...

    def find_run(self, tag: str) -> str | None:
        """按作业备注找作业号，找不到返回 None。"""


def load_sdk() -> VendorSdk:
    """接真机：按环境变量 VENDOR_SDK_MODULE 导入厂家 SDK 的包装模块，它要提供 `connect() -> VendorSdk`。"""
    name = os.environ.get("VENDOR_SDK_MODULE", "")
    if not name:
        raise SystemExit("没有配置厂家 SDK：设 VENDOR_SDK_MODULE=<包装模块>（提供 connect()），或加 --simulate 用模拟接口")
    module = importlib.import_module(name)
    return module.connect()
