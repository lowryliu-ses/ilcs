"""SiLA 2 设备驱动（`sila2_v1`）。

契约见 `devices/contracts/sila2/README.md`。设备服务（驱动宿主、厂商网关或模拟设备）实现其中一部分特性，驱动按
`SiLAService` 报的已实现特性取能力，不再连上就要求 TaskExecution：
- `DeviceInfo`：身份、状态、驱动与配置摘要；没有它的老服务器（只有 TaskExecution 1.0）读 `TaskExecution.DeviceIdentity`；
- `PointAccess`：点位读写，与映射驱动的点位层同一套接口（点位服务、手动写都不用区分驱动）；
- `TaskExecution`：按 ILCS 指令号提交、查询、保持、终止，回执与 `http_json_v1` 同一份契约。
  只读写点位的设备在适配器配置里写 `"tasks": false`，下发一律明确拒绝。
- 设备服务实现了 `AuthorizationService` 就每次调用带令牌（`credential_ref` 指向的 env:// 或凭据目录内的 file://）。

错误分类沿用系统的三分法：
- SiLA 定义错误（联锁、参数非法、设备忙、不支持、动作前就连不上设备）：设备明确没有动作 → `AdapterError`。报错文字
  以按错误标识定下的类别词开头（联锁 → 安全异常，无响应 → 通信异常），故障分类不靠设备给的文字；
- 连接失败、超时：结果未知 → `AdapterUnreachable`；查询时设备服务报 `DeviceUnreachable`（现在问不到）同样按它处理；
- 未定义执行错误、回执不合规：设备有响应但无法确认 → `AdapterIndeterminate`。

模拟设备在身份里报 `simulator: true`：正式环境拒绝接入，免得「真实驱动」背后其实是模拟器。
"""
from __future__ import annotations

import json
import math
import os
import socket
import threading
from pathlib import Path
from urllib.parse import urlparse
import uuid

from ...core.config import settings
from ..base import (
    AdapterContract, AdapterError, AdapterIndeterminate, AdapterUnreachable, CommandRequest,
    CommandResult,
)
from ..contract import parse_receipt

DRIVER = "sila2_v1"
TASKS, INFO, POINTS, AUTH = "TaskExecution", "DeviceInfo", "PointAccess", "AuthorizationService"
FEATURE = TASKS
# 明确拒绝时报错文字的开头：类别词由错误标识决定（domain/exceptions.classify 按文字归类）
REFUSALS = {
    "InvalidParameters": "设备拒绝（InvalidParameters）",
    "Interlocked": "设备联锁未解除（Interlocked）",
    "DeviceBusy": "设备忙（DeviceBusy）",
    "NotSupported": "设备不支持（NotSupported）",
    "DeviceUnreachable": "设备无响应（DeviceUnreachable），没有下发",
    "InvalidAccessToken": "设备服务拒绝了令牌（InvalidAccessToken），没有下发：核对 credential_ref",
}
REJECTIONS = set(REFUSALS) - {"DeviceUnreachable", "InvalidAccessToken"}
# 写点位时这些定义错误都表示没有写入；WriteUnconfirmed 表示写出去了却没拿到结论
WRITE_REFUSALS = {
    "UnknownPoint", "NotWritable", "ControlPoint", "OutOfRange", "InvalidValue", "DeviceBusy", "WriteRejected",
    "DeviceUnreachable", "RequestConflict", "InvalidAccessToken",
}
SILA_TYPE = '<DataType xmlns="http://www.sila-standard.org"><Basic>{}</Basic></DataType>'
# sila2 0.14.0 建客户端时现场编译 protobuf，生成的模块先放进 sys.modules 再删掉，不是线程安全的：执行器并发探测时
# 两个线程同时建客户端会偶发 KeyError（如 'SiLAService_pb2'），被当成连不上、报一条失联。建客户端一律串行
_CLIENT_LOCK = threading.Lock()


class _Deadline:
    """给 sila2 客户端的 gRPC 调用加截止时间。库本身不暴露单次调用超时，卡死的设备会挂住执行器。"""

    def __init__(self, rpc, timeout: float):
        self._rpc = rpc
        self._timeout = timeout

    def with_call(self, message, metadata=None):
        return self._rpc.with_call(message, metadata=metadata, timeout=self._timeout)


