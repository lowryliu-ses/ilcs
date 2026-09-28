"""一问一答文本协议的 TCP 服务器（串口仪器经串口服务器转 TCP 就是这种形态）。

每个连接一个线程，按行读命令、交给方言对象处理、写回一行。方言返回 None 表示「不回复」
（模拟回执丢失）。离线故障会关掉监听并断开所有已有连接，N 秒后重新监听。
"""
from __future__ import annotations

import logging
import socket
import socketserver
import threading
import time

log = logging.getLogger("ilcs.line-sim")


class LineServer:
    def __init__(self, address: str, port: int, dialect, *, newline: bytes = b"\r\n", greeting: str = ""):
        self.address = address
        self.port = port
        self.dialect = dialect
        self.newline = newline
        self.greeting = greeting
        self.server: socketserver.ThreadingTCPServer | None = None
        self.clients: set[socket.socket] = set()
        self.lock = threading.Lock()

    def _handler(self):
        outer = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                sock: socket.socket = self.request
                with outer.lock:
                    outer.clients.add(sock)
                try:
                    if outer.greeting:
                        sock.sendall(outer.greeting.encode() + outer.newline)
                    buffer = b""
                    while True:
                        try:
                            chunk = sock.recv(4096)
                        except OSError:
                            return
                        if not chunk:
                            return
                        buffer += chunk
                        while b"\n" in buffer:
                            raw, _, buffer = buffer.partition(b"\n")
                            line = raw.rstrip(b"\r").decode("latin-1")
                            try:
                                reply = outer.dialect.handle(line)
                            except Exception:  # 方言出错当作设备没回复：客户端只能判结果未知
                                log.exception("处理命令 %r 失败", line)
                                reply = None
                            if reply is _CLOSE:
                                return
                            closing = isinstance(reply, tuple)  # (最后一句回复, CLOSE)：回完就断开
                            if closing:
                                reply = reply[0]
                            if reply is not None:
                                try:
                                    sock.sendall(reply.encode("latin-1", errors="replace") + outer.newline)
                                except OSError:
                                    return
                            if closing:
                                return
                finally:
                    with outer.lock:
                        outer.clients.discard(sock)

        return Handler

    def start(self) -> None:
        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        server = Server((self.address, self.port), self._handler())
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
        self.server = server
        log.info("文本协议模拟设备已监听 %s:%s", self.address, self.port)

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
        def cycle():
            time.sleep(0.2)  # 先把故障命令的应答送回去
            self.stop()
            log.info("模拟离线 %.0f s", seconds)
            time.sleep(seconds)
            self.start()

        threading.Thread(target=cycle, daemon=True).start()


class _Close:
    """方言返回它表示「回完这一句就断开」（UR 仪表盘的 quit）。"""


_CLOSE = _Close()
CLOSE = _CLOSE
