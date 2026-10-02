"""文本链路：TCP（串口服务器、假仪器），或串口（USB 虚拟串口、本机串口、rfc2217:// / socket://，经 pyserial）。

照 balance-dosing 的链路改的。MethodSCRIPT 仪器和天平不一样的地方：

- 一条命令之后仪器可能一直往回发（测量数据一行一行流回来，直到一个空行）：读线程长时间占着读端，
  终止（`Z`）要从另一个线程写进去——读、写各一把锁，写不用等读；
- 行尾只有 `\\n`（仪器不发 `\\r`，收到的 `\\r` 也忽略）；**空行有意义**（脚本结束），不能当噪声跳过；
  空格也有意义（数据包里值为 0 时前缀是空格），只去掉 `\\r`；
- EmStat Pico 用 XON/XOFF 软件流控，上电时可能发一个 XON：收到的 XON / XOFF 字符一律丢掉；
- `try_read_line` 等不到一整行就返回 None、连接保留（读线程隔一会儿看一眼要不要终止）；
  `read_line` 等不到就当链路坏了（关掉连接、下次重连）；
- 发命令之前 `drain` 掉晚到的旧字节（上一条命令出错后仪器可能还补一个空行、终止撞上脚本结束会多一行 `Z!0006`）；
- `sent` 区分「肯定没写出去」和「写出去了」：上层据此区分「仪器肯定没收到」和「不知道仪器收没收到」。
"""
from __future__ import annotations

import socket
import threading
import time
from typing import Any

FLOW_CONTROL = bytes([0x11, 0x13])  # XON、XOFF


class LinkError(OSError):
    """链路断了、超时或对端关了连接。`sent` 为 True 时命令已经写出去，仪器可能收到了。"""

    def __init__(self, message: str, *, sent: bool):
        super().__init__(message)
        self.sent = sent


