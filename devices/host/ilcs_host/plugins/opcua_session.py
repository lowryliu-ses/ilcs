"""OPC UA 会话：端点白名单、安全策略、服务器证书钉住、客户端证书、超时与断线重建（从 ILCS 的 `opcua.py` 抽出）。

错误分类沿用三分法：约定的拒绝状态码 → 明确失败；通信层状态码、超时、断线 → 结果未知并重建会话；其他 Bad 状态码 → 无法确认。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
import socket
import threading
from urllib.parse import urlparse

from ..settings import settings
from .base import AdapterError, AdapterIndeterminate, AdapterUnreachable

# asyncua 每次建连都要就客户端证书里的主机名打告警：OPC UA 按应用 URI 认证，主机名不参与
logging.getLogger("asyncua.crypto.uacrypto").setLevel(logging.ERROR)
# 通信层的状态码：请求可能没到设备，也可能到了没回来——都是结果未知，并且要重建会话
COMMUNICATION = {
    "BadTimeout", "BadRequestTimeout", "BadSessionClosed", "BadSessionIdInvalid", "BadSecureChannelClosed",
    "BadSecureChannelIdInvalid", "BadConnectionClosed", "BadCommunicationError", "BadServerNotConnected",
    "BadNotConnected", "BadTooManySessions", "BadServerHalted", "BadShutdown",
}


class OpcUaSession:
    """opc.tcp 会话：端点白名单、安全策略、服务器证书钉住、客户端证书、超时与断线重建。

    `opcua_v1`（ILCS TaskExecution）与 `opcua_map_v1`（设备自有节点映射）共用。
    """

    def __init__(self, config: dict, credential_ref: str, driver: str):
        self.config = config
        self.driver = driver
        self.endpoint = str(config.get("endpoint") or "")
        parsed = urlparse(self.endpoint)
        if parsed.scheme != "opc.tcp" or not parsed.hostname:
            raise AdapterError(f"{driver} 必须配置 opc.tcp:// 开头的 endpoint")
        self.host, self.port = parsed.hostname, parsed.port or 4840
        if not settings.adapter_host_allowed(self.host):
            raise AdapterError(f"OPC UA 服务器主机 {self.host} 不在驱动宿主的 allowed_hosts 白名单")
        self.policy = str(config.get("security_policy") or "Basic256Sha256")
        if self.policy not in {"Basic256Sha256", "None"}:
            raise AdapterError("security_policy 只支持 Basic256Sha256 或 None")
        if self.policy == "None" and settings.environment == "production":
            raise AdapterError("production 模式禁止不加密的 OPC UA 连接")
        self.mode = str(config.get("security_mode") or "SignAndEncrypt")
        if self.mode not in {"SignAndEncrypt", "Sign"}:
            raise AdapterError("security_mode 只支持 SignAndEncrypt 或 Sign")
        self.application_uri = str(config.get("application_uri") or "urn:ilcs:client")
        self.connect_timeout = self._positive("connect_timeout_sec", 3.0)
        self.request_timeout = self._positive("request_timeout_sec", 10.0)
        self.credential_ref = credential_ref or ""
        self.security = self._security() if self.policy != "None" else None
        self.client = None
        self.lock = threading.RLock()

    def _positive(self, key: str, default: float) -> float:
        try:
            value = float(self.config.get(key, default))
        except (TypeError, ValueError) as exc:
            raise AdapterError(f"{key} 必须是正数") from exc
        if value <= 0 or value > 3600:
            raise AdapterError(f"{key} 必须在 0–3600 秒之间")
        return value

    @staticmethod
    def _within_root(path: Path, label: str) -> Path:
        path = path.resolve()
        root = Path(settings.adapter_credential_root).resolve()
        if path != root and root not in path.parents:
            raise AdapterError(f"{label} 必须位于驱动宿主的 credential_root 目录内")
        if not path.is_file():
            raise AdapterError(f"{label} 不存在或不可读取")
        return path

    def _security(self) -> dict:
        """服务器证书钉住；客户端证书与私钥路径来自 credential_ref 描述文件（相对路径按描述文件所在目录）。"""
        server_certificate = str(self.config.get("server_certificate") or "")
        if not server_certificate:
            raise AdapterError("加密连接必须配置 server_certificate（钉住服务器证书，不信任首次连接拿到的证书）")
        if not self.credential_ref.startswith("file://"):
            raise AdapterError("OPC UA 客户端证书用 credential_ref = file://<描述文件> 引用")
        descriptor = self._within_root(Path(urlparse(self.credential_ref).path), "客户端证书描述文件")
        try:
            described = json.loads(descriptor.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise AdapterError("客户端证书描述文件不是有效 JSON") from exc
        if not isinstance(described, dict) or not described.get("certificate") or not described.get("private_key"):
            raise AdapterError("客户端证书描述文件必须含 certificate 与 private_key")
        return {
            "server_certificate": str(self._within_root(Path(server_certificate), "server_certificate")),
            "certificate": str(self._within_root(descriptor.parent / described["certificate"], "客户端证书")),
            "private_key": str(self._within_root(descriptor.parent / described["private_key"], "客户端私钥")),
        }

    def connect(self):
        """返回已连接的 sync Client；连不上抛 AdapterUnreachable。"""
        if self.client is not None:
            return self.client
        try:  # 先用短超时探测端口：黑洞地址不能挂住执行器
            socket.create_connection((self.host, self.port), timeout=self.connect_timeout).close()
        except OSError as exc:
            raise AdapterUnreachable(f"OPC UA 服务器不可达：{exc.__class__.__name__}") from exc
        from asyncua import ua
        from asyncua.crypto.security_policies import SecurityPolicyBasic256Sha256
        from asyncua.crypto.uacrypto import CertProperties
        from asyncua.sync import Client, ThreadLoop

        def material(path: str) -> CertProperties:  # PEM 或 DER 按内容判断，不看扩展名
            with open(path, "rb") as handle:
                pem = handle.read(11) == b"-----BEGIN "
            return CertProperties(path, extension="pem" if pem else "der")

        # 自带事件循环线程：设成守护线程，执行器退出时不会被它拖住；sync 包装层默认等 120 s，
        # 设备卡住时要按请求超时放手，不能挂住执行器线程
        loop = ThreadLoop(self.request_timeout + self.connect_timeout)
        loop.daemon = True
        loop.start()
        client = Client(self.endpoint, timeout=self.request_timeout, tloop=loop)
        client.application_uri = self.application_uri
        client.aio_obj.name = "ILCS"
        try:
            if self.security is not None:
                client.set_security(
                    SecurityPolicyBasic256Sha256, material(self.security["certificate"]),
                    material(self.security["private_key"]),
                    server_certificate=material(self.security["server_certificate"]),
                    mode=getattr(ua.MessageSecurityMode, self.mode),
                )
            client.connect()
        except Exception as exc:
            self._close(client)
            raise AdapterUnreachable(f"OPC UA 连接失败：{exc.__class__.__name__}: {exc}") from exc
        self.client = client
        return client

    @staticmethod
    def _close(client) -> None:
        try:
            client.disconnect()
        except Exception:
            pass
        try:
            client.tloop.stop()
        except Exception:
            pass

    def drop(self) -> None:
        if self.client is not None:
            self._close(self.client)
        self.client = None

    def classify(self, exc: Exception, action: str, rejections: dict | None = None) -> Exception:
        """Bad 状态码：约定的拒绝 → 明确失败；通信层 → 结果未知并重建会话；其他 → 无法确认。"""
        from asyncua import ua

        if isinstance(exc, AdapterError):
            return exc
        if isinstance(exc, ua.UaStatusCodeError):
            name = ua.status_codes.get_name_and_doc(exc.code)[0]
            rejection = (rejections or {}).get(name)
            if rejection:
                return AdapterError(f"设备拒绝（{rejection}）", code=rejection)
            if name in COMMUNICATION:
                self.drop()
                return AdapterUnreachable(f"{action}无结论：{name}")
            return AdapterIndeterminate(f"{action}返回未约定的状态 {name}")
        self.drop()  # 超时、连接中断、事件循环异常
        return AdapterUnreachable(f"{action}无结论：{exc.__class__.__name__}")