def _point_value(value):
    """手动写的值 → PointAccess 的 PointValue（限定类型的 Any）。类型 XML 要带 SiLA 命名空间。"""
    from sila2.framework.data_types.any import SilaAnyType

    if isinstance(value, bool):
        kind = "Boolean"
    elif isinstance(value, int):
        kind = "Integer"
    elif isinstance(value, float):
        kind = "Real"
    else:
        kind, value = "String", str(value)
    return SilaAnyType(SILA_TYPE.format(kind), value)


def _plain(value):
    """读回来的 Any → 能放进 JSON 的值。"""
    raw = getattr(value, "value", value)
    if isinstance(raw, float) and not math.isfinite(raw):
        return str(raw)
    return raw


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
        if not settings.adapter_host_allowed(self.host):
            raise AdapterError(f"SiLA 设备主机 {self.host} 不在 ILCS_ADAPTER_ALLOWED_HOSTS 白名单")
        self.insecure = bool(self.config.get("insecure", False))
        if self.insecure and settings.environment == "production":
            raise AdapterError("production 模式禁止不加密的 SiLA 2 连接")
        self.ca_file = str(self.config.get("ca_file") or "")
        self.connect_timeout = self._positive("connect_timeout_sec", 3.0)
        self.request_timeout = self._positive("request_timeout_sec", 10.0)
        self.expected_device_id = str(self.config.get("expected_device_id") or "")
        tasks = self.config.get("tasks", True)
        if not isinstance(tasks, bool):
            raise AdapterError("tasks 只能是 true / false（false：只读写点位，不参与自动流程）")
        self.tasks_enabled = tasks
        self.credential_ref = str(getattr(record, "credential_ref", "") or "")
        from .http_json import HttpJsonAdapter

        self.device_timezone = HttpJsonAdapter._timezone(str(self.config.get("device_timezone") or "UTC"))
        self._client = None
        self._features: frozenset[str] = frozenset()
        self._metadata = None
        self.contract = AdapterContract(
            kind="real", protocol=record.protocol or "SiLA 2", version=record.version or "1.0",
            supports_hold=bool(record.supports_hold), supports_abort=bool(record.supports_abort),
            supports_query=bool(record.supports_query), supports_dedup=bool(record.supports_dedup),
            note=record.note or "SiLA 2 设备服务",
        )

    def _positive(self, key: str, default: float) -> float:
        try:
            value = float(self.config.get(key, default))
        except (TypeError, ValueError) as exc:
            raise AdapterError(f"{key} 必须是正数") from exc
        if value <= 0 or value > 3600:
            raise AdapterError(f"{key} 必须在 0–3600 秒之间")
        return value

    def _within_root(self, path: Path, label: str) -> Path:
        path = path.resolve()
        root = Path(settings.adapter_credential_root).resolve()
        if path != root and root not in path.parents:
            raise AdapterError(f"{label}必须位于 ILCS_ADAPTER_CREDENTIAL_ROOT 目录内")
        return path

    def _root_certs(self) -> bytes | None:
        if not self.ca_file:
            return None
        try:
            return self._within_root(Path(self.ca_file), "ca_file ").read_bytes()
        except OSError as exc:
            raise AdapterError("ca_file 无法读取") from exc

    def _token(self) -> str:
        """设备服务要求令牌（实现了 AuthorizationService）：从 credential_ref 读，原文不进配置。"""
        ref = self.credential_ref
        if ref.startswith("env://"):
            token = os.environ.get(ref[len("env://"):], "")
        elif ref.startswith("file://"):
            path = self._within_root(Path(urlparse(ref).path), "令牌文件")
            try:
                if path.stat().st_size > 64 * 1024:
                    raise AdapterError("令牌文件超过 64 KiB")
                token = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise AdapterError("令牌文件不可读取") from exc
        else:
            raise AdapterError("设备服务要求令牌：credential_ref 写 env://<变量名> 或凭据目录内的 file://<令牌文件>")
        token = token.strip()
        if not token:
            raise AdapterError("令牌为空：核对 credential_ref")
        return token

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
            with _CLIENT_LOCK:
                if self.insecure:
                    client = SilaClient(self.host, self.port, insecure=True)
                else:
                    client = SilaClient(self.host, self.port, root_certs=self._root_certs())
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterUnreachable(f"SiLA 连接失败：{exc.__class__.__name__}: {exc}") from exc
        features = frozenset(name for name in (TASKS, INFO, POINTS, AUTH) if hasattr(client, name))
        if not features & {TASKS, INFO}:
            raise AdapterError("设备服务没有实现 ILCS 的 DeviceInfo 或 TaskExecution 特性，不能按 ILCS 契约接入")
        metadata = [client.AuthorizationService.AccessToken(self._token())] if AUTH in features else None
        self._client, self._features, self._metadata = client, features, metadata
        return client

    def _has(self, feature: str) -> bool:
        self._connect()
        return feature in self._features

    def _call(self, feature: str, name: str, *, prop: bool = False, **parameters):
        """调一次命令或读一次属性：带截止时间与令牌。定义错误原样抛给调用方按场合翻译，其余按三分法。"""
        from sila2.client.utils import call_rpc_function
        from sila2.framework.errors.defined_execution_error import DefinedExecutionError
        from sila2.framework.errors.framework_error import FrameworkError
        from sila2.framework.errors.sila_connection_error import SilaConnectionError
        from sila2.framework.errors.undefined_execution_error import UndefinedExecutionError
        from sila2.framework.errors.validation_error import ValidationError

        client = self._connect()
        feature_client = getattr(client, feature)
        if prop:
            wrapped = getattr(feature_client, name)._wrapped_property
            rpc = getattr(feature_client._grpc_stub, f"Get_{name}")
            message = wrapped.get_parameters_message()
        else:
            wrapped = getattr(feature_client, name)._wrapped_command
            rpc = getattr(feature_client._grpc_stub, name)
            message = wrapped.parameters.to_message(**parameters, toplevel_named_data_node=wrapped.parameters)
        try:
            response = call_rpc_function(
                _Deadline(rpc, self.request_timeout), message, metadata=self._metadata, client=client, origin=wrapped,
            )
        except DefinedExecutionError:
            raise
        except ValidationError as exc:
            raise AdapterError(f"设备判定参数非法：{exc}") from exc
        except FrameworkError as exc:  # 元数据不对、调用不被接受：SiLA 框架在执行之前就拒绝了
            raise AdapterError(f"设备服务拒绝了这次调用（{exc.__class__.__name__}）：{exc}") from exc
        except SilaConnectionError as exc:
            self._client = None
            raise AdapterUnreachable(f"SiLA 连接中断：{exc}") from exc
        except UndefinedExecutionError as exc:
            raise AdapterIndeterminate(f"设备内部错误：{exc}") from exc
        except Exception as exc:  # gRPC 超时、通道异常
            self._client = None
            raise AdapterUnreachable(f"SiLA 调用无结论：{exc.__class__.__name__}") from exc
        return wrapped.to_native_type(response) if prop else wrapped.responses.to_native_type(response)

    def _read(self, feature: str, name: str):
        """读属性。设备服务报 DeviceUnreachable（宿主在、设备没回话）按离线处理。"""
        from sila2.framework.errors.defined_execution_error import DefinedExecutionError

        try:
            return self._call(feature, name, prop=True)
        except DefinedExecutionError as exc:
            if exc.identifier == "DeviceUnreachable":
                raise AdapterUnreachable(f"设备无响应：{exc.message}") from exc
            if exc.identifier == "InvalidAccessToken":
                raise AdapterError(REFUSALS["InvalidAccessToken"]) from exc
            raise AdapterIndeterminate(f"读 {feature}.{name} 返回未约定的错误 {exc.identifier}") from exc

    def _task(self, command: str, **parameters) -> dict:
        from sila2.framework.errors.defined_execution_error import DefinedExecutionError

        if not self._has(TASKS):
            raise AdapterError("设备服务没有实现 TaskExecution：这台设备只读写点位，不参与自动流程；设备没有动作")
        try:
            native = self._call(TASKS, command, **parameters)
        except DefinedExecutionError as exc:
            identifier = exc.identifier
            if command == "QueryTask" and identifier == "DeviceUnreachable":
                raise AdapterUnreachable(f"设备暂时问不到：{exc.message}") from exc
            if identifier in REFUSALS:
                raise AdapterError(f"{REFUSALS[identifier]}：{exc.message}") from exc
            raise AdapterIndeterminate(f"设备返回未约定的错误 {identifier}") from exc
        try:
            return json.loads(native[0])
        except (TypeError, ValueError, IndexError) as exc:
            raise AdapterIndeterminate("设备回执不是有效 JSON") from exc

    # ---------- 身份与健康 ----------

    def _status_identity(self) -> dict:
        """探测在线用的身份：DeviceInfo 的身份、状态、驱动信息；老服务器读 TaskExecution.DeviceIdentity。"""
        if not self._has(INFO):
            try:
                return json.loads(self._read(TASKS, "DeviceIdentity"))
            except (TypeError, ValueError) as exc:
                raise AdapterIndeterminate("设备身份不是有效 JSON") from exc
        physical, status, driver = self._read(INFO, "Identity"), self._read(INFO, "Status"), self._read(INFO, "Driver")
        return {
            "device_id": physical.DeviceId, "identity_source": physical.IdentitySource, "vendor": physical.Vendor,
            "model": physical.Model, "serial": physical.SerialNumber, "firmware": physical.Firmware,
            "simulator": physical.Simulator, "interlock": status.Interlock, "accepts_commands": status.AcceptsCommands,
            "state": status.State, "active_command_id": status.ActiveCommandId,
            "driver": {
                "plugin": driver.Plugin, "plugin_version": driver.PluginVersion, "host_version": driver.HostVersion,
                "config_version": driver.ConfigVersion, "config_digest": driver.ConfigDigest,
                "offline_after_sec": driver.OfflineAfterSeconds,
            },
        }

    def identity(self) -> dict:
        """设备自报的身份与方法目录（读取设备自报信息、接入验收用）。方法目录取自 TaskSupport 的设备端程序。"""
        identity = self._status_identity()
        identity["features"] = sorted(self._features)
        if self._has(TASKS) and hasattr(getattr(self._client, TASKS), "TaskSupport"):
            support = self._read(TASKS, "TaskSupport")
            identity["methods"] = [
                {"program": program.Program, "name": program.Name,
                 "capability": "" if row.Capability == "*" else row.Capability}
                for row in support.Capabilities for program in row.Programs
            ]
            identity["methods_source"] = "device"
            identity["task_support"] = {
                "capabilities": [row.Capability for row in support.Capabilities], "hold": support.SupportsHold,
                "abort": support.SupportsAbort, "query": support.SupportsQuery, "dedup": support.SupportsDedup,
                "handoff": support.Handoff,
            }
        return identity

    def healthcheck(self) -> dict:
        identity = self._status_identity()
        if identity.get("simulator") and settings.environment == "production":
            raise AdapterError("该 SiLA 设备自报为模拟器；正式环境不接入模拟设备")
        actual = str(identity.get("device_id") or "")
        if self.expected_device_id and actual != self.expected_device_id:
            raise AdapterError(f"设备身份不匹配：期望 {self.expected_device_id}，实际 {actual or '缺失'}")
        if self.tasks_enabled and TASKS not in self._features:
            raise AdapterError("配置说这台设备参与自动流程，设备服务却没有实现 TaskExecution："
                               "只读写点位的设备在配置里写 \"tasks\": false")
        return {
            "reachable": True, "driver": DRIVER, "protocol": self.contract.protocol,
            "device_id": actual, "model": identity.get("model", ""),
            "simulator": bool(identity.get("simulator")),
            "interlock": bool(identity.get("interlock")),
            "accepts_commands": bool(identity.get("accepts_commands", True)),
            "driver_info": identity.get("driver") or {},
        }

    # ---------- 任务 ----------

    def _result(self, response: dict, command_id: str) -> CommandResult:
        return parse_receipt(response, command_id, f"real:{DRIVER}", self.device_timezone)

    @staticmethod
    def _context(request: CommandRequest) -> str:
        """TaskExecution 1.1 的上下文，与 `http_json_v1` 请求体带的一致：工位、设备方法，投料步骤再带物料
        （称量加料这类设备据此核对装的料、按这个名字回报实际消耗）。"""
        return json.dumps({
            "batch_id": request.batch_id, "step_index": request.step_index, "step_id": request.step_id,
            "target_command_id": request.target_command_id, "station_id": request.station_id,
            **({"method": request.method} if request.method else {}),
            **({"material": dict(request.material)} if request.material else {}),
        }, ensure_ascii=False)

    def submit(self, request: CommandRequest) -> CommandResult:
        if not self.tasks_enabled:
            raise AdapterError("这台设备只读写点位（tasks: false），不参与自动流程：设备没有动作")
        response = self._task(
            "SubmitTask", CommandId=request.command_id, TaskType=request.type,
            Capability=request.capability,
            ParametersJson=json.dumps(request.params or {}, ensure_ascii=False),
            ContextJson=self._context(request),
        )
        return self._result(response, request.command_id)

    def query(self, command_id: str) -> CommandResult | None:
        if not self.contract.supports_query or not self._has(TASKS):
            return None
        response = self._task("QueryTask", CommandId=command_id)
        if response.get("state") == "not_found":
            return None
        return self._result(response, command_id)

    def hold(self, request: CommandRequest) -> CommandResult:
        if not self.contract.supports_hold:
            raise AdapterError("设备声明不支持保持")
        response = self._task(
            "HoldTask", CommandId=request.command_id, TargetCommandId=request.target_command_id,
        )
        return self._result(response, request.command_id)

    def abort(self, request: CommandRequest) -> CommandResult:
        if not self.contract.supports_abort:
            raise AdapterError("设备声明不支持终止")
        response = self._task(
            "AbortTask", CommandId=request.command_id, TargetCommandId=request.target_command_id,
        )
        return self._result(response, request.command_id)

    # ---------- 点位（与映射驱动的点位层同一套接口）----------

    @property
    def tasks(self) -> bool:
        return self.tasks_enabled

    @property
    def status(self) -> dict:
        """点位服务写之前判忙看它：有 DeviceInfo 就能读设备状态（device_state）。"""
        return {"point": "DeviceInfo.Status"} if self.tasks_enabled else {}

    def device_state(self) -> str:
        if not self._has(INFO):
            return "idle"
        return str(self._read(INFO, "Status").State)

    def point_catalog(self) -> list[dict]:
        if not self._has(POINTS):
            return []
        return [{
            "name": row.Name, "label": row.Label, "unit": row.Unit, "writable": bool(row.Writable),
            "min": row.Minimum[0] if row.Minimum else None, "max": row.Maximum[0] if row.Maximum else None,
            "control": bool(row.Control), "value_type": row.ValueType,
        } for row in self._read(POINTS, "Points")]

    def point_specs(self) -> dict:
        return {row["name"]: row for row in self.point_catalog()}

    def read_points(self, names: list[str] | None = None) -> list[dict]:
        from sila2.framework.errors.defined_execution_error import DefinedExecutionError

        catalog = {row["name"]: row for row in self.point_catalog()}
        try:
            values = self._call(POINTS, "ReadPoints", Names=list(names or []))[0]
        except DefinedExecutionError as exc:
            raise AdapterError(f"读点位被拒（{exc.identifier}）：{exc.message}") from exc
        rows = []
        for reading in values:
            meta = {key: value for key, value in catalog.get(reading.Name, {"name": reading.Name}).items()
                    if key != "value_type"}
            rows.append({**meta, "value": None if reading.Error else _plain(reading.Value), "error": reading.Error})
        return rows

    def check_manual_write(self, name: str, value) -> None:
        """手动写之前的核对（不碰设备，设备服务那边还会再核一遍）：登记了、可写、不是控制信号、单个值、在范围里。"""
        specs = self.point_specs()
        if name not in specs:
            raise AdapterError(f"点 {name} 没有在点表里登记")
        spec = specs[name]
        if spec["control"]:
            raise AdapterError(f"点 {name} 是任务用的控制信号，不能手动写：要让设备动作请走指令")
        if not spec["writable"]:
            raise AdapterError(f"点 {name} 没有声明可写（writable: true），不能手动写")
        if value is None or isinstance(value, (dict, list)):
            raise AdapterError("一次只能写一个值（数、布尔或文字）")
        if isinstance(value, float) and not math.isfinite(value):
            raise AdapterError("值必须是有限的数")
        low, high = spec["min"], spec["max"]
        if low is not None or high is not None:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise AdapterError(f"点 {name} 限定了范围，要写一个数")
            if (low is not None and value < low) or (high is not None and value > high):
                raise AdapterError(f"{value:g} 超出点 {name} 允许的范围 {low if low is not None else '—'}–"
                                   f"{high if high is not None else '—'}")

    def write_point_manually(self, name: str, value, request_id: str = "") -> dict:
        """设备服务里一次做完「读当前值、写、回读」。RequestId 用点位写入记录号：重发同一个号只回放原结论。"""
        from sila2.framework.errors.defined_execution_error import DefinedExecutionError

        self.check_manual_write(name, value)
        try:
            outcome = self._call(POINTS, "WritePoint", RequestId=request_id or uuid.uuid4().hex, Name=name,
                                 Value=_point_value(value))[0]
        except DefinedExecutionError as exc:
            if exc.identifier in WRITE_REFUSALS:
                raise AdapterError(f"没有写入（{exc.identifier}）：{exc.message}") from exc
            raise AdapterIndeterminate(
                f"写 {name} = {value!r} 没有结论（{exc.identifier}：{exc.message}）：设备上的值可能已经变了，请核对"
            ) from exc
        return {"before": _plain(outcome.Before), "after": _plain(outcome.After), "matches": bool(outcome.Matches)}
