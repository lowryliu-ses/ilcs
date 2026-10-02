"""模拟接口：假光谱仪（同一组方法），测量位上是一瓶碳酸酯 / LiPF6 电解液的合成拉曼谱，带故障注入。"""
from __future__ import annotations

from pathlib import Path

from driver.config import Config
from driver.device import Instrument

from .fake_spectrometer import FakeSpectrometer, default_config


def simulated_instrument(config_file: str | Path | None = None, *, state_dir: str | Path | None = None,
                         time_scale: float = 1.0) -> Instrument:
    """--simulate：假光谱仪 + 网关配置（不给配置文件就用 `default_config()`）。假光谱仪的激光波长跟配置走。"""
    config = Config.load(config_file) if config_file else Config.parse(default_config())
    return Instrument(FakeSpectrometer(laser_nm=config.laser_nm, time_scale=time_scale), config, state_dir=state_dir)


__all__ = ["FakeSpectrometer", "default_config", "simulated_instrument"]
