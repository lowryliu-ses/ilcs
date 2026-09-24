"""OPC UA 设备驱动（`opcua_v1`）。

设备（或厂商 OPC UA 服务器）实现 `contracts/opcua/TaskExecution.json`：在 `urn:ilcs:task-execution`
命名空间的 `Objects/ILCS/TaskExecution` 上提供 SubmitTask / QueryTask / HoldTask / AbortTask 方法与
DeviceIdentity 变量，回执 JSON 与 `http_json_v1`、`sila2_v1` 同一份契约。

安全：默认 Basic256Sha256 + SignAndEncrypt，服务器证书按配置钉住（不信任首次连接时拿到的证书），
客户端证书与私钥由 `credential_ref` 指向的描述文件给出，不写进配置。`security_policy: "None"` 只允许
在非正式环境使用。

错误分类沿用系统的三分法：
- 契约约定的拒绝状态码（参数非法 / 联锁 / 忙 / 不支持）：设备明确没动 → `AdapterError`；
- 连接失败、会话断开、请求超时：结果未知 → `AdapterUnreachable`；
- 其他 Bad 状态码、回执不合规：设备有响应但无法确认 → `AdapterIndeterminate`。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
import socket
import threading
from urllib.parse import urlparse

from ..core.config import settings
from .base import (
    AdapterContract, AdapterError, AdapterIndeterminate, AdapterUnreachable, CommandRequest,
    CommandResult,
)
from .contract import parse_receipt

DRIVER = "opcua_v1"
CONTRACT = json.loads(
    (Path(__file__).resolve().parents[3] / "contracts" / "opcua" / "TaskExecution.json").read_text(encoding="utf-8")
)
# asyncua 每次建连都要就客户端证书里的主机名打告警：OPC UA 按应用 URI 认证，主机名不参与
logging.getLogger("asyncua.crypto.uacrypto").setLevel(logging.ERROR)
# 通信层的状态码：请求可能没到设备，也可能到了没回来——都是结果未知，并且要重建会话
COMMUNICATION = {
    "BadTimeout", "BadRequestTimeout", "BadSessionClosed", "BadSessionIdInvalid", "BadSecureChannelClosed",
    "BadSecureChannelIdInvalid", "BadConnectionClosed", "BadCommunicationError", "BadServerNotConnected",
    "BadNotConnected", "BadTooManySessions", "BadServerHalted", "BadShutdown",
}


class OpcUaAdapter:
    def __init__(self, record):
        self.station_id = record.station_id
        self.config = dict(record.config or {})
        self.endpoint = str(self.config.get("endpoint") or "")
        parsed = urlparse(self.endpoint)
        if parsed.scheme != "opc.tcp" or not parsed.hostname:
            raise AdapterError("opcua_v1 必须配置 opc.tcp:// 开头的 endpoint")
        self.host, self.port = parsed.hostname, parsed.port or 4840
        if self.host.lower() not in settings.adapter_allowed_host_set:
            raise AdapterError(f"OPC UA 服务器主机 {self.host} 不在 ILCS_ADAPTER_ALLOWED_HOSTS 白名单")
        self.policy = str(self.config.get("security_policy") or "Basic256Sha256")
        if self.policy not in {"Basic256Sha256", "None"}:
            raise AdapterError("security_policy 只支持 Basic256Sha256 或 None")
        if self.policy == "None" and settings.environment == "production":
            raise AdapterError("production 模式禁止不加密的 OPC UA 连接")
        self.mode = str(self.config.get("security_mode") or "SignAndEncrypt")
        if self.mode not in {"SignAndEncrypt", "Sign"}:
            raise AdapterError("security_mode 只支持 SignAndEncrypt 或 Sign")
        self.application_uri = str(self.config.get("application_uri") or "urn:ilcs:client")
        self.connect_timeout = self._positive("connect_timeout_sec", 3.0)
        self.request_timeout = self._positive("request_timeout_sec", 10.0)
        self.expected_device_id = str(self.config.get("expected_device_id") or "")
        from .http_json import HttpJsonAdapter

        self.device_timezone = HttpJsonAdapter._timezone(str(self.config.get("device_timezone") or "UTC"))
        self.credential_ref = record.credential_ref or ""
        self.security = self._security() if self.policy != "None" else None
        self._client = None
        self._nodes: dict = {}
        self._lock = threading.Lock()
        self.contract = AdapterContract(
            kind="real", protocol=record.protocol or "OPC UA", version=record.version or "1.0",
            supports_hold=bool(record.supports_hold), supports_abort=bool(record.supports_abort),
            supports_query=bool(record.supports_query), supports_dedup=bool(record.supports_dedup),
            note=record.note or "OPC UA TaskExecution 设备",
        )

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
            raise AdapterError(f"{label} 必须位于 ILCS_ADAPTER_CREDENTIAL_ROOT 目录内")
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

    # ---------- 连接 ----------

    def _connect(self):
        if self._client is not None:
            return self._client
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
        try:
            index = client.get_namespace_index(CONTRACT["namespace"])
            task = client.nodes.objects.get_child([f"{index}:{name}" for name in CONTRACT["path"]])
            self._nodes = {
                "task": task,
                "identity": task.get_child(f"{index}:DeviceIdentity"),
                **{name: f"{index}:{name}" for name in CONTRACT["methods"]},
            }
        except Exception as exc:
            self._close(client)
            raise AdapterError(f"服务器没有实现 ILCS TaskExecution 契约（{CONTRACT['namespace']}）：{exc}") from exc
        self._client = client
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

    def close(self) -> None:
        """配置换版本或缓存清空时由注册表调用：关掉会话，不留悬空的连接与线程。"""
        with self._lock:
            self._drop()

    def _drop(self) -> None:
        if self._client is not None:
            self._close(self._client)
        self._client = None
        self._nodes = {}

    def _classify(self, exc: Exception, action: str) -> Exception:
        from asyncua import ua

        if isinstance(exc, ua.UaStatusCodeError):
            name = ua.status_codes.get_name_and_doc(exc.code)[0]
            rejection = CONTRACT["rejections"].get(name)
            if rejection:
                return AdapterError(f"设备拒绝（{rejection}）")
            if name in COMMUNICATION:
                self._drop()
                return AdapterUnreachable(f"{action}无结论：{name}")
            return AdapterIndeterminate(f"{action}返回未约定的状态 {name}")
        self._drop()  # 超时、连接中断、事件循环异常
        return AdapterUnreachable(f"{action}无结论：{exc.__class__.__name__}")

    def _invoke(self, method: str, *arguments: str) -> dict:
        with self._lock:
            self._connect()
            try:
                raw = self._nodes["task"].call_method(self._nodes[method], *arguments)
            except Exception as exc:
                raise self._classify(exc, f"OPC UA 调用 {method} ") from exc
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise AdapterIndeterminate("设备回执不是有效 JSON") from exc
        return payload

    # ---------- 契约 ----------

    def identity(self) -> dict:
        with self._lock:
            self._connect()
            try:
                raw = self._nodes["identity"].read_value()
            except Exception as exc:
                raise self._classify(exc, "读取设备身份") from exc
        try:
            return json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise AdapterIndeterminate("DeviceIdentity 不是有效 JSON") from exc

    def healthcheck(self) -> dict:
        identity = self.identity()
        if identity.get("simulator") and settings.environment == "production":
            raise AdapterError("该 OPC UA 设备自报为模拟器；正式环境不接入模拟设备")
        actual = str(identity.get("device_id") or "")
        if self.expected_device_id and actual != self.expected_device_id:
            raise AdapterError(f"设备身份不匹配：期望 {self.expected_device_id}，实际 {actual or '缺失'}")
        return {
            "reachable": True, "driver": DRIVER, "protocol": self.contract.protocol,
            "device_id": actual, "model": identity.get("model", ""),
            "simulator": bool(identity.get("simulator")),
            "interlock": bool(identity.get("interlock")),
            "accepts_commands": bool(identity.get("accepts_commands", True)),
            "security": f"{self.policy}/{self.mode}" if self.security else "None",
        }

    def _result(self, response: dict, command_id: str) -> CommandResult:
        return parse_receipt(response, command_id, f"real:{DRIVER}", self.device_timezone)

    def submit(self, request: CommandRequest) -> CommandResult:
        context = json.dumps({
            "batch_id": request.batch_id, "step_index": request.step_index, "step_id": request.step_id,
            "target_command_id": request.target_command_id,
            **({"method": request.method} if request.method else {}),
        }, ensure_ascii=False)
        response = self._invoke(
            "SubmitTask", request.command_id, request.type, request.capability,
            json.dumps(request.params or {}, ensure_ascii=False), context,
        )
        return self._result(response, request.command_id)

    def query(self, command_id: str) -> CommandResult | None:
        if not self.contract.supports_query:
            return None
        response = self._invoke("QueryTask", command_id)
        if response.get("state") == "not_found":
            return None
        return self._result(response, command_id)

    def hold(self, request: CommandRequest) -> CommandResult:
        if not self.contract.supports_hold:
            raise AdapterError("设备声明不支持保持")
        response = self._invoke("HoldTask", request.command_id, request.target_command_id)
        return self._result(response, request.command_id)

    def abort(self, request: CommandRequest) -> CommandResult:
        if not self.contract.supports_abort:
            raise AdapterError("设备声明不支持终止")
        response = self._invoke("AbortTask", request.command_id, request.target_command_id)
        return self._result(response, request.command_id)
