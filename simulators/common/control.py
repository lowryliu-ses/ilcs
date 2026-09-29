"""模拟设备的统一控制口：故障注入与动作计数，各协议的模拟器都一样。

    GET  /simulator/state[?unit=AGV-01]
         → {"fault", "fault_parameter", "motions", "knows_command_ids", "executions"（认指令号时）, ...}
    POST /simulator/fault  {"mode": "lost_receipt", "parameter": 0, "unit": ""}

接入验收（`api/app/adapters/acceptance.py` 的 `SimulatorControlInjector`）只认这一套接口，不用为每种协议各写一个
注入器；ILCS 侧在适配器配置里登记 `simulator_control: {"url": "http://<模拟器>:9900", "token_ref": "file://…"}`。
各协议自己的注入方式（SiLA 2 SimulatorControl 特性、Modbus 控制寄存器、OPC UA 方法、行协议的 `SIM:` 命令……）
照旧保留，`simulators/*/fault.py` 还是用它们。

- 只在设了 `SIM_CONTROL_PORT` 时监听（试点 compose 里是 9900，只在后端网络可见）；
- `SIM_CONTROL_TOKEN_FILE`：Bearer 令牌文件，不存在就在首次启动时生成（属主只读）。不设就不校验令牌——只用于本机测试；
- `knows_command_ids`：设备认不认 ILCS 指令号。不认的（串口命令、PLC 点表、天平、车队）只报设备的总动作次数
  `motions`，验收据此判断「重投有没有让设备再动一次」。
"""
from __future__ import annotations

import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import secrets
import threading
from typing import Callable, Protocol
from urllib.parse import parse_qs, urlparse

from .device import FAULTS

log = logging.getLogger("ilcs.simulator.control")


class ControlTarget(Protocol):
    knows_command_ids: bool

    def set_fault(self, mode: str, parameter: float, unit: str = "") -> dict: ...

    def state(self, unit: str = "") -> dict: ...


class DeviceTarget:
    """用 `SimulatedDevice` 建模的模拟器：离线由协议层真的断开监听，其余故障交给设备模型。

    `unsupported`：这台模拟设备在协议层注入不了的故障与原因（点表设备没有回执可丢）。控制口照样接受这些模式，
    只是如实告诉验收清单，让它判跳过、不硬判不通过。
    """

    def __init__(self, device, go_offline: Callable[[float], None], *, knows_command_ids: bool = True,
                 unsupported: dict[str, str] | None = None):
        self.device = device
        self.go_offline = go_offline
        self.knows_command_ids = knows_command_ids
        self.unsupported = dict(unsupported or {})

    def set_fault(self, mode: str, parameter: float, unit: str = "") -> dict:
        if mode == "offline":
            self.go_offline(parameter or 5)
            return {**self.state(unit), "fault": "offline", "seconds": parameter or 5}
        self.device.set_fault(mode, parameter)
        return self.state(unit)

    def state(self, unit: str = "") -> dict:
        raw = self.device.state()
        executions = dict(raw.get("executions") or {})
        row = {
            "device_id": raw.get("device_id"), "fault": raw.get("fault"), "fault_parameter": raw.get("fault_parameter"),
            "motions": sum(int(count) for count in executions.values()), "knows_command_ids": self.knows_command_ids,
            "tasks": raw.get("tasks") or {}, "unsupported": dict(self.unsupported),
        }
        if self.knows_command_ids:
            row["executions"] = executions
        return row


def _token(path: str) -> str:
    """读令牌文件；不存在就生成一个。写不进去直接报错：不能静默地不设防。

    生成时先写属主只读的临时文件，再用硬链接发布：别人要么看不到文件、要么看到完整的属主只读文件；
    两个进程同时生成时后到的读先到的那份。
    """
    file = Path(path)
    if file.exists():
        return file.read_text(encoding="utf-8").strip()
    file.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    temporary = file.with_name(f".{file.name}.{secrets.token_hex(4)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(token)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, file)
        except FileExistsError:
            return file.read_text(encoding="utf-8").strip()
    finally:
        temporary.unlink(missing_ok=True)
    log.info("已生成模拟设备控制口令牌 %s；ILCS 适配器配置 simulator_control.token_ref 指向它", file)
    return token


class ControlServer:
    def __init__(self, target: ControlTarget, address: str, port: int, token: str = ""):
        self.target = target
        self.token = token
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):  # 控制请求不刷日志
                pass

            def _send(self, status: int, body: dict) -> None:
                data = json.dumps(body, ensure_ascii=False, default=str).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _authorized(self) -> bool:
                if not server.token:
                    return True
                return hmac.compare_digest(self.headers.get("Authorization", ""), f"Bearer {server.token}")

            def do_GET(self):  # noqa: N802
                parsed = urlparse(self.path)
                if not self._authorized():
                    return self._send(401, {"error": "Unauthorized"})
                if parsed.path.rstrip("/") != "/simulator/state":
                    return self._send(404, {"error": "NotFound"})
                unit = (parse_qs(parsed.query).get("unit") or [""])[0]
                try:
                    return self._send(200, server.target.state(unit))
                except KeyError as exc:
                    return self._send(404, {"error": "NotFound", "message": str(exc)})

            def do_POST(self):  # noqa: N802
                if not self._authorized():
                    return self._send(401, {"error": "Unauthorized"})
                if urlparse(self.path).path.rstrip("/") != "/simulator/fault":
                    return self._send(404, {"error": "NotFound"})
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(length) or b"{}")
                    mode, parameter = str(body.get("mode") or ""), float(body.get("parameter") or 0)
                except (ValueError, TypeError, AttributeError):
                    return self._send(422, {"error": "InvalidParameters", "message": "请求体必须是 JSON 对象"})
                if mode not in FAULTS:
                    return self._send(422, {"error": "InvalidParameters", "message": f"未知故障模式 {mode}"})
                try:
                    return self._send(200, server.target.set_fault(mode, parameter, str(body.get("unit") or "")))
                except KeyError as exc:
                    return self._send(404, {"error": "NotFound", "message": str(exc)})
                except ValueError as exc:
                    return self._send(422, {"error": "InvalidParameters", "message": str(exc)})

        self.httpd = ThreadingHTTPServer((address, port), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]

    def start(self) -> "ControlServer":
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True).start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def start_control(target: ControlTarget, *, port: int | None = None, address: str | None = None,
                  token_file: str | None = None) -> ControlServer | None:
    """按 `SIM_CONTROL_PORT` / `SIM_CONTROL_ADDRESS` / `SIM_CONTROL_TOKEN_FILE` 起控制口；没配端口就不起。"""
    env = os.environ.get
    port = port if port is not None else int(env("SIM_CONTROL_PORT") or 0) or None
    if port is None:
        return None
    token_file = token_file if token_file is not None else env("SIM_CONTROL_TOKEN_FILE", "")
    try:
        token = _token(token_file) if token_file else ""
    except OSError as exc:
        # 令牌写不进去（目录没建、属主不对）：控制口不开——不能不设防地开；设备本身照常模拟，故障项目会标跳过
        log.error("模拟设备控制口令牌 %s 读写失败（%s）：控制口不启动", token_file, exc)
        return None
    server = ControlServer(target, address or env("SIM_CONTROL_ADDRESS", "0.0.0.0"), port, token)
    log.info("模拟设备控制口已启动：%s:%s（%s）", address or env("SIM_CONTROL_ADDRESS", "0.0.0.0"), server.port,
             "带令牌" if token_file else "不校验令牌")
    return server.start()
