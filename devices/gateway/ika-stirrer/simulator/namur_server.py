#!/usr/bin/env python
"""假加热板：TCP 上说 IKA 的 NAMUR 命令，和真板子（经串口服务器）一样只回读命令、写命令什么都不回。

    IN_NAME → `RCT digital`     IN_PV_1 / IN_PV_2 / IN_PV_4 → `25.3 1` / `80.1 2` / `300 4`
    IN_SP_1 / IN_SP_4 → 设定值     OUT_SP_1 x、OUT_SP_4 x、START_1、STOP_1、START_4、STOP_4、RESET → 不回
    OUT_SP_12@t、OUT_SP_42@n、OUT_WD2@m → 回显设的值（看门狗模式 2）

物理模型：加热开着，加热盘温度按一阶惯性趋向设定值（关着趋向室温），瓶里液体（外置探头）再跟着加热盘走；
转速按固定加速度趋向设定值（关着降到 0）。设定值超出板子的范围会被压到范围里（和真板子一样），网关回读能发现。
看门狗模式 2：`OUT_WD2@m` 之后 m 秒内没再收到它，设定值回落到安全值。

测试用的开关：`mute`（一句都不回、写也不认：线断了）、`reset_on_start`（有的型号 START_1 之后温度设定值会复位）。

    # 现场没有板子时，起一块假板，再让网关（不带 --simulate）用 {"kind": "tcp", "host": "127.0.0.1", "port": 4001} 连它
    python simulator/namur_server.py --port 4001
"""
from __future__ import annotations

import argparse
import math
import socketserver
import threading
import time


