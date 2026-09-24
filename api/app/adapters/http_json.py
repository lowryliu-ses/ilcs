"""通用 HTTPS JSON 设备网关驱动。

它不是某台仪器协议的猜测，而是一份可部署的网关契约：厂商 SDK、PLC 或仪器私有
协议可由现场网关转换为这组 HTTP 端点。系统始终传递原 command_id，并要求响应回传
同一 ID、设备时间、质量与明确状态。
"""
from __future__ import annotations

from datetime import datetime, timezone
import http.client
import json
import os
from pathlib import Path
import re
import ssl
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlparse
from urllib.request import (
    HTTPDefaultErrorHandler, HTTPErrorProcessor, HTTPHandler, HTTPRedirectHandler, HTTPSHandler,
    OpenerDirector, Request, UnknownHandler,
)
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..core.config import settings
from .contract import parse_receipt
from .base import (
    AdapterContract, AdapterError, AdapterIndeterminate, AdapterUnreachable, CommandRequest,
    CommandResult,
)

DRIVER = "http_json_v1"
MAX_RESPONSE_BYTES = 1024 * 1024
# 这些状态码说明网关收到了请求但没有给出确定结论：重复投递冲突、超时、限流。
# 设备可能已经在动作，不能当成「明确拒绝」去走可重试分支。
INDETERMINATE_STATUS = {408, 409, 425, 429}


