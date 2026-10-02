"""通用 HTTPS JSON 设备网关驱动。

它不是某台仪器协议的猜测，而是一份可部署的网关契约：厂商 SDK、PLC 或仪器私有
协议可由现场网关转换为这组 HTTP 端点。系统始终传递原 command_id，并要求响应回传
同一 ID、设备时间、质量与明确状态。
"""
from __future__ import annotations

from types import SimpleNamespace
import re

from ...core.config import settings
from ..contract import parse_receipt
from ..base import (
    AdapterContract, AdapterError, AdapterIndeterminate, CommandRequest, CommandResult,
)
from ..http_client import MAX_RESPONSE_BYTES, INDETERMINATE_STATUS, HttpTransport, timezone_of  # noqa: F401

DRIVER = "http_json_v1"


class HttpJsonAdapter:
    def __init__(self, record):
        self.station_id = record.station_id
        self.config = dict(record.config or {})
        self.transport = HttpTransport(self.config, record.credential_ref or "", driver=DRIVER)
        self.base_url = self.transport.base_url
        self.device_timezone = self._timezone(str(self.config.get("device_timezone") or "UTC"))
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
        return timezone_of(name)

    def _call(
        self, method: str, path: str, payload: dict | None = None, *, command_id: str = "",
        allow_not_found: bool = False, idempotency_key: str = "",
    ) -> dict | None:
        headers = {}
        if idempotency_key:
            header = str(self.config.get("idempotency_header") or "Idempotency-Key")
            if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", header):
                raise AdapterError("idempotency_header 格式无效")
            headers[header] = idempotency_key
        value = self.transport.request(
            method, path, payload, fields={"command_id": command_id}, allow_not_found=allow_not_found,
            headers=headers,
        )
        if value is None:
            return None
        if not isinstance(value, dict):
            raise AdapterIndeterminate("设备网关响应必须是 JSON 对象")
        return value

    def identity(self) -> dict:
        """网关健康接口的原始回报：设备身份、厂商、固件、方法目录都在这里（有就报）。"""
        return self._call("GET", self.paths["health"])

    def healthcheck(self) -> dict:
        response = self.identity()
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
        extra = {"method": request.method} if request.method else {}
        if request.material:
            # 这一步投哪种料（名称、单位、用量取哪个参数）：称量加料的网关据此核对装在设备上的料对不对，
            # 并按实际称量回报 delivered.materials。只是附加字段，params 照旧原样下发
            extra["material"] = dict(request.material)
        return {**extra,
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
