"""文本链路：TCP（串口服务器），或串口（本机串口、rfc2217:// / socket:// 串口服务器，经 pyserial）。

照 balance-dosing 的链路改的。IKA 的 NAMUR 命令分两种：读命令（`IN_*`）回一行，写 / 动作命令（`OUT_*`、`START_*`、
`STOP_*`）**什么都不回**。所以这里有两个方法：`ask` 发一行、读一行；`send` 只发不读。

- 一条链路一把锁：计时线程（到点停、采样、喂看门狗）和健康检查可能同时要用同一块板，收发不能交错；
- 任何通信异常都丢掉这条连接、下次重连，不在半截应答上接着读（`LinkError`）；
- 发之前先清掉接收缓冲里的旧字节：上一条超时的应答晚到了，不能当成这一条的应答；
- `sent` 区分「肯定没写出去」和「写出去了」。写命令没有应答，写出去了也不知道设备收没收到——
  上层要确认，就紧跟一条读命令（同一条线、按顺序，读得到应答说明前面的写也到了）。
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
    def __init__(self, spec: dict[str, Any], *, timeout: float = 2.0, read_terminator: bytes = b"\n",
                 encoding: str = "ascii"):
        kind = str(spec.get("kind") or "tcp")
        if kind not in {"tcp", "serial"}:
            raise ValueError(f"链路 kind 只能是 tcp 或 serial，不是 {kind!r}")
        if kind == "tcp" and not (spec.get("host") and spec.get("port")):
            raise ValueError("TCP 链路要写 host 与 port")
        if kind == "serial" and not spec.get("port"):
            raise ValueError("串口链路要写 port（/dev/ttyUSB0、COM5、rfc2217://主机:端口）")
        self.spec = dict(spec)
        self.kind = kind
        self.timeout = float(spec.get("timeout_sec") or timeout)
        # NAMUR：命令以 CR LF 结尾（有的手册写「空格 CR LF」，eol 可以改）
        self.write_terminator = str(spec.get("eol") or "\r\n").encode(encoding)
        self.read_terminator = read_terminator
        # 两条命令之间至少隔多久：设备处理一条命令要时间，连着发太快会丢
        self.gap = float(spec.get("gap_sec") if spec.get("gap_sec") is not None else 0.05)
        self.encoding = encoding
        self.lock = threading.RLock()
        self._conn: Any = None
        self._buffer = b""
        self._last = 0.0
        self.sent = False

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

                # IKA NAMUR 缺省 9600 波特、7 数据位、偶校验、1 停止位，无流控
                conn = serial.serial_for_url(
                    self.spec["port"], baudrate=int(self.spec.get("baudrate") or 9600),
                    bytesize=int(self.spec.get("bytesize") or 7), parity=str(self.spec.get("parity") or "E"),
                    stopbits=float(self.spec.get("stopbits") or 1), timeout=self.timeout,
                    xonxoff=False, rtscts=False, dsrdtr=False,
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

    def _drain(self, conn: Any) -> bool:
        """丢掉接收缓冲里的旧字节（设备从不主动发数据，缓冲里有东西只能是晚到的应答）。对端已关连接返回 False。"""
        self._buffer = b""
        if self.kind == "serial":
            conn.reset_input_buffer()
            return True
        conn.setblocking(False)
        try:
            while True:
                if not conn.recv(1024):
                    return False
        except (BlockingIOError, InterruptedError):
            return True
        finally:
            conn.settimeout(self.timeout)

    def _write(self, command: str) -> None:
        wait = self._last + self.gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self.sent = False
        try:
            conn = self._open()
            if not self._drain(conn):
                # 串口服务器已经关了这条连接：写进去也到不了设备，重连再写
                self.close()
                conn = self._open()
            data = command.encode(self.encoding) + self.write_terminator
            if self.kind == "tcp":
                conn.sendall(data)
            else:
                conn.write(data)
                conn.flush()
        except LinkError:
            raise
        except OSError as exc:
            self.close()
            raise LinkError(f"{self.describe()} 写命令 {command} 出错：{exc}", sent=False) from exc
        finally:
            self._last = time.monotonic()
        self.sent = True

    def _read_chunk(self) -> bytes:
        conn = self._conn
        if self.kind == "tcp":
            return conn.recv(1024)
        return conn.read(conn.in_waiting or 1)

    def _read_line(self, command: str) -> str:
        """读一行（不含行尾）。超时、断线抛 `LinkError`（sent=True：这时总是在等一条已经发出的命令的应答）。"""
        deadline = time.monotonic() + self.timeout
        while self.read_terminator not in self._buffer:
            if time.monotonic() > deadline:
                self.close()
                raise LinkError(f"{self.describe()} 对 {command} 没有应答（{self.timeout:g} 秒）", sent=True)
            try:
                chunk = self._read_chunk()
            except socket.timeout:
                continue
            except OSError as exc:
                self.close()
                raise LinkError(f"{self.describe()} 读 {command} 的应答出错：{exc}", sent=True) from exc
            if not chunk:
                if self.kind == "tcp":
                    self.close()
                    raise LinkError(f"{self.describe()} 关闭了连接，{command} 的应答没收全", sent=True)
                continue  # 串口：这一轮没读到字节
            self._buffer += chunk
        line, _, self._buffer = self._buffer.partition(self.read_terminator)
        return line.decode(self.encoding, errors="replace").strip("\r\n\x03 ")

    def ask(self, command: str) -> str:
        """读命令：发一行、读一行应答。"""
        with self.lock:
            self._write(command)
            return self._read_line(command)

    def send(self, command: str) -> None:
        """写 / 动作命令：只发不读（设备不回）。写不出去抛 `LinkError(sent=False)`；写出去了不代表设备收到了。"""
        with self.lock:
            self._write(command)