class _RefuseRedirect(HTTPRedirectHandler):
    """不跟随重定向：一次 30x 就能把带凭据的请求带出白名单，甚至从 HTTPS 降到 HTTP。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise AdapterError(f"设备网关返回重定向 HTTP {code}，驱动不跟随重定向")


def _split_timeouts(base: type, connect_timeout: float) -> type:
    """连接（含 TLS 握手）用连接超时，建立后换成请求超时。"""

    class Connection(base):
        def connect(self):
            read_timeout = self.timeout
            self.timeout = connect_timeout
            try:
                super().connect()
            finally:
                self.timeout = read_timeout
            self.sock.settimeout(read_timeout)

    return Connection


class _HTTPHandler(HTTPHandler):
    def __init__(self, connect_timeout: float):
        super().__init__()
        self._connection = _split_timeouts(http.client.HTTPConnection, connect_timeout)

    def http_open(self, req):
        return self.do_open(self._connection, req)


class _HTTPSHandler(HTTPSHandler):
    def __init__(self, context: ssl.SSLContext, connect_timeout: float):
        super().__init__(context=context)
        self._tls = context
        self._connection = _split_timeouts(http.client.HTTPSConnection, connect_timeout)

    def https_open(self, req):
        return self.do_open(self._connection, req, context=self._tls)


class HttpJsonAdapter:
    def __init__(self, record):
        self.station_id = record.station_id
        self.config = dict(record.config or {})
        self.base_url = str(self.config.get("base_url") or "").rstrip("/")
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise AdapterError("http_json_v1 必须配置有效的 base_url")
        if (parsed.hostname or "").lower() not in settings.adapter_allowed_host_set:
            raise AdapterError(
                f"设备网关主机 {parsed.hostname or '缺失'} 不在 ILCS_ADAPTER_ALLOWED_HOSTS 白名单"
            )
        if parsed.scheme != "https" and (
            settings.environment == "production"
            or not self.config.get("allow_insecure_http", False)
        ):
            raise AdapterError("真实 HTTP 适配器必须使用 HTTPS；本地联调需显式允许不安全 HTTP")
        self.verify_tls = bool(self.config.get("verify_tls", True))
        if settings.environment == "production" and not self.verify_tls:
            raise AdapterError("production 模式禁止关闭设备网关 TLS 校验")
        self.connect_timeout = self._positive_timeout("connect_timeout_sec", 3.0)
        self.request_timeout = self._positive_timeout("request_timeout_sec", 10.0)
        self.device_timezone = self._timezone(str(self.config.get("device_timezone") or "UTC"))
        self.tls_context = self._tls_context()
        # 手工装配处理链：不带 ProxyHandler（环境变量里的代理会把设备流量绕到白名单之外），
        # 重定向一律拒绝
        self.opener = OpenerDirector()
        for handler in (
            UnknownHandler(),
            _HTTPHandler(self.connect_timeout),
            _HTTPSHandler(self.tls_context, self.connect_timeout),
            HTTPDefaultErrorHandler(),
            _RefuseRedirect(),
            HTTPErrorProcessor(),
        ):
            self.opener.add_handler(handler)
        self.paths = {
            "health": "/health",
            "submit": "/commands",
            "query": "/commands/{command_id}",
            "hold": "/commands/{command_id}/hold",
            "abort": "/commands/{command_id}/abort",
            **(self.config.get("paths") or {}),
        }
        for name, path in self.paths.items():
            if not isinstance(path, str) or not path.startswith("/") or "://" in path:
                raise AdapterError(f"paths.{name} 必须是同一网关下以 / 开头的相对路径")
        self.credential_ref = record.credential_ref or ""
        self.contract = AdapterContract(
            kind="real",
            protocol=record.protocol or "HTTPS JSON",
            version=record.version or "1.0",
            supports_hold=bool(record.supports_hold),
            supports_abort=bool(record.supports_abort),
            supports_query=bool(record.supports_query),
            supports_dedup=bool(record.supports_dedup),
            note=record.note or "通用 HTTPS JSON 设备网关",
        )

    @staticmethod
    def _timezone(name: str):
        if name.upper() == "UTC":
            return timezone.utc
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise AdapterError(f"device_timezone {name} 不是有效的 IANA 时区") from exc

    def _tls_context(self) -> ssl.SSLContext:
        if not self.verify_tls:
            return ssl._create_unverified_context()
        ca_file = str(self.config.get("ca_file") or "")
        if not ca_file:
            return ssl.create_default_context()
        path = Path(ca_file).resolve()
        root = Path(settings.adapter_credential_root).resolve()
        if path != root and root not in path.parents:
            raise AdapterError("ca_file 必须位于 ILCS_ADAPTER_CREDENTIAL_ROOT 目录内")
        try:
            return ssl.create_default_context(cafile=str(path))
        except (OSError, ssl.SSLError) as exc:
            raise AdapterError("ca_file 无法加载为 CA 证书") from exc

    def _positive_timeout(self, key: str, default: float) -> float:
        try:
            value = float(self.config.get(key, default))
        except (TypeError, ValueError) as exc:
            raise AdapterError(f"{key} 必须是正数") from exc
        if value <= 0 or value > 3600:
            raise AdapterError(f"{key} 必须在 0–3600 秒之间")
        return value

    def _credential_headers(self) -> dict[str, str]:
        if not self.credential_ref:
            return {}
        if self.credential_ref.startswith("env://"):
            name = self.credential_ref[len("env://"):]
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise AdapterError("env:// 凭据引用格式无效")
            raw = os.environ.get(name)
            if raw is None:
                raise AdapterError(f"凭据环境变量 {name} 未配置")
        elif self.credential_ref.startswith("file://"):
            path = Path(urlparse(self.credential_ref).path).resolve()
            root = Path(settings.adapter_credential_root).resolve()
            if path != root and root not in path.parents:
                raise AdapterError("凭据文件必须位于 ILCS_ADAPTER_CREDENTIAL_ROOT 目录内")
            try:
                if path.stat().st_size > 64 * 1024:
                    raise AdapterError("凭据文件超过 64 KiB")
                raw = path.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise AdapterError("凭据文件不可读取") from exc
        elif self.credential_ref.startswith("vault://"):
            raise AdapterError("当前部署未配置 Vault 解析器，请改用已挂载的 file:// 或 env:// 引用")
        else:
            raise AdapterError("credential_ref 仅支持 env://、file:// 或已配置的 vault://")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict) and isinstance(parsed.get("headers"), dict):
            headers = {str(key): str(value) for key, value in parsed["headers"].items()}
            if not headers or any("\n" in key + value or "\r" in key + value for key, value in headers.items()):
                raise AdapterError("凭据 headers 格式无效")
            return headers
        return {"Authorization": f"Bearer {raw}"}

    def _url(self, path: str, command_id: str = "") -> str:
        rendered = path.replace("{command_id}", quote(command_id, safe=""))
        return urljoin(f"{self.base_url}/", rendered.lstrip("/"))

    def _call(
        self, method: str, path: str, payload: dict | None = None, *, command_id: str = "",
        allow_not_found: bool = False, idempotency_key: str = "",
    ) -> dict | None:
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
        headers = {"Accept": "application/json", **self._credential_headers()}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if idempotency_key:
            header = str(self.config.get("idempotency_header") or "Idempotency-Key")
            if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", header):
                raise AdapterError("idempotency_header 格式无效")
            headers[header] = idempotency_key
        request = Request(self._url(path, command_id), data=body, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=self.request_timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise AdapterIndeterminate("设备网关响应超过 1 MiB")
        except HTTPError as exc:
            if allow_not_found and exc.code == 404:
                return None
            message = f"设备网关 HTTP {exc.code}"
            if 300 <= exc.code < 400:
                raise AdapterError(f"{message}：驱动不跟随重定向") from exc
            if exc.code >= 500:
                raise AdapterUnreachable(message) from exc
            if exc.code in INDETERMINATE_STATUS:
                raise AdapterIndeterminate(f"{message}：网关未给出确定结论") from exc
            raise AdapterError(message) from exc
        except AdapterError:
            raise
        except (TimeoutError, URLError, OSError, http.client.HTTPException) as exc:
            raise AdapterUnreachable(f"设备网关不可达：{exc.__class__.__name__}") from exc
        # 2xx 之后的任何解读失败都不能证明设备没动：请求已被网关接收
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AdapterIndeterminate("设备网关返回的不是有效 JSON") from exc
        if not isinstance(value, dict):
            raise AdapterIndeterminate("设备网关响应必须是 JSON 对象")
        return value

    def healthcheck(self) -> dict:
        response = self._call("GET", self.paths["health"])
        if response.get("reachable") is False:
            raise AdapterError("设备网关报告设备不可达")
        if response.get("simulator") and settings.environment == "production":
            raise AdapterError("该设备网关自报为模拟器；正式环境不接入模拟设备")
        expected = str(self.config.get("expected_device_id") or "")
        field = str(self.config.get("device_id_field") or "device_id")
        actual = str(response.get(field) or "")
        if expected and actual != expected:
            raise AdapterError(f"设备身份不匹配：期望 {expected}，实际 {actual or '缺失'}")
        return {
            "reachable": True,
            "driver": DRIVER,
            "protocol": self.contract.protocol,
            "device_id": actual,
            "gateway_version": response.get("version", ""),
            # 网关回报了才同步；没回报的按「无联锁、接受指令」，与推送心跳模式的缺省一致
            "simulator": bool(response.get("simulator")),
            "interlock": bool(response.get("interlock")),
            "accepts_commands": bool(response.get("accepts_commands", True)),
        }

    @staticmethod
    def _payload(request: CommandRequest) -> dict:
        return {
            "command_id": request.command_id,
            "station_id": request.station_id,
            "capability": request.capability,
            "params": request.params,
            "type": request.type,
            "batch_id": request.batch_id,
            "step_index": request.step_index,
            "step_id": request.step_id,
            "target_command_id": request.target_command_id,
        }

    def _result(self, response: dict, command_id: str) -> CommandResult:
        return parse_receipt(response, command_id, f"real:{DRIVER}", self.device_timezone)

    def submit(self, request: CommandRequest) -> CommandResult:
        response = self._call(
            "POST", self.paths["submit"], self._payload(request),
            command_id=request.command_id, idempotency_key=request.command_id,
        )
        return self._result(response, request.command_id)

    def query(self, command_id: str) -> CommandResult | None:
        if not self.contract.supports_query:
            return None
        response = self._call(
            "GET", self.paths["query"], command_id=command_id, allow_not_found=True
        )
        return None if response is None else self._result(response, command_id)

    def hold(self, request: CommandRequest) -> CommandResult:
        if not self.contract.supports_hold:
            raise AdapterError("设备声明不支持保持")
        response = self._call(
            "POST", self.paths["hold"], self._payload(request),
            command_id=request.command_id, idempotency_key=request.command_id,
        )
        return self._result(response, request.command_id)

    def abort(self, request: CommandRequest) -> CommandResult:
        if not self.contract.supports_abort:
            raise AdapterError("设备声明不支持终止")
        response = self._call(
            "POST", self.paths["abort"], self._payload(request),
            command_id=request.command_id, idempotency_key=request.command_id,
        )
        return self._result(response, request.command_id)


def record_for_test(**overrides):
    """仅供无数据库驱动测试构造配置，不进入业务运行路径。"""
    values = {
        "station_id": "ST-HTTP-TEST", "config": {}, "credential_ref": "",
        "protocol": "HTTPS JSON", "version": "1.0", "supports_hold": True,
        "supports_abort": True, "supports_query": True, "supports_dedup": True,
        "note": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)
