"""网关的 HTTPS 服务：令牌、TLS、路由。只用 Python 标准库（证书自签要装 cryptography），能跑在设备旁的 Windows 工控机上。

    GET  {prefix}/health                         设备身份、方法目录、联锁、接不接指令
    POST {prefix}/commands                       提交（按 command_id 去重）
    GET  {prefix}/commands/{command_id}          按指令号查询；查不到 404
    POST {prefix}/commands/{command_id}/hold     保持（body 里 target_command_id 是要停的作业）
    POST {prefix}/commands/{command_id}/abort    终止
    GET  {prefix}/simulator/state                模拟设备才有：故障模式、各指令号动作次数、总动作次数
    POST {prefix}/simulator/fault                模拟设备才有：{"mode": "lost_receipt", "parameter": 0}

每个请求都要带 `Authorization: Bearer <令牌>`；令牌文件不存在就在首次启动时生成（创建时即属主只读），ILCS 侧的
credential_ref 指向它。HTTPS 网关必须有令牌；明文 HTTP（--insecure）只用于本机联调，缺省只监听 127.0.0.1。状态码与 ILCS 驱动的判定一一对应：参数非法 / 不支持 422，联锁 / 忙 423（明确拒绝，设备没动），
查不到 404；回执丢失时直接断开连接、不回任何响应。模拟设备的控制口与 ILCS 的统一控制口是同一套接口，
接入验收的故障项目直接能用。
"""
from __future__ import annotations

import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import logging
import os
from pathlib import Path
import secrets
import ssl
import threading
import time
from typing import Any
from urllib.parse import unquote, urlparse

from .device import REJECTIONS, Device, ReceiptLost, Rejected
from .gateway import Gateway
from .ledger import Ledger, LedgerError

MAX_BODY = 1024 * 1024
RESTART_ATTEMPTS = 10
FAULT_MODES = {"none", "offline", "slow_submit", "lost_receipt", "no_dedup", "fail", "partial", "stuck", "interlock",
               "busy", "clock_skew"}
log = logging.getLogger("ilcs.gateway")


class _Dropped(Exception):
    """回执丢失：连接直接断开，不写任何响应。"""


def write_private(path: str | Path, data: bytes) -> None:
    """写秘密文件：先写到属主只读的临时文件、落盘，再原子替换——任何时刻都不会有别人可读的半个文件。"""
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    temporary = file.with_name(f".{file.name}.{secrets.token_hex(4)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, file)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def ensure_token(path: str | Path) -> str:
    file = Path(path)
    if not file.exists():
        write_private(file, secrets.token_urlsafe(32).encode())
        log.info("已生成访问令牌 %s；ILCS 适配器 credential_ref 指向它", file)
    token = file.read_text(encoding="utf-8").strip()
    if len(token) < 16:
        raise SystemExit(f"访问令牌 {file} 太短（至少 16 个字符）")
    return token


def ensure_certificate(cert: str | Path, key: str | Path, host_name: str) -> None:
    """证书不存在就自签一张（主机名写进 SAN，ILCS 侧 ca_file 指向这张证书）。要装 cryptography。"""
    cert, key = Path(cert), Path(key)
    if cert.exists() and key.exists():
        return
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID
    except ImportError as exc:  # pragma: no cover - 取决于部署环境
        raise SystemExit("自签证书要安装 cryptography；或者用 --cert / --key 指定现场签发的证书") from exc
    import datetime
    import ipaddress

    private = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host_name)])
    try:
        alt = x509.IPAddress(ipaddress.ip_address(host_name))
    except ValueError:
        alt = x509.DNSName(host_name)
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(private.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=825))
        .add_extension(x509.SubjectAlternativeName([alt]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(private, hashes.SHA256())
    )
    cert.parent.mkdir(parents=True, exist_ok=True)
    write_private(key, private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                             serialization.NoEncryption()))
    cert.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    log.info("已生成自签证书 %s（主机名 %s）；ILCS 侧 ca_file 指向它", cert, host_name)


