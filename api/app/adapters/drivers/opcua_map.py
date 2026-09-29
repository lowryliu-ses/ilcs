"""OPC UA 节点 / 方法映射驱动（`opcua_map_v1`）。

面向「PLC 或视觉系统已经有 OPC UA 服务器，但地址空间是厂家自己的，没有实现 ILCS TaskExecution」的设备。
作业逻辑见 `point_map.py`；这里只把点名解释成节点：

- `points.<名>` 写节点 ID：`"ns=3;s=\\"DB_ILCS\\".\\"State\\""`，或带命名空间 URI 的 `"nsu=urn:…;s=Line.State"`
  （服务器重启后命名空间序号可能变，URI 不变）；也可以写 `{"node": …, "scale": 0.1}`。
- 写入按节点当前的数据类型编码（Boolean / Int16 / UInt32 / Float / Double / String …），不猜类型。
- 启动也可以是方法调用：`"start": {"method": {"object": "<对象节点>", "method": "<方法节点>", "args": ["{program}"]}}`。

安全与会话（证书钉住、客户端证书、None 策略只限非正式环境）与 `opcua_v1` 共用 `OpcUaSession`。
写入被服务器回 Bad 状态说明没写进去 → 明确失败；超时、会话断开 → 结果未知并重建会话。
"""
from __future__ import annotations

import re

from ..base import AdapterError, AdapterIndeterminate, AdapterUnreachable
from .opcua import COMMUNICATION, OpcUaSession
from .point_map import PointMapAdapter

DRIVER = "opcua_map_v1"
NAMESPACE_URI = re.compile(r"^nsu=(?P<uri>[^;]+);(?P<rest>.+)$")
# 方法调用返回这些状态码 = 设备明确没执行（可在配置 rejections 里补充）
DEFAULT_REJECTIONS = {
    "BadInvalidState": "Interlocked", "BadInvalidArgument": "InvalidParameters", "BadOutOfRange": "InvalidParameters",
    "BadResourceUnavailable": "DeviceBusy", "BadNotSupported": "NotSupported", "BadUserAccessDenied": "AccessDenied",
}


class OpcUaMapAdapter(PointMapAdapter):
    DRIVER = DRIVER
    PROTOCOL = "OPC UA 节点映射"
    NOTE = "OPC UA 节点映射（设备自有地址空间）"

    def __init__(self, record, journal_key: str = ""):
        super().__init__(record, journal_key)
        self.session = OpcUaSession(self.config, self.credential_ref, DRIVER)
        self.rejections = {**DEFAULT_REJECTIONS, **(self.config.get("rejections") or {})}
        self._nodes: dict = {}
        self._types: dict = {}
        self._client = None
        for name, point in self.points.items():
            node = point if isinstance(point, str) else (point or {}).get("node")
            if not isinstance(node, str) or not node.strip():
                raise AdapterError(f"点 {name} 必须写节点 ID（字符串或 {{\"node\": …}}）")

    def close(self) -> None:
        with self.session.lock:
            self.session.drop()
            self._nodes, self._types = {}, {}

    # ---------- 节点 ----------

    def _node_text(self, name: str) -> str:
        point = self.points[name]
        return point if isinstance(point, str) else point["node"]

    def _resolve(self, client, text: str) -> str:
        match = NAMESPACE_URI.match(text.strip())
        if match is None:
            return text.strip()
        try:
            index = client.get_namespace_index(match.group("uri"))
        except Exception as exc:
            raise AdapterError(f"服务器没有命名空间 {match.group('uri')}") from exc
        return f"ns={index};{match.group('rest')}"

    def _node(self, name: str, *, method: str = ""):
        with self.session.lock:
            client = self.session.connect()
            if client is not self._client:  # 新会话：节点缓存作废
                self._client, self._nodes, self._types = client, {}, {}
            key = method or name
            if key not in self._nodes:
                text = method or self._node_text(name)
                try:
                    self._nodes[key] = client.get_node(self._resolve(client, text))
                except AdapterError:
                    raise
                except Exception as exc:
                    raise AdapterError(f"节点 ID {text!r} 无法解析：{exc}") from exc
            return self._nodes[key]

    def _fail(self, exc: Exception, action: str, *, write: bool) -> Exception:
        from asyncua import ua

        if isinstance(exc, ua.UaStatusCodeError):
            status = ua.status_codes.get_name_and_doc(exc.code)[0]
            if status not in COMMUNICATION:
                if write:
                    return AdapterError(f"{action}被服务器拒绝：{status}")
                return AdapterIndeterminate(f"{action}返回 {status}")
        error = self.session.classify(exc, action)
        if self.session.client is None:
            self._nodes, self._types = {}, {}
        return error

    # ---------- I/O ----------

    def read_point(self, name: str):
        node = self._node(name)
        try:
            return node.read_value()
        except Exception as exc:
            raise self._fail(exc, f"读 {name} ", write=False) from exc

    def write_point(self, name: str, value) -> None:
        from asyncua import ua

        node = self._node(name)
        try:
            if name not in self._types:
                self._types[name] = node.read_data_value().Value.VariantType
            variant_type = self._types[name]
            node.write_value(ua.DataValue(ua.Variant(self._cast(value, variant_type), variant_type)))
        except (AdapterError, AdapterUnreachable):
            raise
        except Exception as exc:
            raise self._fail(exc, f"写 {name} ", write=True) from exc

    @staticmethod
    def _cast(value, variant_type):
        from asyncua import ua

        integers = {ua.VariantType.SByte, ua.VariantType.Byte, ua.VariantType.Int16, ua.VariantType.UInt16,
                    ua.VariantType.Int32, ua.VariantType.UInt32, ua.VariantType.Int64, ua.VariantType.UInt64}
        if variant_type == ua.VariantType.Boolean:
            return bool(value)
        if variant_type in integers:
            return int(round(float(value)))
        if variant_type in {ua.VariantType.Float, ua.VariantType.Double}:
            return float(value)
        if variant_type in {ua.VariantType.String, ua.VariantType.LocalizedText}:
            return str(value)
        raise AdapterError(f"节点数据类型 {variant_type.name} 驱动不写（只写布尔、整数、浮点与字符串）")

    def call_method(self, spec: dict, arguments: list) -> None:
        if not spec.get("object") or not spec.get("method"):
            raise AdapterError("start.method 必须给出 object 与 method 节点 ID")
        target = self._node("", method=spec["object"])
        method = self._node("", method=spec["method"])
        try:
            target.call_method(method, *arguments)
        except Exception as exc:
            from asyncua import ua

            if isinstance(exc, ua.UaStatusCodeError):
                status = ua.status_codes.get_name_and_doc(exc.code)[0]
                if status in self.rejections:
                    raise AdapterError(f"设备拒绝启动（{self.rejections[status]} / {status}）") from exc
            raise self.session.classify(exc, "调用启动方法") from exc