class Link:
    def __init__(self, spec: dict[str, Any], *, timeout: float = 3.0):
        kind = str(spec.get("kind") or "tcp")
        if kind not in {"tcp", "serial"}:
            raise ValueError(f"链路 kind 只能是 tcp 或 serial，不是 {kind!r}")
        if kind == "tcp" and not (spec.get("host") and spec.get("port")):
            raise ValueError("TCP 链路要写 host 与 port")
        if kind == "serial" and not spec.get("port"):
            raise ValueError("串口链路要写 port（COM5、/dev/ttyACM0、rfc2217://主机:端口）")
        self.spec = dict(spec)
        self.kind = kind
        self.timeout = float(spec.get("timeout_sec") or timeout)
        self.lock = threading.RLock()        # 开、关连接
        self.read_lock = threading.RLock()   # 读线程长时间占着
        self.write_lock = threading.Lock()   # 写很快，终止不用等读
        self._conn: Any = None
        self._buffer = b""
        self.connects = 0  # 成功连上过几次：上层据此知道「重连过」，重连后先把仪器同步到已知状态
        self.sent = False

    def describe(self) -> str:
        return f"{self.spec['host']}:{self.spec['port']}" if self.kind == "tcp" else str(self.spec["port"])

    @property
    def connected(self) -> bool:
        return self._conn is not None

    # ---------- 连接 ----------

    def _open(self) -> Any:
        with self.lock:
            if self._conn is not None:
                return self._conn
            try:
                if self.kind == "tcp":
                    conn = socket.create_connection((self.spec["host"], int(self.spec["port"])), timeout=self.timeout)
                    conn.settimeout(self.timeout)
                else:
                    import serial  # pyserial：只有串口才要装

                    # EmStat4：921600、8N1，直连 UART 时建议 RTS/CTS；USB 虚拟串口这些设置不起作用。
                    # EmStat Pico：230400、8N1、XON/XOFF。缺省值由配置按型号给（driver/config.py）
                    conn = serial.serial_for_url(
                        self.spec["port"], baudrate=int(self.spec.get("baudrate") or 921600),
                        bytesize=int(self.spec.get("bytesize") or 8), parity=str(self.spec.get("parity") or "N"),
                        stopbits=float(self.spec.get("stopbits") or 1), timeout=self.timeout,
                        rtscts=bool(self.spec.get("rtscts", False)), xonxoff=bool(self.spec.get("xonxoff", False)),
                        dsrdtr=False,
                    )
            except (OSError, ValueError) as exc:
                raise LinkError(f"连不上 {self.describe()}：{exc}", sent=False) from exc
            self._conn, self._buffer = conn, b""
            self.connects += 1
            return conn

    def open(self) -> None:
        self._open()

    def close(self) -> None:
        with self.lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except OSError:
                    pass
            self._conn, self._buffer = None, b""

    # ---------- 收 ----------

    def _read_chunk(self, wait: float) -> bytes:
        conn = self._conn
        if conn is None:
            raise OSError("连接已关闭")
        wait = max(0.005, min(wait, self.timeout))
        if self.kind == "tcp":
            conn.settimeout(wait)
            return conn.recv(4096)
        conn.timeout = wait
        return conn.read(conn.in_waiting or 1)

    def _line(self, timeout: float, *, keep: bool) -> str | None:
        """读一行（不含行尾）。`keep`：等不到返回 None、连接保留；否则当链路坏了抛 LinkError。"""
        with self.read_lock:
            if self._conn is None:
                self._open()
            deadline = time.monotonic() + timeout
            while b"\n" not in self._buffer:
                left = deadline - time.monotonic()
                if left <= 0:
                    if keep:
                        return None
                    self.close()
                    raise LinkError(f"{self.describe()} 应答超时", sent=True)
                try:
                    chunk = self._read_chunk(left)
                except socket.timeout:
                    continue
                except OSError as exc:
                    self.close()
                    raise LinkError(f"{self.describe()} 读应答出错：{exc}", sent=True) from exc
                if not chunk:
                    if self.kind == "tcp":
                        self.close()
                        raise LinkError(f"{self.describe()} 关闭了连接", sent=True)
                    continue  # 串口：这一轮没读到字节
                self._buffer += bytes(byte for byte in chunk if byte not in FLOW_CONTROL)
            line, _, self._buffer = self._buffer.partition(b"\n")
            return line.decode("ascii", errors="replace").replace("\r", "")

    def read_line(self, timeout: float | None = None) -> str:
        """读一行。超时、断线抛 `LinkError`（sent=True：这时总是在等一条已经发出的命令的应答）。"""
        return self._line(self.timeout if timeout is None else timeout, keep=False)

    def try_read_line(self, timeout: float) -> str | None:
        """等一行，`timeout` 内没有完整的一行就返回 None（连接保留）。断线照样抛 `LinkError`。"""
        return self._line(timeout, keep=True)

    def drain(self, quiet: float = 0.05, longest: float = 2.0) -> list[str]:
        """丢掉晚到的旧字节：一直读到 `quiet` 秒没有新字节（最长 `longest` 秒）。返回丢掉的整行（日志用）。"""
        dropped: list[str] = []
        with self.read_lock:
            if self._conn is None:
                return dropped
            deadline = time.monotonic() + longest
            while time.monotonic() < deadline:
                try:
                    chunk = self._read_chunk(quiet)
                except socket.timeout:
                    break
                except OSError:
                    self.close()
                    break
                if not chunk:
                    if self.kind == "tcp":
                        self.close()
                    break
                self._buffer += bytes(byte for byte in chunk if byte not in FLOW_CONTROL)
            # 整行留给日志；半行也一起丢掉（下一条命令的应答从干净的缓冲开始）
            dropped = [line.decode("ascii", errors="replace") for line in self._buffer.split(b"\n")[:-1]]
            self._buffer = b""
        return dropped

    # ---------- 发 ----------

    def write(self, text: str) -> None:
        """写一段（一行或几行，调用方带好 `\\n`）。写不出去抛 `LinkError(sent=False)`。不清接收缓冲。"""
        data = text.encode("ascii")
        with self.write_lock:
            self.sent = False
            try:
                conn = self._open()
                if self.kind == "tcp":
                    conn.sendall(data)
                else:
                    conn.write(data)
                    conn.flush()
            except LinkError:
                raise
            except OSError as exc:
                self.close()
                raise LinkError(f"{self.describe()} 写命令出错：{exc}", sent=False) from exc
            self.sent = True