class GatewayServer:
    def __init__(self, device: Device, *, device_id: str, state_dir: str | Path, address: str = "127.0.0.1",
                 port: int = 8443, prefix: str = "/api/v1", token: str = "", tls: ssl.SSLContext | None = None):
        self.device_id = device_id
        self.gateway = Gateway(device, Ledger(Path(state_dir) / f"{device_id}.json"))
        self.address, self.port, self.prefix = address, port, prefix.rstrip("/")
        self.token = token
        self.tls = tls
        self.httpd: ThreadingHTTPServer | None = None
        self.lock = threading.Lock()

    # ---------- 路由 ----------

    def route(self, method: str, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if not path.startswith(self.prefix):
            return 404, {"error": "NotFound", "message": path}
        parts = [unquote(part) for part in path[len(self.prefix):].strip("/").split("/") if part]
        gateway = self.gateway
        if method == "GET" and parts == ["health"]:
            return 200, gateway.health()
        if parts[:1] == ["simulator"]:
            return self._simulator(method, parts, body)
        if method == "POST" and parts == ["commands"]:
            return 200, gateway.submit(body)
        if method == "GET" and len(parts) == 2 and parts[0] == "commands":
            receipt = gateway.query(parts[1])
            return (404, {"error": "NotFound", "command_id": parts[1]}) if receipt is None else (200, receipt)
        if method == "POST" and len(parts) == 3 and parts[0] == "commands" and parts[2] in {"hold", "abort"}:
            return 200, gateway.control(parts[2], parts[1], body)
        return 404, {"error": "NotFound", "message": path}

    def _simulator(self, method: str, parts: list[str], body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        target = self.gateway.device.fault_target()
        if target is None:
            return 404, {"error": "NotFound", "message": "真实设备没有模拟控制口"}
        if method == "GET" and parts == ["simulator", "state"]:
            return 200, self.gateway.fault_state()
        if method == "POST" and parts == ["simulator", "fault"]:
            mode, parameter = str(body.get("mode") or ""), float(body.get("parameter") or 0)
            if mode not in FAULT_MODES:
                return 422, {"error": "InvalidParameters", "message": f"未知故障模式 {mode}"}
            if mode == "offline":
                self.go_offline(parameter or 5)
                return 200, {**self.gateway.fault_state(), "fault": "offline", "seconds": parameter or 5}
            try:
                target.set_fault(mode, parameter)
            except ValueError as exc:
                return 422, {"error": "InvalidParameters", "message": str(exc)}
            return 200, self.gateway.fault_state()
        return 404, {"error": "NotFound"}

    # ---------- 服务 ----------

    def _handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "ILCS-Gateway-SDK/1.0"
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):
                log.debug("%s %s", self.address_string(), fmt % args)

            def _reply(self, status: int, body: dict[str, Any]) -> None:
                raw = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _body(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY:
                    raise Rejected("invalid", "请求体超过 1 MiB")
                try:
                    value = json.loads(self.rfile.read(length) or b"{}")
                except ValueError as exc:
                    raise Rejected("invalid", "请求体不是有效 JSON") from exc
                if not isinstance(value, dict):
                    raise Rejected("invalid", "请求体必须是 JSON 对象")
                return value

            def _handle(self, method: str) -> None:
                supplied = self.headers.get("Authorization") or ""
                if server.token and not hmac.compare_digest(supplied, f"Bearer {server.token}"):
                    self._reply(401, {"error": "Unauthorized", "message": "缺少或错误的访问令牌"})
                    return
                try:
                    status, body = server.route(method, urlparse(self.path).path, self._body() if method == "POST" else {})
                except Rejected as rejection:
                    error, status = REJECTIONS[rejection.kind]
                    body = {"error": error, "message": rejection.message}
                except ReceiptLost:
                    self.close_connection = True
                    self.connection.close()
                    return
                except LedgerError as exc:
                    # 台账不可用：宁可不接活，也不在忘了自己做过什么的状态下驱动设备
                    status, body = 503, {"error": "LedgerUnavailable", "message": str(exc)}
                except Exception as exc:  # noqa: BLE001  网关自己的意外：不知道结论，按 5xx 报（ILCS 判结果未知）
                    log.exception("网关处理请求出错")
                    status, body = 500, {"error": "GatewayError", "message": str(exc)}
                self._reply(status, body)

            def do_GET(self):  # noqa: N802
                self._handle("GET")

            def do_POST(self):  # noqa: N802
                self._handle("POST")

        return Handler

    def start(self) -> "GatewayServer":
        with self.lock:
            httpd = ThreadingHTTPServer((self.address, self.port), self._handler())
            httpd.daemon_threads = True
            if self.tls is not None:
                # 握手推迟到处理线程里的首次读写：不握手的连接不能卡住接受新连接的线程
                httpd.socket = self.tls.wrap_socket(httpd.socket, server_side=True, do_handshake_on_connect=False)
            self.port = httpd.server_address[1]
            threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True).start()
            self.httpd = httpd
        log.info("网关 %s 已启动：%s://%s:%s%s", self.device_id, "https" if self.tls else "http", self.address,
                 self.port, self.prefix)
        return self

    def stop(self) -> None:
        with self.lock:
            httpd, self.httpd = self.httpd, None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()

    def go_offline(self, seconds: float) -> None:
        """模拟失联：真的停止监听 N 秒再恢复（先把这次控制请求的应答送回去）。"""
        def cycle():
            time.sleep(0.2)
            self.stop()
            log.info("模拟离线 %.0f s", seconds)
            time.sleep(seconds)
            # 恢复监听失败就隔一会儿再试：不能从此停在离线——验收的失联项目之后还要按指令号收尾
            for attempt in range(1, RESTART_ATTEMPTS + 1):
                try:
                    self.start()
                    return
                except OSError:
                    log.exception("网关恢复监听失败（第 %d 次），1 秒后重试", attempt)
                    time.sleep(1)
            log.error("网关恢复监听失败 %d 次，保持离线：请重启网关", RESTART_ATTEMPTS)

        threading.Thread(target=cycle, daemon=True).start()


def _loopback(address: str) -> bool:
    if address == "localhost":
        return True
    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:
        return False


def serve(device: Device, **options: Any) -> GatewayServer:
    """起一个网关并开始监听。参数见 `build_server`。"""
    return build_server(device, **options).start()


def build_server(device: Device, *, device_id: str, state_dir: str | Path, address: str | None = None,
                 port: int = 8443, prefix: str = "/api/v1", token_file: str | Path | None = None,
                 cert: str | Path | None = None, key: str | Path | None = None, host_name: str = "localhost",
                 insecure: bool = False) -> GatewayServer:
    """建好一个网关（证书、令牌、台账都就绪），还不开始监听：调用方自己 `start()`。

    - HTTPS（缺省）：必须有证书与令牌，缺省监听所有地址；
    - `insecure=True`：明文 HTTP，只用于本机联调（ILCS 侧要显式 allow_insecure_http），缺省只监听 127.0.0.1；
      不带令牌时只许监听本机地址——明文又不认人的网关放到网络上，谁都能让设备动。
    """
    address = address or ("127.0.0.1" if insecure else "0.0.0.0")
    tls = None
    if not insecure:
        if cert is None or key is None:
            raise SystemExit("HTTPS 要给证书与私钥（--cert / --key），本机联调可以用 --insecure")
        if not token_file:
            raise SystemExit("HTTPS 网关要有访问令牌文件（token_file）：ILCS 侧的 credential_ref 指向它")
        ensure_certificate(cert, key, host_name)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.minimum_version = ssl.TLSVersion.TLSv1_2
        tls.load_cert_chain(str(cert), str(key))
    elif not token_file and not _loopback(address):
        raise SystemExit(f"明文 HTTP 且没有访问令牌时只能监听本机地址，不能监听 {address}")
    token = ensure_token(token_file) if token_file else ""
    return GatewayServer(device, device_id=device_id, state_dir=state_dir, address=address, port=port, prefix=prefix,
                         token=token, tls=tls)
