"""设备侧 HTTP 通道：`http_json_v1`（ILCS 网关契约）与 `rest_map_v1`（设备自有 REST 接口）共用。

- 主机必须在 `ILCS_ADAPTER_ALLOWED_HOSTS` 白名单；正式环境只允许 HTTPS 且必须校验证书；
- 不读 `HTTP(S)_PROXY`（代理会把设备流量绕到白名单之外），不跟随重定向（30x 可以把带凭据的请求
  带出白名单或降级到 HTTP）；连接（含 TLS 握手）与读写分开计时；
- 凭据只从 `credential_ref`（env:// 或 ILCS_ADAPTER_CREDENTIAL_ROOT 下的 file://）读：纯 token 当
  Bearer，或 `{"headers": {...}}` 原样作为请求头。

响应分类：2xx 读 JSON；404 可按调用方要求当「没有」；30x 与其他 4xx 是明确拒绝；408 / 409 / 425 / 429
是「收到了但没给结论」；5xx、连接失败、超时是结果未知。
"""
from __future__ import annotations

from datetime import timezone
import http.client
import json
import os
from pathlib import Path
import re
import ssl
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlparse
from urllib.request import (
    HTTPDefaultErrorHandler, HTTPErrorProcessor, HTTPHandler, HTTPRedirectHandler, HTTPSHandler,
    OpenerDirector, Request, UnknownHandler,
)
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..core.config import settings
from .base import AdapterError, AdapterIndeterminate, AdapterUnreachable

MAX_RESPONSE_BYTES = 1024 * 1024
# 这些状态码说明对端收到了请求但没有给出确定结论：重复投递冲突、超时、限流。
# 设备可能已经在动作，不能当成「明确拒绝」去走可重试分支。
INDETERMINATE_STATUS = {408, 409, 425, 429}


