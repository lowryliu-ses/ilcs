"""假电芯检测仪表的 TCP 口与统一控制口；ILCS 侧照常用 `line_command_v1` 连它，和接真仪表走同一条路。

- Keithley 2450：LAN 原始套接字（真表 5025），结束符 LF；
- Keithley 2400：真表只有 RS-232（与 GPIB），现场经串口服务器转 TCP；模拟仪表直接开一个 TCP 口，ILCS 用
  `socket://主机:端口` 当串口连，结束符 CR；
- Hioki BT3562 系列：LAN（BT356xA，缺省命令端口 23）或 RS-232C，结束符 CR+LF。

收到 CR 或 LF 都算一行结束（CR+LF 中间的空行忽略），回复按型号的结束符。设了 `SIM_CONTROL_PORT` 就另开统一控制口
（`devices/simulators/common/control.py`）：故障注入与测量计数，接入验收的故障项目走它。仪表不认 ILCS 指令号，
控制口只报总测量次数。

    python devices/gateway/scpi-cell-meter/simulator/server.py --model keithley-2450 --port 5025
    python devices/gateway/scpi-cell-meter/simulator/server.py --model keithley-2400 --port 4001
    SIM_CONTROL_PORT=9900 python devices/gateway/scpi-cell-meter/simulator/server.py --model hioki-bt3562 --port 2323
"""
from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
import re
import signal
import socket
import socketserver
import sys
import threading
import time
from typing import Any

MODULE = Path(__file__).resolve().parents[1]
DEVICES = MODULE.parents[1]  # devices/：simulators 包（统一控制口）所在的目录
for _path in (str(DEVICES), str(MODULE)):
    if _path not in sys.path:  # 直接运行（容器、本机联调）时也能找到这两个包
        sys.path.insert(0, _path)

from simulator.hioki import HiokiBT3562  # noqa: E402
from simulator.keithley import Keithley2400, Keithley2450  # noqa: E402
from simulator.scpi import CLOSE, Cell, ScpiMeter  # noqa: E402

log = logging.getLogger("ilcs.scpi-cell-meter")
LINE_END = re.compile(rb"[\r\n]")
MODELS = {"keithley-2450": Keithley2450, "keithley-2400": Keithley2400, "hioki-bt3562": HiokiBT3562}
# 真仪表的缺省端口（2400 是串口服务器的常用端口）；本机不是 root 时 Hioki 换一个大于 1024 的端口
DEFAULT_PORTS = {"keithley-2450": 5025, "keithley-2400": 4001, "hioki-bt3562": 23}


def build_meter(model: str, *, ocv_V: float = 3.85, ir_mohm: float = 15.0, serial: str = "", **options: Any) -> ScpiMeter:
    if model not in MODELS:
        raise ValueError(f"不认识的模拟仪表 {model}；可选 {', '.join(MODELS)}")
    return MODELS[model](Cell(ocv_V=ocv_V, ir_ohm=ir_mohm / 1000), serial, **options)


