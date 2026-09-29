"""模拟接口：和厂家 SDK 同一组方法（driver/vendor_sdk.py），驱动代码一行不改就能对着它跑。

作业按时长完成；故障经网关的统一控制口注入（`faults`）：联锁、忙、回执丢失、做到最后报故障、一直不结束。
离线由网关服务自己处理（真的停止监听）。它模拟的是「一台会出问题的设备」，不是一个永远成功的桩。
"""
from __future__ import annotations

import hashlib
import itertools
import threading
import time
from typing import Any

from ilcs_gateway import FaultState, ReceiptLost

from driver.vendor_sdk import SdkError

# 模拟设备上有的程序：与真设备核对（驱动的 PROGRAMS 是 ILCS 侧能选的，这里是设备上真有的）
PROGRAMS = {"CC-CV", "GITT"}


class FakeVendorSdk:
    def __init__(self, serial: str = "SIM-CYC-M1", *, channels: int = 8, run_seconds: float = 5.0,
                 model: str = "CYCLER-32", vendor: str = "示例厂家（模拟）", firmware: str = "sim-2.3"):
        self.serial, self.model, self.vendor, self.firmware = serial, model, vendor, firmware
        self.channels = channels
        self.run_seconds = run_seconds
        self.faults = FaultState()
        self.runs: dict[str, dict[str, Any]] = {}
        self.counter = itertools.count(1001)
        self.lock = threading.RLock()

    def info(self) -> dict[str, Any]:
        return {"serial": self.serial, "model": self.model, "vendor": self.vendor, "firmware": self.firmware,
                "channels": self.channels, "estop": self.faults.interlock, "simulator": True}

    def free_channels(self) -> list[int]:
        with self.lock:
            busy = {run["channel"] for run in self.runs.values() if run["state"] in {"RUNNING", "PAUSED"}}
        return [channel for channel in range(1, self.channels + 1) if channel not in busy]

    def start_program(self, channel: int, program: str, settings: dict[str, float], tag: str) -> str:
        self.faults.check_start()
        if program not in PROGRAMS:
            raise SdkError(f"程序 {program} 不存在")
        with self.lock:
            run_id = f"RUN-{next(self.counter)}"
            mode = self.faults.moved()
            self.runs[run_id] = {"channel": channel, "program": program, "settings": dict(settings), "tag": tag,
                                 "state": "RUNNING", "elapsed": 0.0, "since": time.monotonic(), "fault": mode}
        if mode == "slow_submit":
            time.sleep(self.faults.parameter)
        if mode == "lost_receipt":
            raise ReceiptLost(run_id)
        return run_id

    def _advance(self, run: dict[str, Any]) -> None:
        if run["state"] != "RUNNING":
            run["since"] = time.monotonic()
            return
        run["elapsed"] += time.monotonic() - run["since"]
        run["since"] = time.monotonic()
        if run["fault"] == "stuck" or run["elapsed"] < self.run_seconds:
            return
        run["state"] = "ERROR" if run["fault"] == "fail" else "FINISHED"
        if run["fault"] == "fail":
            run["alarm"] = "通道过温报警，作业中止"

    def run_state(self, run_id: str) -> dict[str, Any]:
        with self.lock:
            run = self.runs.get(run_id)
            if run is None:
                raise SdkError(f"作业 {run_id} 不存在")
            self._advance(run)
            progress = min(1.0, run["elapsed"] / max(self.run_seconds, 1e-6))
            noise = 1 + (hashlib.sha256(run_id.encode()).digest()[0] / 255 - 0.5) / 100
            return {"state": run["state"], "cycles": int(10 * progress), "capacity_mAh": round(3.2 * progress * noise, 4),
                    "voltage": round(3.0 + (run["settings"].get("vmax", 4.2) - 3.0) * progress, 4),
                    "alarm": run.get("alarm", "")}

    def pause(self, run_id: str) -> None:
        with self.lock:
            run = self._running(run_id)
            self._advance(run)
            run["state"] = "PAUSED"

    def resume(self, run_id: str) -> None:
        with self.lock:
            run = self.runs.get(run_id)
            if run is None or run["state"] != "PAUSED":
                raise SdkError(f"作业 {run_id} 没有在暂停")
            run["state"], run["since"] = "RUNNING", time.monotonic()

    def stop(self, run_id: str) -> None:
        with self.lock:
            run = self.runs.get(run_id)
            if run is not None and run["state"] in {"RUNNING", "PAUSED"}:
                self._advance(run)
                run["state"] = "STOPPED"

    def find_run(self, tag: str) -> str | None:
        with self.lock:
            return next((run_id for run_id, run in self.runs.items() if run["tag"] == tag), None)

    def _running(self, run_id: str) -> dict[str, Any]:
        run = self.runs.get(run_id)
        if run is None or run["state"] != "RUNNING":
            raise SdkError(f"作业 {run_id} 不在运行")
        return run
