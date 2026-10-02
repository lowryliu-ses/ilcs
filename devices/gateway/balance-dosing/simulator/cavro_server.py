"""假注射泵：TCP 上说 Cavro DT 协议，柱塞、分配阀、各端口储液都在 `World` 里。

    /1I3R  阀转到 3 号口      /1P1500R  吸 1500 步      /1D1500R  推 1500 步      /1ZR  初始化（柱塞回 0）
    /1Q    查状态              /1?       读柱塞位置       /1T       终止当前动作

应答 `/0<状态字节>[数据]<ETX><CR><LF>`：状态字节 0x40 | 0x20（就绪）| 错误码。动作按 `step_seconds` 一步一步「走」，
走完之前查状态是忙。推液时阀在出液口，液体按密度落到秤盘上；阀在储液口推回储液瓶；储液用完吸到的是空气。
`error` 可以手工置一个错误码（测试泵报错）。
"""
from __future__ import annotations

import re
import socketserver
import threading
import time

from .world import World

COMMAND = re.compile(r"([A-Za-z?])(\d*)")


class FakePump:
    def __init__(self, world: World, *, syringe_ul: float, steps: int, output_port: int, ports: int = 12,
                 step_seconds: float = 0.0002, initialized: bool = True):
        self.world = world
        self.syringe_ul, self.steps, self.output_port, self.ports = syringe_ul, steps, output_port, ports
        self.step_seconds = step_seconds
        self.valve = output_port
        self.plunger = 0
        self.content: tuple[str, float] | None = None  # 注射器里的液体：(物料, 密度)；None 是空气
        self.busy_until = 0.0
        self.error = 0 if initialized else 7
        self.lock = threading.RLock()

    def _ul(self, steps: int) -> float:
        return steps / self.steps * self.syringe_ul

    def handle(self, frame: str) -> str:
        if not frame.startswith("/") or len(frame) < 3:
            return self._reply(2)
        body = frame[2:]
        with self.lock:
            if body == "Q":
                return self._reply()
            if body == "?":
                return self._reply(data=str(self.plunger))
            if body == "T":
                self.busy_until = 0.0
                return self._reply()
            if not body.endswith("R"):
                return self._reply(2)
            if time.monotonic() < self.busy_until:
                return self._reply(15)  # 忙的时候又来一条动作命令
            for letter, number in COMMAND.findall(body[:-1]):
                code = self._execute(letter.upper(), int(number) if number else None)
                if code:
                    self.error = code
                    return self._reply(code)
            return self._reply()

    def _execute(self, letter: str, value: int | None) -> int:
        if letter == "Z":
            self._move_plunger(-self.plunger)
            self.error = 0
            return 0
        if self.error == 7:
            return 7
        if letter in {"I", "O"}:
            if value is None or not 1 <= value <= self.ports:
                return 3
            self.valve = value
            self.busy_until = time.monotonic() + 0.01
            return 0
        if letter == "P":
            if value is None or self.plunger + value > self.steps:
                return 3
            return self._move_plunger(value)
        if letter == "D":
            if value is None or value > self.plunger:
                return 3
            return self._move_plunger(-value)
        if letter == "V":
            return 0
        return 2

    def _move_plunger(self, delta: int) -> int:
        volume = self._ul(abs(delta))
        reservoir = self.world.reservoirs.get(self.valve)
        if delta > 0:  # 吸
            if reservoir is not None and reservoir.volume_ul >= volume:
                reservoir.volume_ul -= volume
                self.content = (reservoir.material, reservoir.density)
            elif self.valve == self.output_port:
                return 11  # 不能从出液口吸
            else:
                self.content = None  # 储液用完：吸到的是空气
        elif delta < 0 and self.content is not None:  # 推
            material, density = self.content
            if self.valve == self.output_port:
                self.world.add(volume / 1000 * density)
            elif reservoir is not None:
                reservoir.volume_ul += volume
        self.plunger += delta
        if self.plunger == 0:
            self.content = None
        self.busy_until = time.monotonic() + abs(delta) * self.step_seconds
        return 0

    def _reply(self, error: int | None = None, data: str = "") -> str:
        code = self.error if error is None else error
        ready = 0x20 if time.monotonic() >= self.busy_until else 0
        return "/0" + chr(0x40 | ready | (code & 0x0F)) + data + "\x03\r\n"


class PumpServer:
    def __init__(self, pump: FakePump, host: str = "127.0.0.1", port: int = 0):
        self.pump = pump
        owner = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                buffer = b""
                while True:
                    chunk = self.request.recv(1024)
                    if not chunk:
                        return
                    buffer += chunk
                    while b"\r" in buffer:
                        line, buffer = buffer.split(b"\r", 1)
                        frame = line.decode("ascii", errors="replace").strip()
                        if frame:
                            self.request.sendall(owner.pump.handle(frame).encode("latin-1"))

        self.server = socketserver.ThreadingTCPServer((host, port), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