class MeterServer:
    """一台假仪表的 TCP 口：每个连接一个线程，一行命令交给仪表、回一行（或不回、断开）。

    `go_offline(N)` 关掉监听并断开已有连接，N 秒后在同一个端口重新监听（模拟失联）。端口给 0 时由系统挑，
    `start()` 之后看 `port`。
    """

    def __init__(self, meter: ScpiMeter, address: str = "127.0.0.1", port: int = 0):
        self.meter = meter
        self.address = address
        self.port = port
        self.server: socketserver.ThreadingTCPServer | None = None
        self.clients: set[socket.socket] = set()
        self.lock = threading.Lock()
        self.offline_until = 0.0

    def _handler(self):
        outer = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                sock: socket.socket = self.request
                with outer.lock:
                    outer.clients.add(sock)
                try:
                    buffer = b""
                    while True:
                        try:
                            chunk = sock.recv(4096)
                        except OSError:
                            return
                        if not chunk:
                            return
                        buffer += chunk
                        while (found := LINE_END.search(buffer)) is not None:
                            raw, buffer = buffer[:found.start()], buffer[found.end():]
                            if not raw.strip():
                                continue
                            try:
                                reply = outer.meter.handle(raw.decode("latin-1"))
                            except Exception:  # 仪表模型出错当作没回复：驱动只能判结果未知
                                log.exception("处理命令 %r 失败", raw)
                                reply = None
                            if reply is CLOSE:
                                return
                            if reply is not None:
                                try:
                                    sock.sendall(reply.encode("latin-1", errors="replace")
                                                 + outer.meter.newline.encode())
                                except OSError:
                                    return
                finally:
                    with outer.lock:
                        outer.clients.discard(sock)
                    try:
                        sock.close()
                    except OSError:
                        pass

        return Handler

    def start(self) -> "MeterServer":
        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        server = Server((self.address, self.port), self._handler())
        self.port = server.server_address[1]
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.server = server
        log.info("模拟仪表 %s 已监听 %s:%s", self.meter.MODEL, self.address, self.port)
        return self

    def stop(self) -> None:
        server, self.server = self.server, None
        if server is not None:
            server.shutdown()
            server.server_close()
        with self.lock:
            clients, self.clients = list(self.clients), set()
        for sock in clients:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()

    def go_offline(self, seconds: float) -> None:
        self.offline_until = time.monotonic() + seconds

        def cycle():
            time.sleep(0.2)  # 先让控制口的应答送回去
            self.stop()
            log.info("模拟失联 %.1f s", seconds)
            time.sleep(max(0.0, self.offline_until - time.monotonic()))
            for attempt in range(20):  # 端口还没完全放开时隔一会儿再试，不能就此停在离线
                try:
                    self.start()
                    return
                except OSError:
                    time.sleep(0.25)
            log.error("模拟仪表恢复监听失败：请重启它")

        threading.Thread(target=cycle, daemon=True).start()


class MeterControl:
    """统一控制口的目标（接口同 `devices/simulators/common/control.py` 的 `DeviceTarget`）。"""

    knows_command_ids = False  # 仪表不认 ILCS 指令号：验收按总测量次数判「重投有没有再测一次」

    def __init__(self, meter: ScpiMeter, server: MeterServer):
        self.meter = meter
        self.server = server

    def set_fault(self, mode: str, parameter: float, unit: str = "") -> dict:
        if mode == "offline":
            seconds = parameter or 5
            self.server.go_offline(seconds)
            return {**self.state(unit), "fault": "offline", "seconds": seconds}
        self.meter.set_fault(mode, parameter)
        return self.state(unit)

    def state(self, unit: str = "") -> dict:
        state = self.meter.state()
        return {
            "device_id": self.meter.serial, "model": self.meter.MODEL, "fault": state["fault"],
            "fault_parameter": state["fault_parameter"], "motions": state["motions"], "knows_command_ids": False,
            "tasks": {}, "unsupported": dict(self.meter.unsupported), "meter": state,
        }


def parse(argv=None) -> argparse.Namespace:
    env = os.environ.get
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", default=env("SIM_MODEL", "keithley-2450"), choices=sorted(MODELS))
    parser.add_argument("--address", default=env("SIM_ADDRESS", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(env("SIM_PORT", "0")) or None,
                        help="缺省按真仪表：2450 用 5025、2400（串口服务器）4001、Hioki 23")
    parser.add_argument("--serial", default=env("SIM_SERIAL", ""), help="自报序列号，缺省 ILCS-SIMULATOR-…")
    parser.add_argument("--ocv", type=float, default=float(env("SIM_OCV_V", "3.85")), help="电芯开路电压（V）")
    parser.add_argument("--ir", type=float, default=float(env("SIM_IR_MOHM", "15")), help="电芯交流内阻（mΩ）")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    from simulators.common.control import start_control

    logging.basicConfig(level=os.environ.get("SIM_LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    args = parse(argv)
    meter = build_meter(args.model, ocv_V=args.ocv, ir_mohm=args.ir, serial=args.serial)
    server = MeterServer(meter, args.address, args.port or DEFAULT_PORTS[args.model]).start()
    control = start_control(MeterControl(meter, server))
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    while not stop.wait(0.5):
        pass
    server.stop()
    if control is not None:
        control.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
