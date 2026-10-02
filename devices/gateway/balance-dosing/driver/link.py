"""一问一答的文本链路：TCP，或串口（本机串口、rfc2217:// / socket:// 串口服务器，经 pyserial）。

天平（MT-SICS）和注射泵（Cavro DT 协议）都是「发一行、回一行」。这里只管收发：

- 一条链路一把锁：加料的后台线程和状态查询可能同时要用同一台设备，收发不能交错；
- 任何通信异常都丢掉这条连接、下次重连，不在半截应答上接着读（`LinkError`）；
- `ask` 之前出的错（连不上）和之后出的错（发了没回）分开：`sent` 记最近一次命令有没有写出去，
  上层据此区分「设备肯定没收到」和「不知道设备收没收到」。
"""
from __future__ import annotations

import socket
import threading
import time
from typing import Any


class LinkError(OSError):
    """链路断了、超时或对端关了连接。`sent` 为 True 时命令已经写出去，设备可能收到了。"""

    def __init__(self, message: str, *, sent: bool):
        super().__init__(message)
        self.sent = sent


class Link:
    def __init__(self, spec: dict[str, Any], *, timeout: float = 5.0, read_terminator: bytes = b"\n",
                 write_terminator: bytes = b"\r\n", encoding: str = "ascii"):
        kind = str(spec.get("kind") or "tcp")
        if kind not in {"tcp", "serial"}:
            raise ValueError(f"链路 kind 只能是 tcp 或 serial，不是 {kind!r}")
        if kind == "tcp" and not (spec.get("host") and spec.get("port")):
            raise ValueError("TCP 链路要写 host 与 port")
        if kind == "serial" and not spec.get("port"):
            raise ValueError("串口链路要写 port（/dev/ttyUSB0、COM3、rfc2217://主机:端口）")
        self.spec = dict(spec)
        self.kind = kind
        self.timeout = float(spec.get("timeout_sec") or timeout)
        self.read_terminator = read_terminator
        self.write_terminator = write_terminator
        self.encoding = encoding
        self.lock = threading.RLock()
        self._conn: Any = None
        self._buffer = b""

    def describe(self) -> str:
        return f"{self.spec['host']}:{self.spec['port']}" if self.kind == "tcp" else str(self.spec["port"])

    # ---------- 连接 ----------

    def _open(self) -> Any:
        if self._conn is not None:
            return self._conn
        try:
            if self.kind == "tcp":
                conn = socket.create_connection((self.spec["host"], int(self.spec["port"])), timeout=self.timeout)
                conn.settimeout(self.timeout)
            else:
                import serial  # pyserial：只有串口设备才要装

                conn = serial.serial_for_url(
                    self.spec["port"], baudrate=int(self.spec.get("baudrate") or 9600),
                    bytesize=int(self.spec.get("bytesize") or 8), parity=str(self.spec.get("parity") or "N"),
                    stopbits=float(self.spec.get("stopbits") or 1), timeout=self.timeout,
                )
        except (OSError, ValueError) as exc:
            raise LinkError(f"连不上 {self.describe()}：{exc}", sent=False) from exc
        self._conn, self._buffer = conn, b""
        return conn

    def close(self) -> None:
        with self.lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except OSError:
                    pass
            self._conn, self._buffer = None, b""

    # ---------- 收发 ----------

    def _write(self, data: bytes) -> None:
        conn = self._open()
        if self.kind == "tcp":
            conn.sendall(data)
        else:
            conn.write(data)
            conn.flush()

    def _read_chunk(self, wait: float | None = None) -> bytes:
        """读一块。`wait` 是这一次最多等多久：短轮询时不能让一次 recv 按整条链路的超时卡住、一直占着锁。"""
        conn = self._conn
        if conn is None:
            raise OSError("连接已关闭")
        wait = self.timeout if wait is None else max(0.01, min(wait, self.timeout))
        if self.kind == "tcp":
            conn.settimeout(wait)
            return conn.recv(1024)
        conn.timeout = wait
        return conn.read(conn.in_waiting or 1)

    def read_line(self, timeout: float | None = None) -> str:
        """读一行（不含行尾）。超时、断线抛 `LinkError`（sent=True：这时总是在等一条已经发出的命令的应答）。"""
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        while self.read_terminator not in self._buffer:
            if time.monotonic() > deadline:
                self.close()
                raise LinkError(f"{self.describe()} 应答超时", sent=True)
            try:
                chunk = self._read_chunk(deadline - time.monotonic())
            except socket.timeout:
                continue
            except OSError as exc:
                self.close()
                raise LinkError(f"{self.describe()} 读应答出错：{exc}", sent=True) from exc
            if not chunk:
                if self.kind == "tcp":
                    self.close()
                    raise LinkError(f"{self.describe()} 关闭了连接，应答没收全", sent=True)
                continue  # 串口：这一轮没读到字节
            self._buffer += chunk
        line, _, self._buffer = self._buffer.partition(self.read_terminator)
        return line.decode(self.encoding, errors="replace").strip("\r\n\x03 ")

    def try_read_line(self, timeout: float) -> str | None:
        """等一行，`timeout` 内没有完整的一行就返回 None（连接保留）。给「先回已收下、做完再回结论」的命令用。"""
        deadline = time.monotonic() + timeout
        while self.read_terminator not in self._buffer:
            if time.monotonic() > deadline:
                return None
            try:
                chunk = self._read_chunk(deadline - time.monotonic())
            except socket.timeout:
                continue
            except OSError as exc:
                self.close()
                raise LinkError(f"{self.describe()} 读应答出错：{exc}", sent=True) from exc
            if not chunk:
                if self.kind == "tcp":
                    self.close()
                    raise LinkError(f"{self.describe()} 关闭了连接", sent=True)
                continue
            self._buffer += chunk
        line, _, self._buffer = self._buffer.partition(self.read_terminator)
        return line.decode(self.encoding, errors="replace").strip("\r\n\x03 ")

    def write(self, command: str) -> None:
        """只发不收（应答由调用方另读）；不清缓冲区——前一条命令的异步结论可能已经在里面了。"""
        with self.lock:
            try:
                self._write(command.encode(self.encoding) + self.write_terminator)
            except LinkError:
                raise
            except OSError as exc:
                self.close()
                raise LinkError(f"{self.describe()} 写命令出错：{exc}", sent=False) from exc

    def ask(self, command: str, *, timeout: float | None = None) -> str:
        """发一行命令、读一行应答。"""
        with self.lock:
            self.sent = False
            self._buffer = b""
            try:
                self._write(command.encode(self.encoding) + self.write_terminator)
            except LinkError:
                raise
            except OSError as exc:
                self.close()
                raise LinkError(f"{self.describe()} 写命令出错：{exc}", sent=False) from exc
            self.sent = True
            return self.read_line(timeout)

    sent = False
