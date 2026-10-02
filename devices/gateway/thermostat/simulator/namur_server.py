#!/usr/bin/env python
"""假搅拌板：TCP 上说 IKA 的 NAMUR 命令（照 ika-stirrer 的假板取了搅拌那一半），读命令回「数值 通道号」，写命令什么都不回。

    IN_NAME → `RCT digital`     IN_PV_4 → `300 4`     IN_SP_4 → `300 4`
    OUT_SP_4 n、START_4、STOP_4、START_1、STOP_1 → 不回

转速按固定加速度趋向设定值（关着降到 0）；设定值超出板子的范围会被压到范围里（和真板子一样），网关回读能发现。
测试用的开关：`mute`（一句都不回、写也不认：线断了）、`reset_on_start`（启动之后转速设定值复位，网关要回读重发）。

    python simulator/namur_server.py --port 4001
"""
from __future__ import annotations

import argparse
import math
import threading
import time

try:
    from .chillers import LineServer
except ImportError:  # 直接运行 python simulator/namur_server.py
    from chillers import LineServer


class FakePlate:
    def __init__(self, name: str = "RCT digital", *, max_speed: float = 1500.0, min_speed: float = 50.0,
                 ramp_rpm_s: float = 5000.0):
        self.name = name
        self.max_speed, self.min_speed, self.ramp_rpm_s = max_speed, min_speed, ramp_rpm_s
        self.speed, self.speed_setpoint = 0.0, 0.0
        self.motor = self.heater = False
        self.mute = False
        self.reset_on_start = False
        self.log: list[str] = []
        self.lock = threading.RLock()
        self.since = time.monotonic()

    def _advance(self) -> None:
        now = time.monotonic()
        dt, self.since = now - self.since, now
        goal = self.speed_setpoint if self.motor else 0.0
        step = self.ramp_rpm_s * dt
        self.speed = goal if abs(goal - self.speed) <= step else self.speed + math.copysign(step, goal - self.speed)

    def _clamp(self, value: float) -> float:
        return 0.0 if value <= 0 else max(self.min_speed, min(self.max_speed, value))

    def handle(self, line: str) -> str | None:
        command = line.strip()
        with self.lock:
            if self.mute:
                return None
            self.log.append(command)
            self._advance()
            if command == "IN_NAME":
                return self.name
            if command == "IN_PV_4":
                return f"{self.speed:.0f} 4"
            if command == "IN_SP_4":
                return f"{self.speed_setpoint:.0f} 4"
            if command.startswith("OUT_SP_4 "):
                try:
                    self.speed_setpoint = self._clamp(float(command.split()[1]))
                except (IndexError, ValueError):
                    pass
                return None
            if command == "START_4":
                self.motor = True
                if self.reset_on_start:
                    self.speed_setpoint = self.min_speed  # 型号的毛病：启动把设定值复位了
                return None
            if command == "STOP_4":
                self.motor = False
                return None
            if command in {"START_1", "STOP_1"}:
                self.heater = command == "START_1"
                return None
            return None  # 不认识的命令：真板子也不回

    def snapshot(self) -> dict:
        with self.lock:
            self._advance()
            return {"motor": self.motor, "heater": self.heater, "speed": self.speed,
                    "speed_setpoint": self.speed_setpoint}


class NamurServer:
    """一块假板一个 TCP 端口。"""

    def __init__(self, plate: FakePlate, host: str = "127.0.0.1", port: int = 0):
        self.plate = plate
        # 每行都去找 plate.handle：测试里换掉它（模拟型号的毛病、断线）也生效
        self.lines = LineServer(lambda line: self.plate.handle(line), host=host, port=port)
        self.port = self.lines.port
        self.thread = self.lines.thread

    def stop(self) -> None:
        self.lines.stop()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4001)
    args = parser.parse_args(argv)
    server = NamurServer(FakePlate(ramp_rpm_s=500), host=args.host, port=args.port)
    print(f"假搅拌板在 {args.host}:{server.port}")
    try:
        server.thread.join()
    except KeyboardInterrupt:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
