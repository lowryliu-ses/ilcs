#!/usr/bin/env python
"""假冷水机：TCP 上说三家的真协议（Huber PB 命令、Julabo、LAUDA），真实接口（driver/chillers.py）照常连它们。

物理模型（`Bath`）：控温开着，浴温按一阶惯性趋向设定值（制冷能力有下限 `floor_c`：设定值再低也到不了），
关着或报警时慢慢漂回室温；`time_scale` 把模型时间放快（测试用）。

协议上和手册一致的地方（测试靠它们测真实接口）：

- Huber：`{Mtt****` 读、`{Mttvvvv` 写，回 `{Stt vvvv`（写完之后的值：设定值超出 0x30 / 0x31 的范围会被限幅）；
  没开放的地址回 `7FFF`；格式不对一句都不回；状态字第 14 位在「重启」后第一次读时是 0（`restart()` 模拟断电重启：
  远程写的设定值回到面板上的值、控温关掉）；
- Julabo：`in` 命令回一行，`out` 命令不回；面板控制模式（`remote = False`）时 `out` 命令悄悄忽略；
  值超范围不回话、下一次 `status` 报 `-10 VALUE TOO SMALL` / `-11 VALUE TOO LARGE`；命令不认识报 `-08 INVALID COMMAND`；
- LAUDA：写命令回 `OK` / `ERR_x`（`ERR_6` 值不允许、`ERR_5` 数值语法错、`ERR_3` 命令不认识）；`_` 也可以写成空格；
  读数是定点格式（`025.30`、`-10.03`）。

测试用的开关：`mute`（一句都不回：线断了）、`log`（收到的每条命令）、报警（`alarm`）。

    # 现场没有冷水机时起一台假的，再让网关（不带 --simulate）用 {"kind": "tcp", "host": "127.0.0.1", "port": 8101} 连它
    python simulator/chillers.py --kind huber --port 8101
"""
from __future__ import annotations

import argparse
import math
import socketserver
import threading
import time
from typing import Callable


class Bath:
    """冷浴的一阶模型。"""

    def __init__(self, *, ambient_c: float = 22.0, tau_s: float = 60.0, drift_tau_s: float | None = None,
                 time_scale: float = 1.0, floor_c: float = -80.0, setpoint_c: float | None = None):
        self.ambient = float(ambient_c)
        self.tau = float(tau_s)
        self.drift_tau = float(drift_tau_s or 4 * tau_s)
        self.time_scale = float(time_scale)
        self.floor = float(floor_c)      # 制冷能力的下限：设定值低于它也只能冷到它
        self.temp = self.ambient
        self.setpoint = float(self.ambient if setpoint_c is None else setpoint_c)
        self.running = False
        self.alarm = False
        self.lock = threading.RLock()
        self.since = time.monotonic()

    def advance(self) -> None:
        with self.lock:
            now = time.monotonic()
            dt, self.since = (now - self.since) * self.time_scale, now
            if self.running and not self.alarm:
                target, tau = max(self.setpoint, self.floor), self.tau
            else:
                target, tau = self.ambient, self.drift_tau
            self.temp += (target - self.temp) * (1 - math.exp(-dt / max(tau, 1e-6)))

    def snapshot(self) -> dict:
        with self.lock:
            self.advance()
            return {"temp": self.temp, "setpoint": self.setpoint, "running": self.running, "alarm": self.alarm}


class FakeChiller:
    """三家假冷水机的共同部分：一行命令 → 应答（None = 不回）。"""

    def __init__(self, bath: Bath, *, min_setpoint: float = -40.0, max_setpoint: float = 100.0):
        self.bath = bath
        self.min_setpoint, self.max_setpoint = float(min_setpoint), float(max_setpoint)
        self.mute = False
        self.log: list[str] = []
        self.lock = threading.RLock()

    def handle(self, line: str) -> str | None:
        with self.lock, self.bath.lock:
            if self.mute:
                return None
            line = line.strip()
            self.log.append(line)
            self.bath.advance()
            return self.answer(line)

    def answer(self, line: str) -> str | None:
        raise NotImplementedError

    def set_alarm(self, on: bool = True) -> None:
        with self.bath.lock:
            self.bath.advance()
            self.bath.alarm = on


