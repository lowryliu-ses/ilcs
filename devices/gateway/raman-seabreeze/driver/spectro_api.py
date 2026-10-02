"""光谱仪的调用面：驱动（driver/device.py）只依赖这里列出的几个方法。

真实实现是 `driver/seabreeze_spec.py`（经开源的 python-seabreeze 走 USB），模拟实现是
`simulator/fake_spectrometer.py`。两边返回同样的东西，驱动代码只有一份。

光谱仪只管采谱：激光由外部开关与联锁，不在这一层（见 README「激光」）。
"""
from __future__ import annotations

from typing import Any, Protocol


class SpectrometerError(Exception):
    """光谱仪报错或连不上（厂家库报错、USB 断开、型号不支持某项校正）。消息里写明原因。"""


class Spectrometer(Protocol):
    def identity(self) -> dict[str, Any]:
        """{"serial", "model", "firmware", "pixels", "max_intensity"}；模拟设备另有 "simulator": True，注入联锁时
        "interlock": True。连不上就抛 `SpectrometerError`。采谱期间也要答得上来（健康检查），不能等这一张读完。"""

    def integration_limits_us(self) -> tuple[int, int]:
        """这台光谱仪允许的积分时间（微秒）：(下限, 上限)。"""

    def set_integration_us(self, us: int) -> None:
        """设积分时间（微秒）。"""

    def wavelengths(self) -> list[float]:
        """每个像素的波长（nm），与 `intensities` 一一对应。"""

    def intensities(self, dark: bool, nonlinearity: bool) -> list[float]:
        """采一张谱（阻塞约一个积分时间），每个像素的计数。`dark` 扣电子暗电平（遮光像素），
        `nonlinearity` 做非线性校正；型号不支持时抛 `SpectrometerError`。"""

    def max_intensity(self) -> float:
        """满量程计数（饱和值）。"""

    def close(self) -> None:
        """释放设备（USB 同一时刻只能被一个程序打开）。"""
