"""OPC UA TaskExecution 设备（插件 `opcua_task`，从 ILCS 的 `opcua_v1` 抽出，明确失败带 SiLA 错误码）。

设备（或厂商 OPC UA 服务器）实现 `devices/contracts/opcua/TaskExecution.json`：在 `urn:ilcs:task-execution`
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
from pathlib import Path

from ..settings import settings
from .base import (
    AdapterContract, AdapterError, AdapterIndeterminate, AdapterUnreachable, CommandRequest,
    CommandResult,
)
from .contract import parse_receipt
from .http_client import timezone_of
from .opcua_session import OpcUaSession

DRIVER = "opcua_v1"
CONTRACT = json.loads(
    (Path(__file__).resolve().parents[3] / "contracts" / "opcua" / "TaskExecution.json").read_text(encoding="utf-8")
)


class OpcUaAdapter:
    def __init__(self, record):
        self.station_id = record.station_id
        self.config = dict(record.config or {})
        self.session = OpcUaSession(self.config, record.credential_ref or "", DRIVER)
        self.expected_device_id = str(self.config.get("expected_device_id") or "")
        self.device_timezone = timezone_of(str(self.config.get("device_timezone") or "UTC"))
        self._nodes: dict = {}
        self._lock = self.session.lock
        self.contract = AdapterContract(
            kind="real", protocol=record.protocol or "OPC UA", version=record.version or "1.0",
            supports_hold=bool(record.supports_hold), supports_abort=bool(record.supports_abort),
            supports_query=bool(record.supports_query), supports_dedup=bool(record.supports_dedup),
            note=record.note or "OPC UA TaskExecution 设备",
        )

    @property
    def policy(self) -> str:
        return self.session.policy

    @property
    def mode(self) -> str:
        return self.session.mode

    @property
    def security(self):
        return self.session.security

    # ---------- 连接 ----------

    def _connect(self):
        if self.session.client is not None and self._nodes:
            return self.session.client
        client = self.session.connect()
        try:
            index = client.get_namespace_index(CONTRACT["namespace"])
            task = client.nodes.objects.get_child([f"{index}:{name}" for name in CONTRACT["path"]])
            self._nodes = {
                "task": task,
                "identity": task.get_child(f"{index}:DeviceIdentity"),
                **{name: f"{index}:{name}" for name in CONTRACT["methods"]},
            }
        except Exception as exc:
            self.session.drop()
            raise AdapterError(f"服务器没有实现 ILCS TaskExecution 契约（{CONTRACT['namespace']}）：{exc}") from exc
        return client

    def close(self) -> None:
        """配置换版本或缓存清空时由注册表调用：关掉会话，不留悬空的连接与线程。"""
        with self._lock:
            self._drop()

    def _drop(self) -> None:
        self.session.drop()
        self._nodes = {}

    def _classify(self, exc: Exception, action: str) -> Exception:
        error = self.session.classify(exc, action, CONTRACT["rejections"])
        if self.session.client is None:
            self._nodes = {}
        return error

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
            raise AdapterError("设备声明不支持保持", code="NotSupported")
        response = self._invoke("HoldTask", request.command_id, request.target_command_id)
        return self._result(response, request.command_id)

    def abort(self, request: CommandRequest) -> CommandResult:
        if not self.contract.supports_abort:
            raise AdapterError("设备声明不支持终止", code="NotSupported")
        response = self._invoke("AbortTask", request.command_id, request.target_command_id)
        return self._result(response, request.command_id)