class FakePlate:
    def __init__(self, name: str = "RCT digital", *, ambient: float = 25.0, max_temp: float = 310.0,
                 max_speed: float = 1500.0, min_speed: float = 50.0, tau_s: float = 1.0, ramp_rpm_s: float = 3000.0):
        self.name = name
        self.ambient, self.max_temp, self.max_speed, self.min_speed = ambient, max_temp, max_speed, min_speed
        self.tau_s, self.ramp_rpm_s = tau_s, ramp_rpm_s
        self.plate = self.medium = ambient
        self.speed = 0.0
        self.temp_setpoint, self.speed_setpoint = ambient, 0.0
        self.heater = self.motor = False
        self.safe_temp, self.safe_speed = ambient, 0.0
        self.watchdog, self.fed, self.watchdog_event = 0, 0.0, False
        self.mute = False
        self.reset_on_start = False
        self.log: list[str] = []
        self.lock = threading.RLock()
        self.since = time.monotonic()

    # ---------- 物理 ----------

    def _advance(self) -> None:
        now = time.monotonic()
        dt, self.since = now - self.since, now
        if self.watchdog and not self.watchdog_event and now - self.fed > self.watchdog:
            self.watchdog_event = True  # 模式 2：设定值回落到安全值，加热、搅拌照开
            self.temp_setpoint, self.speed_setpoint = self.safe_temp, self.safe_speed
        # 只能加热：加热开着趋向设定值（低于室温的设定值也到不了室温以下），关着自然冷到室温
        target = max(self.temp_setpoint, self.ambient) if self.heater else self.ambient
        alpha = 1 - math.exp(-dt / max(self.tau_s, 1e-6))
        self.plate += (target - self.plate) * alpha
        self.medium += (self.plate - self.medium) * (1 - math.exp(-dt / max(2 * self.tau_s, 1e-6)))
        goal = self.speed_setpoint if self.motor else 0.0
        step = self.ramp_rpm_s * dt
        self.speed = goal if abs(goal - self.speed) <= step else self.speed + math.copysign(step, goal - self.speed)

    def _clamp_temp(self, value: float) -> float:
        return max(0.0, min(self.max_temp, value))

    def _clamp_speed(self, value: float) -> float:
        return 0.0 if value <= 0 else max(self.min_speed, min(self.max_speed, value))

    # ---------- 命令 ----------

    def handle(self, line: str) -> str | None:
        """一行命令 → 应答（不含行尾）；写命令返回 None（不回）。"""
        command = line.strip()
        with self.lock:
            if self.mute:
                return None
            self.log.append(command)
            self._advance()
            if command == "IN_NAME":
                return self.name
            readings = {"IN_PV_1": (self.medium, 1, 1), "IN_PV_2": (self.plate, 2, 1), "IN_PV_4": (self.speed, 4, 0),
                        "IN_SP_1": (self.temp_setpoint, 1, 1), "IN_SP_4": (self.speed_setpoint, 4, 0)}
            if command in readings:
                value, channel, digits = readings[command]
                return f"{value:.{digits}f} {channel}"
            if command.startswith("OUT_SP_1 ") or command.startswith("OUT_SP_4 "):
                try:
                    value = float(command.split()[1])
                except (IndexError, ValueError):
                    return None
                if command.startswith("OUT_SP_1 "):
                    self.temp_setpoint = self._clamp_temp(value)
                else:
                    self.speed_setpoint = self._clamp_speed(value)
                return None
            if command == "START_1":
                self.heater = True
                if self.reset_on_start:
                    self.temp_setpoint = self.ambient  # 型号的毛病：启动加热把设定值复位了
                return None
            if command == "STOP_1":
                self.heater = False
                return None
            if command == "START_4":
                self.motor = True
                return None
            if command == "STOP_4":
                self.motor = False
                return None
            if command == "RESET":
                self.heater = self.motor = False
                return None
            for prefix in ("OUT_SP_12@", "OUT_SP_42@", "OUT_WD2@"):
                if command.startswith(prefix):
                    return self._watchdog(prefix, command[len(prefix):])
            return None  # 不认识的命令：真板子也不回

    def _watchdog(self, prefix: str, text: str) -> str | None:
        try:
            value = float(text)
        except ValueError:
            return None
        if prefix == "OUT_SP_12@":
            self.safe_temp = self._clamp_temp(value)
        elif prefix == "OUT_SP_42@":
            self.safe_speed = self._clamp_speed(value)
        elif value == 0:
            self.watchdog, self.watchdog_event = 0, False
        elif 20 <= value <= 1500:
            self.watchdog, self.fed = value, time.monotonic()
        else:
            return None
        return text

    # 测试用：看板子此刻的样子
    def snapshot(self) -> dict:
        with self.lock:
            self._advance()
            return {"heater": self.heater, "motor": self.motor, "plate": self.plate, "medium": self.medium,
                    "speed": self.speed, "temp_setpoint": self.temp_setpoint, "speed_setpoint": self.speed_setpoint,
                    "watchdog": self.watchdog, "watchdog_event": self.watchdog_event}


class NamurServer:
    """一块假板一个 TCP 端口（像串口服务器那样透明转发）。"""

    def __init__(self, plate: FakePlate, host: str = "127.0.0.1", port: int = 0):
        self.plate = plate
        owner = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                buffer = b""
                while True:
                    try:
                        chunk = self.request.recv(1024)
                    except OSError:
                        return
                    if not chunk:
                        return
                    buffer += chunk
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        text = line.decode("ascii", errors="replace").strip("\r ")
                        if not text:
                            continue
                        reply = owner.plate.handle(text)
                        if reply is not None:
                            self.request.sendall(reply.encode("ascii") + b"\r\n")

        self.server = socketserver.ThreadingTCPServer((host, port), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4001)
    parser.add_argument("--name", default="RCT digital", help="IN_NAME 回什么")
    parser.add_argument("--tau", type=float, default=30.0, help="加热盘温度的时间常数（秒）")
    args = parser.parse_args(argv)
    server = NamurServer(FakePlate(args.name, tau_s=args.tau, ramp_rpm_s=500), host=args.host, port=args.port)
    print(f"假加热板（{args.name}）在 {args.host}:{server.port}")
    try:
        server.thread.join()
    except KeyboardInterrupt:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
