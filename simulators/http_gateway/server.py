"""ILCS HTTPS JSON 网关模拟设备。

模拟「厂商 SDK / 私有协议 → 现场网关 → HTTPS JSON」这一类接入：按 `http_json_v1` 驱动的网关契约
（见 docs/设备适配器配置模板.md「已内置：HTTPS JSON 网关驱动」）提供端点，外加仅模拟器才有的
`/simulator/fault`、`/simulator/state`。系统侧用 `http_json_v1` 驱动接入，和接一台真网关走同一条路。

    python simulators/http_gateway/server.py --device-id SIM-COAT-01 --port 8443 \\
        --cert-dir /run/secrets/ilcs/gateway --host-name gateway-sim-coater

首次启动在 --cert-dir 生成自签证书 `<设备ID>.crt/.key` 与访问令牌 `<设备ID>.token`：系统侧 ca_file 指向证书，
credential_ref 指向令牌文件。每个请求都要带 `Authorization: Bearer <令牌>`，否则 401。
--insecure 用明文 HTTP，仅用于本机测试（系统侧要显式 allow_insecure_http）。

状态码与驱动的判定规则一一对应：参数非法 / 不支持 422、联锁 / 忙 423（明确拒绝，设备没动），
查不到指令 404；回执丢失时直接断开连接不回任何响应（驱动判结果未知）。
"""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import logging
import os
from pathlib import Path
import secrets
import ssl
import sys
import threading
import time
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:  # 直接运行（容器）时也能找到 simulators 包
    sys.path.insert(0, str(ROOT))

from simulators.common.certs import ensure_certificate, self_signed_certificate  # noqa: E402
from simulators.common.device import DeviceRejected, ReceiptLost, SimulatedDevice  # noqa: E402
from simulators.common.runtime import build_device, configure_logging, device_arguments, serve_forever  # noqa: E402

REJECTION_STATUS = {"InvalidParameters": 422, "NotSupported": 422, "Interlocked": 423, "DeviceBusy": 423}
MAX_BODY = 1024 * 1024
log = logging.getLogger("ilcs.gateway-sim")


class _Dropped(Exception):
    """回执丢失：连接直接断开，不写任何响应。"""


