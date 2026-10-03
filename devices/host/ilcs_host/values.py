"""点位值与 SiLA `Any` 之间的换算（`PointAccess.PointValue` 只收 Real / Integer / Boolean / String）。

`Any` 的类型 XML 必须带 SiLA 命名空间：sila2 0.14.0 按字符串比对 `AllowedTypes`，不带会被拒收。
"""
from __future__ import annotations

import math

from sila2.framework.data_types.any import SilaAnyType

TYPE_XML = '<DataType xmlns="http://www.sila-standard.org"><Basic>{}</Basic></DataType>'
VALUE_TYPES = ("Real", "Integer", "Boolean", "String")


def to_any(value) -> SilaAnyType:
    """读回来的值 → Any。读不到（None）给空文字，调用方在 Error 里写明原因；非有限的数按文字报。"""
    if isinstance(value, bool):
        return SilaAnyType(TYPE_XML.format("Boolean"), value)
    if isinstance(value, int):
        return SilaAnyType(TYPE_XML.format("Integer"), value)
    if isinstance(value, float) and math.isfinite(value):
        return SilaAnyType(TYPE_XML.format("Real"), value)
    return SilaAnyType(TYPE_XML.format("String"), "" if value is None else str(value))


def from_any(value: SilaAnyType):
    """收到的 Any → 写给插件的值（插件按点的类型编码）。"""
    return value.value


def value_type(point) -> str:
    """点位目录里的值类型：点上写了 `value_type` 就用它；Modbus 按寄存器类型推；其余按数处理。"""
    if not isinstance(point, dict):
        return "Real"
    declared = point.get("value_type")
    if declared in VALUE_TYPES:
        return declared
    kind = point.get("type")
    if kind == "bool":
        return "Boolean"
    if kind == "ascii":
        return "String"
    if kind in {"uint16", "int16", "uint32", "int32"} and point.get("scale", 1) == 1:
        return "Integer"
    return "Real"
