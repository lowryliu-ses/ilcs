"""写「模拟接口」用的小工具：故障模式与动作计数。

设备模块的模拟接口推荐做成「假的厂家 SDK」——和真 SDK 同一组方法，驱动代码（driver/）一行不改，测试走的就是
真实的映射逻辑，只有最底下那层换成了假的。假 SDK 里放一个 `FaultState`：

- `check_start()`：联锁 / 忙时抛 `Rejected`（设备没动）；
- `moved()`：设备真的开始动作了一次（统一控制口报的 `motions`，验收据此判断重投有没有让设备再动一次）；
- `lose_receipt` / `fail` / `stuck`：动作了但应答丢掉、做到最后报故障、一直不结束。

离线（`offline`）由网关服务自己处理：真的停止监听 N 秒。
"""
from __future__ import annotations

import threading
from typing import Any

from .device import Rejected

SUPPORTED = {"none", "lost_receipt", "fail", "stuck", "interlock", "busy", "slow_submit"}


class FaultState:
    def __init__(self) -> None:
        self.mode = "none"
        self.parameter = 0.0
        self.motions = 0
        self.lock = threading.Lock()

    def set_fault(self, mode: str, parameter: float = 0.0) -> None:
        if mode not in SUPPORTED:
            raise ValueError(f"这台模拟设备不支持故障 {mode}；可选 {', '.join(sorted(SUPPORTED))}")
        with self.lock:
            self.mode, self.parameter = mode, float(parameter or 0)

    def fault_state(self) -> dict[str, Any]:
        with self.lock:
            return {"fault": self.mode, "fault_parameter": self.parameter, "motions": self.motions}

    @property
    def interlock(self) -> bool:
        return self.mode == "interlock"

    def check_start(self) -> None:
        """开始动作之前：联锁、忙都是明确拒绝，设备没动。"""
        with self.lock:
            mode = self.mode
        if mode == "interlock":
            raise Rejected("interlocked", "安全联锁触发，设备未动作")
        if mode == "busy":
            raise Rejected("busy", "设备忙，未接受作业")

    def moved(self) -> str:
        """设备真的开始动作了一次；返回动作那一刻的故障模式（lost_receipt / fail / stuck 由调用方处理）。"""
        with self.lock:
            self.motions += 1
            return self.mode
