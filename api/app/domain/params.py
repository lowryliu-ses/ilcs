"""能力参数的类型与单位：设备参数怎么解释、单位之间能不能换算。纯函数，不碰数据库。

能力的 `params` 仍是「键 → 显示名称」，界面、历史快照都照旧读它；类型与单位另存在
`param_specs`（键 → {type, unit, required}）。没登记规格的参数按「数值、单位未登记、必填」解释——
这正是有规格之前的行为，所以老能力、老流程不受影响。

单位只做同一量纲内的比例换算（g ↔ mg、mL ↔ μL）。温度这类不能按比例换算的量只认同名单位；
表里没有的单位也只认同名。换算用十进制：设定值会原样下发给设备，不能带进浮点误差。
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

PARAM_TYPES = {"number": "数值", "integer": "整数"}

# 规范写法 → (量纲, 相对该量纲基准单位的倍数)。倍数为 None 的量不能按比例换算，只认同名单位
_UNITS: dict[str, tuple[str, Decimal | None]] = {
    "kg": ("mass", Decimal("1000")), "g": ("mass", Decimal("1")),
    "mg": ("mass", Decimal("0.001")), "μg": ("mass", Decimal("0.000001")),
    "L": ("volume", Decimal("1000")), "mL": ("volume", Decimal("1")), "μL": ("volume", Decimal("0.001")),
    "m": ("length", Decimal("1000")), "cm": ("length", Decimal("10")), "mm": ("length", Decimal("1")),
    "μm": ("length", Decimal("0.001")),
    "h": ("time", Decimal("3600")), "min": ("time", Decimal("60")), "s": ("time", Decimal("1")),
    "A": ("current", Decimal("1")), "mA": ("current", Decimal("0.001")),
    "Ah": ("charge", Decimal("1")), "mAh": ("charge", Decimal("0.001")),
    "V": ("voltage", Decimal("1")), "mV": ("voltage", Decimal("0.001")),
    "bar": ("pressure", Decimal("100000")), "kPa": ("pressure", Decimal("1000")),
    "mbar": ("pressure", Decimal("100")), "Pa": ("pressure", Decimal("1")),
    "℃": ("temperature", None), "%": ("fraction", None),
}
# 常见的另一种写法：ASCII 的 u 代替 μ、小写 l、°C
_ALIASES = {
    "ug": "μg", "ul": "μL", "ml": "mL", "l": "L", "um": "μm", "°c": "℃", "degc": "℃",
    "sec": "s", "mah": "mAh", "ah": "Ah", "ma": "mA", "mv": "mV", "kpa": "kPa", "pa": "Pa",
}


def canonical_unit(unit: Any) -> str:
    """单位的规范写法：去空白，微符号统一成希腊字母 μ，常见别名收成一种。认不出的原样返回。"""
    text = str(unit or "").strip().replace("µ", "μ")
    if not text:
        return ""
    if text in _UNITS:
        return text
    return _ALIASES.get(text.lower(), text)


def convertible(source: Any, target: Any) -> bool:
    a, b = canonical_unit(source), canonical_unit(target)
    if not a or not b:
        return False
    if a == b:
        return True
    left, right = _UNITS.get(a), _UNITS.get(b)
    return bool(left and right and left[0] == right[0] and left[1] is not None and right[1] is not None)


def convert(value: Decimal, source: Any, target: Any) -> Decimal | None:
    """把 source 单位下的值换成 target 单位；不能换算返回 None。"""
    a, b = canonical_unit(source), canonical_unit(target)
    if not convertible(a, b):
        return None
    if a == b:
        return value
    return value * _UNITS[a][1] / _UNITS[b][1]  # type: ignore[operator]


def split_ratio(unit: Any) -> tuple[str, str] | None:
    """系数单位「目标单位/来源单位」，如 μL/mg → ("μL", "mg")。不是比值形式返回 None。"""
    text = str(unit or "").strip()
    if text.count("/") != 1:
        return None
    numerator, denominator = (canonical_unit(part) for part in text.split("/"))
    if not numerator or not denominator:
        return None
    return numerator, denominator


def decimal_of(value: Any) -> Decimal | None:
    """数值取十进制；布尔、空串、非数、无穷都不算数值。"""
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def spec_of(capability: dict | None, key: str) -> dict[str, Any]:
    """某个能力参数的规格。没登记的按「数值、单位未登记、必填」——有规格之前的行为。"""
    raw = ((capability or {}).get("param_specs") or {}).get(key) or {}
    label = ((capability or {}).get("params") or {}).get(key) or key
    kind = raw.get("type") if raw.get("type") in PARAM_TYPES else "number"
    return {
        "label": label, "type": kind, "unit": canonical_unit(raw.get("unit")),
        "required": raw.get("required", True) is not False,
    }


def spec_issues(params: dict[str, str], specs: dict[str, Any]) -> list[str]:
    """能力定义里的参数规格是否自洽：只能给已定义的参数写规格，类型、单位、必填的格式正确。"""
    issues: list[str] = []
    if specs in (None, {}):
        return issues
    if not isinstance(specs, dict):
        return ["参数规格格式不正确"]
    for key, spec in specs.items():
        if key not in (params or {}):
            issues.append(f"参数规格 {key} 不是本能力的参数")
            continue
        if not isinstance(spec, dict):
            issues.append(f"参数 {key} 的规格格式不正确")
            continue
        if spec.get("type") not in (None, "", *PARAM_TYPES):
            issues.append(f"参数 {key} 的类型 {spec.get('type')} 不受支持（只能是数值或整数）")
        if not isinstance(spec.get("unit", ""), str):
            issues.append(f"参数 {key} 的单位必须是文字")
        if not isinstance(spec.get("required", True), bool):
            issues.append(f"参数 {key} 的「必填」只能是是或否")
    return issues


def clean_specs(params: dict[str, str], specs: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """入库前的规格：去掉已删除参数的规格，单位收成规范写法，缺省值不存。"""
    result: dict[str, dict[str, Any]] = {}
    for key, spec in (specs or {}).items():
        if key not in (params or {}) or not isinstance(spec, dict):
            continue
        row: dict[str, Any] = {}
        if spec.get("type") in PARAM_TYPES and spec.get("type") != "number":
            row["type"] = spec["type"]
        if canonical_unit(spec.get("unit")):
            row["unit"] = canonical_unit(spec.get("unit"))
        if spec.get("required") is False:
            row["required"] = False
        if row:
            result[key] = row
    return result


def value_issues(spec: dict[str, Any], value: Any) -> list[str]:
    """一个已填的参数值是否符合规格（目前只有整数要核对）。"""
    number = decimal_of(value)
    if number is None:
        return [f"{spec['label']} 必须是数值"]
    if spec["type"] == "integer" and number != number.to_integral_value():
        return [f"{spec['label']} 必须是整数"]
    return []
