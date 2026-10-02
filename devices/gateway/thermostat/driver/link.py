"""文本链路：TCP（串口服务器、设备自己的网口），或串口（本机串口、rfc2217:// / socket:// 串口服务器，经 pyserial）。

照 ika-stirrer 的链路改的（同一仓库，模块各自带一份，不跨模块导入）。几种协议都是「一行命令、一行应答」，
区别只在细节，由链路参数给（缺省值由 `config.py` 按厂家填好）：

- 写命令有没有应答：Huber PB 命令、LAUDA 命令都回；Julabo 的 `out_*`、IKA 的 `OUT_*` / `START_*` / `STOP_*` 不回。
  所以有两个方法：`ask` 发一行、读一行；`send` 只发不读；
- 行尾：`eol`（Huber、LAUDA、IKA 是 CR LF，Julabo 是 CR）；应答都以 LF 结尾（前面的 CR 去掉）；
- 两条命令之间至少隔多久：`gap_sec`（Julabo 手册要 250 ms）；
- 串口参数：`baudrate`、`bytesize`、`parity`、`stopbits`、`rtscts`（Julabo 是硬件握手）。

和 ika-stirrer 一样：一条链路一把锁（计时线程、健康检查可能同时要用）；任何通信异常都丢掉这条连接、下次重连；
发之前先清掉接收缓冲里的旧字节（上一条超时的应答晚到了，不能当成这一条的）；`sent` 区分「肯定没写出去」和「写出去了」。
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
        self.write_terminator = str(spec.get("eol") or "\r\n").encode(encoding)
        self.read_terminator = read_terminator
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

                conn = serial.serial_for_url(
                    self.spec["port"], baudrate=int(self.spec.get("baudrate") or 9600),
                    bytesize=int(self.spec.get("bytesize") or 8), parity=str(self.spec.get("parity") or "N"),
                    stopbits=float(self.spec.get("stopbits") or 1), timeout=self.timeout,
                    xonxoff=False, rtscts=bool(self.spec.get("rtscts")), dsrdtr=False,
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
        """丢掉接收缓冲里的旧字节（这些设备都不主动发数据，缓冲里有东西只能是晚到的应答）。对端已关连接返回 False。"""
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
                # 对端已经关了这条连接：写进去也到不了设备，重连再写
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
        self._last = time.monotonic()
        return line.decode(self.encoding, errors="replace").strip("\r\n\x03 ")

    def ask(self, command: str) -> str:
        """发一行、读一行应答。"""
        with self.lock:
            self._write(command)
            return self._read_line(command)

    def send(self, command: str) -> None:
        """只发不读（设备对这条命令不回）。写不出去抛 `LinkError(sent=False)`；写出去了不代表设备收到了。"""
        with self.lock:
            self._write(command)
