"""模拟接口：和真实光谱仪同一组方法（driver/spectro_api.py），驱动代码一行不改就能对着它跑。

一台 785 nm 激发的拉曼光谱仪（QE Pro 一类：1024 像素、约 795–1000 nm），测量位上放着一瓶碳酸酯 / LiPF6 电解液：
谱 = 荧光基线 + 几个洛伦兹峰 + 噪声（散粒噪声 + 读出噪声）+ 电子暗电平，计数随积分时间线性增长，
到满量程 65535 削顶（饱和）。噪声用固定种子的随机数：同样的调用顺序得到同样的谱。
采一张谱阻塞「积分时间 × time_scale」秒（测试里调小）。只用标准库，模拟网关的容器里不装 numpy / seabreeze。

故障经网关的统一控制口注入（`faults`，驱动在启动时调 `check_start` / `moved`）：联锁、忙、回执丢失、
采完报错（fail）、一直不结束（stuck）、提交慢（slow_submit）。离线由网关服务自己处理（真的停止监听）。
"""
from __future__ import annotations

import math
import random
import threading
import time
from typing import Any

from ilcs_gateway import FaultState

from driver.spectro_api import SpectrometerError

# 碳酸酯（EC / EMC）+ LiPF6 电解液的拉曼峰：(拉曼位移 cm-1, 相对强度, 半高半宽 cm-1, 归属)
PEAKS = (
    (715.0, 0.30, 5.0, "EC 环变形"),
    (741.0, 0.45, 4.0, "PF6⁻ P–F 对称伸缩"),
    (893.0, 1.00, 5.0, "EC 环呼吸（游离 EC）"),
    (904.0, 0.40, 5.5, "EC 环呼吸（Li⁺ 溶剂化的 EC）"),
    (1090.0, 0.18, 8.0, "C–O 伸缩"),
    (1450.0, 0.35, 10.0, "CH₂ / CH₃ 变形"),
    (1770.0, 0.28, 9.0, "C=O 伸缩"),
)
PEAK_COUNTS_PER_S = 15000.0   # 最强峰（893 cm-1）每秒的计数：积分 1 s 约四分之一满量程，4 s 以上削顶
FLUORESCENCE_PER_S = 1500.0   # 荧光基线每秒的计数
DARK_LEVEL = 1000.0           # 电子暗电平（与积分时间无关）
DARK_CURRENT_PER_S = 20.0
READ_NOISE = 8.0


def default_config(device_id: str = "SIM-RAMAN-01") -> dict[str, Any]:
    """--simulate 没给 --config 时用的配置（与 simulator/raman-sim.json 相同）：785 nm，积分 1 s，读原始计数；
    repeats 最多 5 次（与电解液线拉曼工位的极限一致）。"""
    return {
        "device_id": device_id, "model": "QE Pro", "vendor": "Ocean Insight", "capability": "cap.ely.raman",
        "laser": {"kind": "external", "wavelength_nm": 785},
        "integration_ms": 1000, "max_integration_ms": 10000, "max_repeats": 5, "shift_range_cm1": [150, 2000],
        "correct_dark_counts": False, "correct_nonlinearity": False,
        "programs": {"RAMAN": {"name": "拉曼谱（785 nm，积分 1 s）", "integration_ms": 1000},
                     "RAMAN-FAST": {"name": "快速拉曼谱（积分 0.3 s）", "integration_ms": 300}},
        "default_program": "RAMAN",
    }


def _calibration(pixels: int, start_nm: float, end_nm: float) -> list[float]:
    """像素 → 波长：和真光谱仪一样是条略弯的多项式（中间偏离直线约 0.75%）。"""
    last = pixels - 1
    return [start_nm + (end_nm - start_nm) * (u + 0.03 * u * (1 - u)) for u in (index / last for index in range(pixels))]


def _signal(shift: float) -> float:
    """每秒的计数：荧光基线（宽的鼓包）+ 洛伦兹峰。"""
    baseline = FLUORESCENCE_PER_S * (1.0 + 0.5 * math.exp(-((shift - 1400.0) / 900.0) ** 2))
    peaks = sum(height / (1.0 + ((shift - center) / width) ** 2) for center, height, width, _ in PEAKS)
    return baseline + PEAK_COUNTS_PER_S * peaks


class FakeSpectrometer:
    def __init__(self, *, serial: str = "ILCS-SIMULATOR-RAMAN", model: str = "QE-PRO", laser_nm: float = 785.0,
                 pixels: int = 1024, start_nm: float = 795.0, end_nm: float = 1000.0, max_intensity: float = 65535.0,
                 limits_us: tuple[int, int] = (8_000, 60_000_000), seed: int = 7, time_scale: float = 1.0,
                 dark_pixels: bool = True):
        self.serial, self.model = serial, model
        self.laser_nm = laser_nm
        self.full_scale = float(max_intensity)
        self.limits_us = limits_us
        self.time_scale = time_scale
        self.dark_pixels = dark_pixels  # 有没有遮光像素：没有的型号不能扣电子暗电平
        self.faults = FaultState()
        self.random = random.Random(seed)
        self.lock = threading.Lock()
        self.integration_us = 100_000
        self.scans = 0
        self.closed = 0
        self.wavelength_nm = _calibration(pixels, start_nm, end_nm)
        self.rate = [_signal(1e7 / laser_nm - 1e7 / value) for value in self.wavelength_nm]

    def identity(self) -> dict[str, Any]:
        return {"serial": self.serial, "model": self.model, "firmware": "模拟 1.0", "pixels": len(self.wavelength_nm),
                "max_intensity": self.full_scale, "simulator": True, "interlock": self.faults.interlock}

    def integration_limits_us(self) -> tuple[int, int]:
        return self.limits_us

    def set_integration_us(self, us: int) -> None:
        low, high = self.limits_us
        if not low <= int(us) <= high:
            raise SpectrometerError(f"积分时间 {us} µs 不在 {low}–{high} µs 内")
        with self.lock:
            self.integration_us = int(us)

    def wavelengths(self) -> list[float]:
        return list(self.wavelength_nm)

    def intensities(self, dark: bool, nonlinearity: bool) -> list[float]:
        """`nonlinearity` 不改读数：假光谱仪本来就是线性的。"""
        if dark and not self.dark_pixels:
            raise SpectrometerError("This device does not support dark count correction.")
        seconds = self.integration_us / 1e6
        time.sleep(seconds * self.time_scale)
        with self.lock:
            gauss = self.random.gauss
            offset = DARK_LEVEL + DARK_CURRENT_PER_S * seconds
            counts = []
            for rate in self.rate:
                expected = rate * seconds + offset
                # 散粒噪声（信号与暗电流）+ 读出噪声；ADC 出整数，到满量程削顶
                value = expected + gauss(0.0, math.sqrt(expected - DARK_LEVEL + READ_NOISE ** 2))
                counts.append(float(min(self.full_scale, max(0.0, round(value)))))
            if dark:
                estimate = offset + gauss(0.0, READ_NOISE / 4)  # 遮光像素的平均
                counts = [value - estimate for value in counts]
            self.scans += 1
            return counts

    def max_intensity(self) -> float:
        return self.full_scale

    def close(self) -> None:
        self.closed += 1