class _RefuseRedirect(HTTPRedirectHandler):
    """不跟随重定向：一次 30x 就能把带凭据的请求带出白名单，甚至从 HTTPS 降到 HTTP。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise AdapterError(f"设备接口返回重定向 HTTP {code}，驱动不跟随重定向")


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


def timezone_of(name: str):
    if name.upper() == "UTC":
        return timezone.utc
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise AdapterError(f"device_timezone {name} 不是有效的 IANA 时区") from exc


def _within_credential_root(path: Path, label: str) -> Path:
    path = path.resolve()
    root = Path(settings.adapter_credential_root).resolve()
    if path != root and root not in path.parents:
        raise AdapterError(f"{label}必须位于 ILCS_ADAPTER_CREDENTIAL_ROOT 目录内")
    return path


def _origin(url: str) -> tuple:
    parsed = urlparse(url)
    try:
        port = parsed.port or {"http": 80, "https": 443}.get(parsed.scheme)
    except ValueError:
        port = None
    return parsed.scheme, (parsed.hostname or "").lower(), port, parsed.username, parsed.password


class HttpTransport:
    def __init__(self, config: dict, credential_ref: str, *, driver: str, label: str = "设备网关"):
        self.config = config
        self.label = label
        self.base_url = str(config.get("base_url") or "").rstrip("/")
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise AdapterError(f"{driver} 必须配置有效的 base_url")
        if not settings.adapter_host_allowed(parsed.hostname or ""):
            raise AdapterError(f"{label}主机 {parsed.hostname or '缺失'} 不在 ILCS_ADAPTER_ALLOWED_HOSTS 白名单")
        if parsed.scheme != "https" and (
            settings.environment == "production" or not config.get("allow_insecure_http", False)
        ):
            raise AdapterError("真实 HTTP 适配器必须使用 HTTPS；本地联调需显式允许不安全 HTTP")
        self.verify_tls = bool(config.get("verify_tls", True))
        if settings.environment == "production" and not self.verify_tls:
            raise AdapterError(f"production 模式禁止关闭{label} TLS 校验")
        self.connect_timeout = self._positive("connect_timeout_sec", 3.0)
        self.request_timeout = self._positive("request_timeout_sec", 10.0)
        self.tls_context = self._tls_context()
        # 手工装配处理链：不带 ProxyHandler，重定向一律拒绝
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
        self.credential_ref = credential_ref or ""
        self.extra_headers = {str(k): str(v) for k, v in (config.get("headers") or {}).items()}
        for key, value in self.extra_headers.items():
            if "\n" in key + value or "\r" in key + value:
                raise AdapterError("headers 不能含换行")

    def _positive(self, key: str, default: float) -> float:
        try:
            value = float(self.config.get(key, default))
        except (TypeError, ValueError) as exc:
            raise AdapterError(f"{key} 必须是正数") from exc
        if value <= 0 or value > 3600:
            raise AdapterError(f"{key} 必须在 0–3600 秒之间")
        return value

    def _tls_context(self) -> ssl.SSLContext:
        if not self.verify_tls:
            return ssl._create_unverified_context()
        ca_file = str(self.config.get("ca_file") or "")
        if not ca_file:
            return ssl.create_default_context()
        path = _within_credential_root(Path(ca_file), "ca_file ")
        try:
            return ssl.create_default_context(cafile=str(path))
        except (OSError, ssl.SSLError) as exc:
            raise AdapterError("ca_file 无法加载为 CA 证书") from exc

    def credential_headers(self) -> dict[str, str]:
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
            path = _within_credential_root(Path(urlparse(self.credential_ref).path), "凭据文件")
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

    def url(self, path: str, fields: dict | None = None) -> str:
        """base_url 下的地址。拼出来的地址必须仍是 base_url 那台主机（同协议、同主机、同端口）：
        映射配置里写成完整网址（`https://别的主机/…`）的路径会把凭据带去白名单之外，一律拒绝。"""
        rendered = path
        for name, value in (fields or {}).items():
            rendered = rendered.replace("{" + name + "}", quote(str(value), safe=""))
        joined = urljoin(f"{self.base_url}/", rendered.lstrip("/"))
        if _origin(joined) != _origin(self.base_url):
            raise AdapterError(f"{self.label}请求路径 {path} 指向了 base_url 之外的主机：请求与凭据只发往 base_url")
        return joined

    def request(
        self, method: str, path: str, payload=None, *, fields: dict | None = None, allow_not_found: bool = False,
        headers: dict | None = None, allow_empty: bool = False, detail: bool = False,
    ):
        """返回解析后的 JSON（对象或数组）；404 且 allow_not_found → None；空响应且 allow_empty → None。"""
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
        merged = {"Accept": "application/json", **self.extra_headers, **self.credential_headers(), **(headers or {})}
        if body is not None:
            merged["Content-Type"] = "application/json"
        request = Request(self.url(path, fields), data=body, headers=merged, method=method)
        try:
            with self.opener.open(request, timeout=self.request_timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise AdapterIndeterminate(f"{self.label}响应超过 1 MiB")
        except HTTPError as exc:
            if allow_not_found and exc.code == 404:
                return None
            message = f"{self.label} HTTP {exc.code}"
            if detail:
                try:
                    text = exc.read(512).decode("utf-8", errors="replace").strip()
                except Exception:
                    text = ""
                if text:
                    message = f"{message}：{text[:200]}"
            if 300 <= exc.code < 400:
                raise AdapterError(f"{message}：驱动不跟随重定向") from exc
            if exc.code >= 500:
                raise AdapterUnreachable(message) from exc
            if exc.code in INDETERMINATE_STATUS:
                raise AdapterIndeterminate(f"{message}：对端未给出确定结论") from exc
            raise AdapterError(message) from exc
        except AdapterError:
            raise
        except (TimeoutError, URLError, OSError, http.client.HTTPException) as exc:
            raise AdapterUnreachable(f"{self.label}不可达：{exc.__class__.__name__}") from exc
        # 2xx 之后的任何解读失败都不能证明设备没动：请求已被对端接收
        if not raw.strip() and allow_empty:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AdapterIndeterminate(f"{self.label}返回的不是有效 JSON") from exc
