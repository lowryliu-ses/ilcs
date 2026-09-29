"""文本协议模拟设备的故障注入：连上设备端口，发模拟器专用命令 SIM:FAULT / SIM:STATE?（真设备没有这两条）。"""
from __future__ import annotations

import json
import socket


def send(host: str, port: int, line: str, *, newline: bytes = b"\r\n", greeting: bool = False, timeout: float = 5) -> str:
    with socket.create_connection((host, port), timeout=timeout) as sock:
        buffer = b""

        def read_line() -> str:
            nonlocal buffer
            while b"\n" not in buffer:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buffer += chunk
            raw, _, buffer = buffer.partition(b"\n")
            return raw.rstrip(b"\r").decode("latin-1")

        if greeting:
            read_line()
        sock.sendall(line.encode() + newline)
        return read_line()


def show(reply: str) -> None:
    try:
        print(json.dumps(json.loads(reply), ensure_ascii=False, indent=2))
    except ValueError:
        print(reply)