# ---------- Huber：PB 命令 ----------

def _encode(celsius: float) -> int:
    return int(round(celsius * 100)) & 0xFFFF


def _decode(raw: int) -> float:
    value = raw - 0x10000 if raw & 0x8000 else raw
    return (raw if value < -15111 else value) / 100


class FakeHuber(FakeChiller):
    def __init__(self, bath: Bath, *, serial: int = 23_456_789, process_sensor: bool = False, **limits):
        super().__init__(bath, **limits)
        self.serial = serial
        self.process_sensor = process_sensor
        self.error = 0          # 错误号（负数）；0 = 没有
        self.warning = 0
        self.fresh = True       # 「重启」后还没读过状态字
        self.panel_setpoint = bath.setpoint

    def set_alarm(self, on: bool = True) -> None:
        super().set_alarm(on)
        self.error = -1331 if on else 0  # 随便一个错误号：真机的号码表见 Huber 手册

    def restart(self) -> None:
        """模拟断电重启：数据命令改的设定值回到面板上的值，控温关掉，状态字第 14 位下一次读是 0。"""
        with self.lock, self.bath.lock:
            self.bath.advance()
            self.bath.setpoint, self.bath.running, self.fresh = self.panel_setpoint, False, True

    def answer(self, line: str) -> str | None:
        if len(line) != 8 or not line.startswith("{M"):
            return None  # 格式不对：一句都不回
        try:
            address = int(line[2:4], 16)
            value = None if line[4:8] == "****" else int(line[4:8], 16)
        except ValueError:
            return None
        return f"{{S{address:02X}{self._variable(address, value) & 0xFFFF:04X}"

    def _variable(self, address: int, value: int | None) -> int:
        bath = self.bath
        if address == 0x00:
            if value is not None:
                bath.setpoint = min(self.max_setpoint, max(self.min_setpoint, _decode(value)))
            return _encode(bath.setpoint)
        if address == 0x01:
            return _encode(bath.temp)
        if address == 0x05:
            if value == 1:
                self.error = 0
            return self.error
        if address == 0x06:
            if value == 1:
                self.warning = 0
            return self.warning
        if address == 0x07:
            return _encode(bath.temp) if self.process_sensor else _encode(-151.0)
        if address == 0x0A:
            bits = (bath.running << 0) | (bath.running << 2) | (bath.running << 4) | (1 << 5) | (1 << 7)
            bits |= (bool(self.error) << 8) | (bool(self.warning) << 9) | ((not self.fresh) << 14)
            self.fresh = False
            return bits
        if address == 0x14:
            if value in (0, 1):
                bath.running = bool(value) and not self.error  # 有错误时开不起来
            return int(bath.running)
        if address == 0x16:
            return int(bath.running)
        if address == 0x1B:
            return self.serial & 0xFFFF
        if address == 0x1C:
            return (self.serial >> 16) & 0xFFFF
        if address in (0x30, 0x31):
            if value is not None:
                limit = _decode(value)
                self.min_setpoint, self.max_setpoint = (limit, self.max_setpoint) if address == 0x30 \
                    else (self.min_setpoint, limit)
            return _encode(self.min_setpoint if address == 0x30 else self.max_setpoint)
        if address == 0x26:
            return 2900 if bath.running else 0
        return 0x7FFF  # 没有这个变量 / 没开放


# ---------- Julabo ----------