def handler_for(device: SimulatedDevice, runner: "SimulatorRunner", prefix: str, token: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ILCS-Gateway-Sim/1.0"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # 走 logging，别往 stderr 直写
            log.debug("%s %s", self.address_string(), fmt % args)

        def _reply(self, status: int, body: dict) -> None:
            raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise DeviceRejected("InvalidParameters", "请求体超过 1 MiB")
            try:
                value = json.loads(self.rfile.read(length) or b"{}")
            except ValueError as exc:
                raise DeviceRejected("InvalidParameters", "请求体不是有效 JSON") from exc
            if not isinstance(value, dict):
                raise DeviceRejected("InvalidParameters", "请求体必须是 JSON 对象")
            return value

        def _authorized(self) -> bool:
            supplied = self.headers.get("Authorization") or ""
            return bool(token) and hmac.compare_digest(supplied, f"Bearer {token}")

        def _route(self, method: str):
            path = urlparse(self.path).path
            if not path.startswith(prefix):
                return 404, {"error": "NotFound", "message": path}
            parts = [unquote(part) for part in path[len(prefix):].strip("/").split("/") if part]
            if method == "GET" and parts == ["health"]:
                identity = device.identity()
                return 200, {**identity, "reachable": True, "version": identity["firmware"]}
            if method == "GET" and parts == ["simulator", "state"]:
                return 200, device.state()
            if method == "POST" and parts == ["simulator", "fault"]:
                body = self._body()
                mode, parameter = str(body.get("mode") or ""), float(body.get("parameter") or 0)
                if mode == "offline":
                    runner.go_offline(parameter or 5)
                    return 200, {**device.state(), "fault": "offline", "seconds": parameter or 5}
                try:
                    return 200, device.set_fault(mode, parameter)
                except ValueError as exc:
                    return 422, {"error": "InvalidParameters", "message": str(exc)}
            if method == "POST" and parts == ["commands"]:
                body = self._body()
                context = {key: body.get(key) for key in ("batch_id", "step_index", "step_id", "target_command_id")}
                params = body.get("params") or {}
                if not isinstance(params, dict):
                    raise DeviceRejected("InvalidParameters", "params 必须是对象")
                try:
                    return 200, device.submit(
                        str(body.get("command_id") or ""), str(body.get("type") or "dispatch"),
                        str(body.get("capability") or ""), params, context,
                    )
                except ReceiptLost as exc:
                    raise _Dropped() from exc
            if method == "GET" and len(parts) == 2 and parts[0] == "commands":
                receipt = device.query(parts[1])
                if receipt["state"] == "not_found":
                    return 404, {"error": "NotFound", "command_id": parts[1]}
                return 200, receipt
            if method == "POST" and len(parts) == 3 and parts[0] == "commands" and parts[2] in {"hold", "abort"}:
                target = str(self._body().get("target_command_id") or "")
                action = device.hold if parts[2] == "hold" else device.abort
                return 200, action(parts[1], target)
            return 404, {"error": "NotFound", "message": path}

        def _handle(self, method: str) -> None:
            if not self._authorized():
                self._reply(401, {"error": "Unauthorized", "message": "缺少或错误的访问令牌"})
                return
            try:
                status, body = self._route(method)
            except DeviceRejected as error:
                status, body = REJECTION_STATUS[error.identifier], {"error": error.identifier, "message": error.message}
            except _Dropped:
                self.close_connection = True
                self.connection.close()
                return
            self._reply(status, body)

        def do_GET(self):  # noqa: N802
            self._handle("GET")

        def do_POST(self):  # noqa: N802
            self._handle("POST")

    return Handler


class SimulatorRunner:
    def __init__(self, args: argparse.Namespace, device: SimulatedDevice, token: str | None = None):
        self.args = args
        self.device = device
        self.server: ThreadingHTTPServer | None = None
        self.lock = threading.Lock()
        self.tls: ssl.SSLContext | None = None
        if not args.insecure:
            key, cert = ensure_certificate(args.cert_dir, args.device_id, lambda: self_signed_certificate(
                args.host_name, "ILCS Gateway Simulator",
            ))
            self.tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            self.tls.minimum_version = ssl.TLSVersion.TLSv1_2
            self.tls.load_cert_chain(str(cert), str(key))
        self.token = token if token is not None else self._token()

    def _token(self) -> str:
        path = Path(self.args.cert_dir) / f"{self.args.device_id}.token"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(secrets.token_urlsafe(32))
            path.chmod(0o600)
            log.info("已生成访问令牌 %s；ILCS 适配器 credential_ref 指向它", path)
        return path.read_text().strip()

    def start(self) -> None:
        with self.lock:
            handler = handler_for(self.device, self, self.args.path_prefix.rstrip("/"), self.token)
            server = ThreadingHTTPServer((self.args.address, self.args.port), handler)
            server.daemon_threads = True
            if self.tls is not None:
                # 握手推迟到处理线程里的首次读写：不握手的连接不能卡住接受新连接的线程
                server.socket = self.tls.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
            threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True).start()
            self.server = server
        scheme = "http" if self.tls is None else "https"
        log.info("网关模拟设备 %s 已启动：%s://%s:%s%s", self.args.device_id, scheme, self.args.address,
                 self.args.port, self.args.path_prefix)

    def stop(self) -> None:
        with self.lock:
            server, self.server = self.server, None
        if server is not None:
            server.shutdown()
            server.server_close()

    def go_offline(self, seconds: float) -> None:
        def cycle():
            time.sleep(0.2)  # 先把 fault 请求的应答送回去
            self.stop()
            log.info("模拟离线 %.0f s", seconds)
            time.sleep(seconds)
            self.start()

        threading.Thread(target=cycle, daemon=True).start()


def parse(argv=None) -> argparse.Namespace:
    env = os.environ.get
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    device_arguments(parser, default_port=8443)
    parser.add_argument("--cert-dir", default=env("SIM_CERT_DIR", "./gateway-certs"))
    parser.add_argument("--path-prefix", default=env("SIM_PATH_PREFIX", "/api/v1"))
    parser.add_argument("--insecure", action="store_true", default=env("SIM_INSECURE", "0") == "1")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    configure_logging()
    args = parse(argv)
    device = build_device(args)
    runner = SimulatorRunner(args, device)
    runner.start()
    serve_forever(device, args.tick_seconds, runner.stop)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
