"""模拟接口：和真实 BTS 同一组方法（driver/bts_api.py），驱动代码一行不改就能对着它跑。

一个通道一行状态：启动后 working，按时长跑完变 finish；故障经网关的统一控制口注入（`faults`）：
联锁、忙、回执丢失、做到最后保护停机（protect）、一直不结束。`simulator/bts_server.py` 把同一个假 BTS 套上
BTS 的 TCP XML 接口，用来测真实接口（driver/bts.py + aurora-neware）。
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import threading
import time
from typing import Any

from ilcs_gateway import FaultState, ReceiptLost

from driver.bts_api import BtsRefused

STEPS = Path(__file__).resolve().parent / "steps"
ACTIVE = {"working", "pause"}


def default_config(device_id: str = "SIM-NEWARE-BTS", channels: int = 8) -> dict[str, Any]:
    """--simulate 没给 --config 时用的配置：一台 8 通道柜，白名单全开、自动挑通道，工步文件是模拟用的占位文件。"""
    return {
        "device_id": device_id, "model": "CT-4008T（模拟）", "vendor": "Neware（模拟）",
        "channels": [f"1-1-{index}" for index in range(1, channels + 1)], "auto_channel": True,
        "programs": {"CC-CV": {"name": "恒流恒压循环", "file": str(STEPS / "CC-CV.xml")},
                     "GITT": {"name": "恒电流间歇滴定", "file": str(STEPS / "GITT.xml")}},
        "default_program": "CC-CV",
    }


class FakeBts:
    def __init__(self, pipelines: list[str] | None = None, *, run_seconds: float = 5.0):
        self.pipelines = list(pipelines or [f"1-1-{index}" for index in range(1, 9)])
        self.run_seconds = run_seconds
        self.faults = FaultState()
        self.lock = threading.RLock()
        self.rows: dict[str, dict[str, Any]] = {
            pipeline: {"workstatus": "finish", "barcode": "", "step_file": "", "elapsed": 0.0,
                       "since": time.monotonic(), "fault": "none"} for pipeline in self.pipelines}

    def info(self) -> dict[str, Any]:
        return {"pipelines": list(self.pipelines), "version": "BTS 8.0（模拟）", "server": "fake",
                "simulator": True, "interlock": self.faults.interlock}

    def _row(self, pipeline: str) -> dict[str, Any]:
        row = self.rows.get(pipeline)
        if row is None:
            raise BtsRefused(f"BTS 上没有通道 {pipeline}")
        return row

    def _advance(self, row: dict[str, Any]) -> None:
        now = time.monotonic()
        if row["workstatus"] == "working":
            row["elapsed"] += now - row["since"]
            if row["fault"] != "stuck" and row["elapsed"] >= self.run_seconds:
                row["workstatus"] = "protect" if row["fault"] == "fail" else "finish"
        row["since"] = now

    def channels(self, pipelines: list[str]) -> dict[str, dict[str, Any]]:
        with self.lock:
            result = {}
            for pipeline in pipelines:
                row = self._row(pipeline)
                self._advance(row)
                result[pipeline] = self._public(pipeline, row)
            return result

    def _public(self, pipeline: str, row: dict[str, Any]) -> dict[str, Any]:
        progress = min(1.0, row["elapsed"] / max(self.run_seconds, 1e-6))
        noise = 1 + (hashlib.sha256((pipeline + row["barcode"]).encode()).digest()[0] / 255 - 0.5) / 100
        running = row["workstatus"] in ACTIVE
        return {
            "workstatus": row["workstatus"], "barcode": row["barcode"], "cycle": 1 + int(9 * progress),
            "step": 1 + int(3 * progress) if running else None, "step_type": "cc" if running else None,
            "voltage": round(3.0 + 1.2 * progress, 4), "current": 0.0015 if running else 0.0,
            "capacity": round(0.0032 * progress * noise, 6), "energy": round(0.012 * progress * noise, 6),
            "log_code": 302001 if row["workstatus"] == "protect" else 0,
        }

    def start(self, pipeline: str, barcode: str, step_file: str, save_dir: str) -> None:
        self.faults.check_start()
        with self.lock:
            row = self._row(pipeline)
            self._advance(row)
            if row["workstatus"] in ACTIVE:
                raise BtsRefused(f"通道 {pipeline} 在 {row['workstatus']}")
            mode = self.faults.moved()
            row.update(workstatus="working", barcode=barcode, step_file=step_file, elapsed=0.0,
                       since=time.monotonic(), fault=mode)
        if mode == "slow_submit":
            time.sleep(self.faults.parameter)
        if mode == "lost_receipt":
            raise ReceiptLost(pipeline)

    def stop(self, pipeline: str) -> None:
        with self.lock:
            row = self._row(pipeline)
            self._advance(row)
            if row["workstatus"] in ACTIVE:
                row["workstatus"] = "stop"

    # 测试用：模拟人工在 BTS 上操作
    def manual(self, pipeline: str, **changes: Any) -> None:
        with self.lock:
            self._row(pipeline).update(changes, since=time.monotonic())
