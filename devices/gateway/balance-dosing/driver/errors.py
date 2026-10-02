"""设备明确报出来的错：命令被拒、参数不对、过载、加样头问题、泵报错。和「链路断了、不知道设备收没收到」（`LinkError`）分开。"""
from __future__ import annotations


class DeviceError(Exception):
    """设备明确报错。"""


class DeviceBusy(DeviceError):
    """设备正忙（MT-SICS 的 I、Quantos 的「另一个作业在跑」）：这条命令没执行。"""