class FakeJulabo(FakeChiller):
    STATES = {0: "MANUAL STOP", 1: "MANUAL START", 2: "REMOTE STOP", 3: "REMOTE START"}

    def __init__(self, bath: Bath, *, version: str = "JULABO CF41 VERSION 1.30", remote: bool = True, **limits):
        super().__init__(bath, **limits)
        self.version = version
        self.remote = remote
        self.message = ""   # 对上一条命令的意见，下一次 status 报一次

    def answer(self, line: str) -> str | None:
        parts = line.strip().split()
        if not parts:
            return None
        command, argument = parts[0].lower(), (parts[1] if len(parts) > 1 else "")
        bath = self.bath
        if command == "version":
            return self.version
        if command == "status":
            return self._status()
        if command == "in_pv_00":
            return f"{bath.temp:.2f}"
        if command == "in_sp_00":
            return f"{bath.setpoint:.2f}"
        if command == "in_mode_05":
            return "1" if bath.running else "0"
        if command in {"out_sp_00", "out_mode_05"}:
            if not self.remote:
                return None  # 面板控制模式：out 命令悄悄忽略
            if command == "out_mode_05":
                if argument in {"0", "1"}:
                    bath.running = argument == "1"
                else:
                    self.message = "-08 INVALID COMMAND"
                return None
            try:
                value = float(argument)
            except ValueError:
                self.message = "-08 INVALID COMMAND"
                return None
            if value < self.min_setpoint:
                self.message = "-10 VALUE TOO SMALL"
            elif value > self.max_setpoint:
                self.message = "-11 VALUE TOO LARGE"
            else:
                bath.setpoint = round(value, 2)
            return None
        self.message = "-08 INVALID COMMAND"
        return None

    def _status(self) -> str:
        if self.bath.alarm:
            return "-01 LOW LEVEL ALARM"
        if self.message:
            message, self.message = self.message, ""
            return message
        code = (2 if self.remote else 0) + (1 if self.bath.running else 0)
        return f"{code:02d} {self.STATES[code]}"


# ---------- LAUDA ----------

class FakeLauda(FakeChiller):
    def __init__(self, bath: Bath, *, model: str = "PRO RP 1090 C", version: str = "V2.30", **limits):
        super().__init__(bath, **limits)
        self.model, self.version = model, version
        self.changes = 0   # 设定值改了几次（WK / WKL 一小时只许 20 次）

    @staticmethod
    def fixed(value: float) -> str:
        return f"{value:06.2f}"  # 定点格式：025.30、-10.03

    def answer(self, line: str) -> str | None:
        text = line.strip().upper().replace(" ", "_")
        bath = self.bath
        replies = {"TYPE": self.model, "VERSION_R": self.version, "VERSION": self.version,
                   "STATUS": "-1" if bath.alarm else "0", "STAT": "1000000" if bath.alarm else "0000000",
                   "IN_PV_00": self.fixed(bath.temp), "IN_SP_00": self.fixed(bath.setpoint),
                   "IN_MODE_02": "0" if bath.running else "1"}
        if text in replies:
            return replies[text]
        if text.startswith("OUT_SP_00_"):
            try:
                value = float(text[len("OUT_SP_00_"):])
            except ValueError:
                return "ERR_5"
            if not self.min_setpoint <= value <= self.max_setpoint:
                return "ERR_6"
            bath.setpoint = round(value, 2)
            self.changes += 1
            return "OK"
        if text == "START":
            bath.running = True
            return "OK"
        if text == "STOP":
            bath.running = False
            return "OK"
        return "ERR_3"


FAKES: dict[str, type[FakeChiller]] = {"huber": FakeHuber, "julabo": FakeJulabo, "lauda": FakeLauda}


class LineServer:
    """一台假设备一个 TCP 端口（像串口服务器那样透明转发）：CR 或 LF 都算一行结束，应答以 CR LF 结尾。"""

    def __init__(self, handler: Callable[[str], str | None], host: str = "127.0.0.1", port: int = 0):
        owner = self
        self.handler = handler

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
                    buffer += chunk.replace(b"\r", b"\n")
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        text = line.decode("ascii", errors="replace")
                        if not text.strip():
                            continue
                        reply = owner.handler(text)
                        if reply is not None:
                            try:
                                self.request.sendall(reply.encode("ascii") + b"\r\n")
                            except OSError:
                                return

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
    parser.add_argument("--kind", choices=sorted(FAKES), default="huber")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8101)
    parser.add_argument("--ambient", type=float, default=22.0, help="室温 ℃")
    parser.add_argument("--tau", type=float, default=120.0, help="浴温的时间常数（秒）")
    args = parser.parse_args(argv)
    fake = FAKES[args.kind](Bath(ambient_c=args.ambient, tau_s=args.tau))
    server = LineServer(fake.handle, host=args.host, port=args.port)
    print(f"假冷水机（{args.kind}）在 {args.host}:{server.port}")
    try:
        server.thread.join()
    except KeyboardInterrupt:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
