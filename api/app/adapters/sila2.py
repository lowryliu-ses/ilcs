"""SiLA 2 设备驱动（`sila2_v1`）。

设备（或厂商网关）实现 `contracts/sila2/TaskExecution.sila.xml`：按 ILCS 指令号提交、查询、保持、
终止任务，回执与 `http_json_v1` 同一份契约。和 http_json 一样，驱动只做协议转换，不做业务判断。

错误分类沿用系统的三分法：
- SiLA 定义错误（联锁、参数非法、设备忙、不支持）：设备明确拒绝、没有动作 → `AdapterError`；
- 连接失败、超时：结果未知 → `AdapterUnreachable`；
- 未定义执行错误、回执不合规：设备有响应但无法确认 → `AdapterIndeterminate`。

模拟设备在身份里报 `simulator: true`：正式环境拒绝接入，免得「真实驱动」背后其实是模拟器。
"""
from __future__ import annotations

import json
import socket
from pathlib import Path

from ..core.config import settings
from .base import (
    AdapterContract, AdapterError, AdapterIndeterminate, AdapterUnreachable, CommandRequest,
    CommandResult,
)
from .contract import parse_receipt

DRIVER = "sila2_v1"
FEATURE = "TaskExecution"
REJECTIONS = {"InvalidParameters", "Interlocked", "DeviceBusy", "NotSupported"}


class _Deadline:
    """给 sila2 客户端的 gRPC 调用加截止时间。库本身不暴露单次调用超时，卡死的设备会挂住执行器。"""

    def __init__(self, rpc, timeout: float):
        self._rpc = rpc
        self._timeout = timeout

    def with_call(self, message, metadata=None):
        return self._rpc.with_call(message, metadata=metadata, timeout=self._timeout)


