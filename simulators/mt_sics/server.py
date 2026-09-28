"""ILCS MT-SICS 天平模拟设备（TCP，行结束符 CR LF），系统侧用 `mt_sics_v1` 驱动接入。

实现 MT-SICS 通用命令：`I2` 型号与量程、`I3` 软件版本、`I4` 序列号、`I10` 天平编号、`S` 稳定重量、
`SI` 立即读数、`T` 去皮、`Z` 置零、`@` 复位；不认识的命令回 `ES`。秤盘上的重量是 `--sample-mass`
（缺省 0.0152 g）加一个按称重次数确定的微小偏差，可以用模拟器专用命令 `SIM:LOAD <克>` 改。

故障（`SIM:FAULT <模式> [参数]`）：`busy` / `interlock` → `S I`（不能执行）；`fail` → `S +`（超载）；
`slow_submit` → 称重回复迟到 N 秒；`lost_receipt` → 称了但不回复；`offline` → 断开 N 秒。
`SIM:STATE?` 返回故障、称重次数（`weighings`）与当前秤盘重量。

    python simulators/mt_sics/server.py --device-id SIM-BAL-01 --port 4305 --sample-mass 0.0152
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:  # 直接运行（容器）时也能找到 simulators 包
    sys.path.insert(0, str(ROOT))

from simulators.common.device import FAULTS  # noqa: E402
from simulators.common.linesrv import LineServer  # noqa: E402
from simulators.common.runtime import configure_logging  # noqa: E402

MARK = "ILCS-SIMULATOR"


class Balance:
    """一台 0.01 mg 读数的分析天平。"""

    def __init__(self, device_id: str, model: str, sample_mass: float, capacity: float = 220.0):
        self.device_id = device_id
        self.model = model
        self.capacity = capacity
        self.load = sample_mass
        self.tare = 0.0
        self.zero = 0.0
        self.fault = "none"
        self.parameter = 0.0
        self.weighings = 0
        self.lock = threading.Lock()
        self.runner: "SimulatorRunner | None" = None

    def reading(self) -> float:
        digest = hashlib.sha256(f"{self.device_id}:{self.weighings}".encode()).digest()
        noise = (digest[0] / 255 - 0.5) * 0.00002  # ±0.01 mg
        return round(self.load - self.tare - self.zero + noise, 5)

    def state(self) -> dict:
        return {"device_id": self.device_id, "fault": self.fault, "fault_parameter": self.parameter,
                "weighings": self.weighings, "load_g": self.load, "tare_g": self.tare}

    def handle(self, line: str) -> str | None:
        text = line.strip()
        if not text:
            return None
        if text.startswith("SIM:"):
            return self.simulator(text)
        command = text.split()[0].upper()
        with self.lock:
            fault = self.fault
        if command == "I2":
            return f'I2 A "{self.model} {self.capacity:.5f} g"'
        if command == "I3":
            return f'I3 A "2.1.0 {MARK}"'
        if command == "I4":
            return f'I4 A "{self.device_id}"'
        if command == "I10":
            return f'I10 A "{self.device_id}"'
        if command == "@":
            return f'I4 A "{self.device_id}"'
        if command in {"S", "SI"}:
            if fault in {"busy", "interlock"}:
                return "S I"
            if fault == "fail" or self.load > self.capacity:
                return "S +"
            with self.lock:
                self.weighings += 1
                value = self.reading()
            if fault == "slow_submit":
                time.sleep(self.parameter)
            if fault == "lost_receipt":
                return None
            return f"S S {value:>12.5f} g"
        if command == "T":
            if fault in {"busy", "interlock"}:
                return "T I"
            with self.lock:
                self.tare = self.load - self.zero
            return f"T S {self.tare:>12.5f} g"
        if command == "Z":
            if fault in {"busy", "interlock"}:
                return "Z I"
            with self.lock:
                self.zero = self.load - self.tare
            return "Z A"
        return "ES"

    def simulator(self, text: str) -> str:
        parts = text.split()
        if parts[0] == "SIM:STATE?":
            return json.dumps(self.state())
        if parts[0] == "SIM:LOAD" and len(parts) == 2:
            with self.lock:
                self.load = float(parts[1])
            return "OK"
        if parts[0] == "SIM:FAULT" and len(parts) >= 2 and parts[1] in FAULTS:
            parameter = float(parts[2]) if len(parts) > 2 else 0.0
            if parts[1] == "offline":
                if self.runner is not None:
                    self.runner.server.go_offline(parameter or 5)
                return "OK"
            with self.lock:
                self.fault, self.parameter = parts[1], parameter
            return "OK"
        return "ES"


class SimulatorRunner:
    def __init__(self, args: argparse.Namespace, balance: Balance):
        self.args = args
        self.balance = balance
        balance.runner = self
        self.server = LineServer(args.address, args.port, balance, newline=b"\r\n")

    def start(self) -> None:
        self.server.start()

    def stop(self) -> None:
        self.server.stop()


def parse(argv=None) -> argparse.Namespace:
    env = os.environ.get
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--device-id", default=env("SIM_DEVICE_ID", "SIM-BAL-01"))
    parser.add_argument("--model", default=env("SIM_MODEL", "XPR226"))
    parser.add_argument("--address", default=env("SIM_ADDRESS", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(env("SIM_PORT", "4305")))
    parser.add_argument("--sample-mass", type=float, default=float(env("SIM_SAMPLE_MASS", "0.0152")))
    return parser.parse_args(argv)


def main(argv=None) -> int:
    configure_logging()
    args = parse(argv)
    runner = SimulatorRunner(args, Balance(args.device_id, args.model, args.sample_mass))
    runner.start()
    stop = threading.Event()
    import signal

    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    stop.wait()
    runner.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