class Sila2Adapter:
    def __init__(self, record):
        self.station_id = record.station_id
        self.config = dict(record.config or {})
        self.host = str(self.config.get("host") or "")
        try:
            self.port = int(self.config.get("port") or 0)
        except (TypeError, ValueError) as exc:
            raise AdapterError("sila2_v1 的 port 必须是整数") from exc
        if not self.host or not (0 < self.port < 65536):
            raise AdapterError("sila2_v1 必须配置 host 与 port")
        if self.host.lower() not in settings.adapter_allowed_host_set:
            raise AdapterError(f"SiLA 设备主机 {self.host} 不在 ILCS_ADAPTER_ALLOWED_HOSTS 白名单")
        self.insecure = bool(self.config.get("insecure", False))
        if self.insecure and settings.environment == "production":
            raise AdapterError("production 模式禁止不加密的 SiLA 2 连接")
        self.ca_file = str(self.config.get("ca_file") or "")
        self.connect_timeout = self._positive("connect_timeout_sec", 3.0)
        self.request_timeout = self._positive("request_timeout_sec", 10.0)
        self.expected_device_id = str(self.config.get("expected_device_id") or "")
        from .http_json import HttpJsonAdapter

        self.device_timezone = HttpJsonAdapter._timezone(str(self.config.get("device_timezone") or "UTC"))
        self._client = None
        self.contract = AdapterContract(
            kind="real", protocol=record.protocol or "SiLA 2", version=record.version or "1.0",
            supports_hold=bool(record.supports_hold), supports_abort=bool(record.supports_abort),
            supports_query=bool(record.supports_query), supports_dedup=bool(record.supports_dedup),
            note=record.note or "SiLA 2 TaskExecution 设备",
        )

    def _positive(self, key: str, default: float) -> float:
        try:
            value = float(self.config.get(key, default))
        except (TypeError, ValueError) as exc:
            raise AdapterError(f"{key} 必须是正数") from exc
        if value <= 0 or value > 3600:
            raise AdapterError(f"{key} 必须在 0–3600 秒之间")
        return value

    def _root_certs(self) -> bytes | None:
        if not self.ca_file:
            return None
        path = Path(self.ca_file).resolve()
        root = Path(settings.adapter_credential_root).resolve()
        if path != root and root not in path.parents:
            raise AdapterError("ca_file 必须位于 ILCS_ADAPTER_CREDENTIAL_ROOT 目录内")
        try:
            return path.read_bytes()
        except OSError as exc:
            raise AdapterError("ca_file 无法读取") from exc

    # ---------- 连接 ----------

    def _connect(self):
        if self._client is not None:
            return self._client
        # SilaClient 构造时会同步拉取特性清单；先用短超时探测端口，别让黑洞地址挂住执行器
        try:
            socket.create_connection((self.host, self.port), timeout=self.connect_timeout).close()
        except OSError as exc:
            raise AdapterUnreachable(f"SiLA 设备不可达：{exc.__class__.__name__}") from exc
        from sila2.client import SilaClient

        try:
            if self.insecure:
                client = SilaClient(self.host, self.port, insecure=True)
            else:
                client = SilaClient(self.host, self.port, root_certs=self._root_certs())
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterUnreachable(f"SiLA 连接失败：{exc.__class__.__name__}: {exc}") from exc
        if not hasattr(client, FEATURE):
            raise AdapterError(f"设备没有实现 {FEATURE} 特性，不能按 ILCS 任务契约驱动")
        self._client = client
        return client

    def _invoke(self, command: str, **parameters) -> dict:
        from sila2.client.utils import call_rpc_function
        from sila2.framework.errors.defined_execution_error import DefinedExecutionError
        from sila2.framework.errors.sila_connection_error import SilaConnectionError
        from sila2.framework.errors.undefined_execution_error import UndefinedExecutionError
        from sila2.framework.errors.validation_error import ValidationError

        client = self._connect()
        feature = getattr(client, FEATURE)
        wrapped = getattr(feature, command)._wrapped_command
        message = wrapped.parameters.to_message(**parameters, toplevel_named_data_node=wrapped.parameters)
        try:
            response = call_rpc_function(
                _Deadline(getattr(feature._grpc_stub, command), self.request_timeout),
                message, metadata=None, client=client, origin=wrapped,
            )
        except DefinedExecutionError as exc:
            identifier = getattr(exc, "identifier", "")
            if identifier in REJECTIONS:
                raise AdapterError(f"设备拒绝（{identifier}）：{exc.message}") from exc
            raise AdapterIndeterminate(f"设备返回未约定的错误 {identifier}") from exc
        except ValidationError as exc:
            raise AdapterError(f"设备判定参数非法：{exc}") from exc
        except SilaConnectionError as exc:
            self._client = None
            raise AdapterUnreachable(f"SiLA 连接中断：{exc}") from exc
        except UndefinedExecutionError as exc:
            raise AdapterIndeterminate(f"设备内部错误：{exc}") from exc
        except Exception as exc:  # gRPC 超时、通道异常
            self._client = None
            raise AdapterUnreachable(f"SiLA 调用无结论：{exc.__class__.__name__}") from exc
        try:
            payload = json.loads(wrapped.responses.to_native_type(response)[0])
        except (TypeError, ValueError, IndexError) as exc:
            raise AdapterIndeterminate("设备回执不是有效 JSON") from exc
        return payload

    # ---------- 契约 ----------

    def identity(self) -> dict:
        client = self._connect()
        try:
            raw = getattr(client, FEATURE).DeviceIdentity.get()
            return json.loads(raw)
        except AdapterError:
            raise
        except Exception as exc:
            self._client = None
            raise AdapterUnreachable(f"读取设备身份失败：{exc.__class__.__name__}") from exc

    def healthcheck(self) -> dict:
        identity = self.identity()
        if identity.get("simulator") and settings.environment == "production":
            raise AdapterError("该 SiLA 设备自报为模拟器；正式环境不接入模拟设备")
        actual = str(identity.get("device_id") or "")
        if self.expected_device_id and actual != self.expected_device_id:
            raise AdapterError(f"设备身份不匹配：期望 {self.expected_device_id}，实际 {actual or '缺失'}")
        return {
            "reachable": True, "driver": DRIVER, "protocol": self.contract.protocol,
            "device_id": actual, "model": identity.get("model", ""),
            "simulator": bool(identity.get("simulator")),
            "interlock": bool(identity.get("interlock")),
            "accepts_commands": bool(identity.get("accepts_commands", True)),
        }

    def _result(self, response: dict, command_id: str) -> CommandResult:
        return parse_receipt(response, command_id, f"real:{DRIVER}", self.device_timezone)

    @staticmethod
    def _context(request: CommandRequest) -> str:
        return json.dumps({
            "batch_id": request.batch_id, "step_index": request.step_index, "step_id": request.step_id,
            "target_command_id": request.target_command_id,
            **({"method": request.method} if request.method else {}),
        }, ensure_ascii=False)

    def submit(self, request: CommandRequest) -> CommandResult:
        response = self._invoke(
            "SubmitTask", CommandId=request.command_id, TaskType=request.type,
            Capability=request.capability,
            ParametersJson=json.dumps(request.params or {}, ensure_ascii=False),
            ContextJson=self._context(request),
        )
        return self._result(response, request.command_id)

    def query(self, command_id: str) -> CommandResult | None:
        if not self.contract.supports_query:
            return None
        response = self._invoke("QueryTask", CommandId=command_id)
        if response.get("state") == "not_found":
            return None
        return self._result(response, command_id)

    def hold(self, request: CommandRequest) -> CommandResult:
        if not self.contract.supports_hold:
            raise AdapterError("设备声明不支持保持")
        response = self._invoke(
            "HoldTask", CommandId=request.command_id, TargetCommandId=request.target_command_id,
        )
        return self._result(response, request.command_id)

    def abort(self, request: CommandRequest) -> CommandResult:
        if not self.contract.supports_abort:
            raise AdapterError("设备声明不支持终止")
        response = self._invoke(
            "AbortTask", CommandId=request.command_id, TargetCommandId=request.target_command_id,
        )
        return self._result(response, request.command_id)
