"""能力参数的类型与单位：设备参数怎么解释、单位之间能不能换算。纯函数，不碰数据库。

能力的 `params` 仍是「键 → 显示名称」，界面、历史快照都照旧读它；类型与单位另存在
`param_specs`（键 → {type, unit, required}）。没登记规格的参数按「数值、单位未登记、必填」解释——
这正是有规格之前的行为，所以老能力、老流程不受影响。

单位只做同一量纲内的比例换算（g ↔ mg、mL ↔ μL）。温度这类不能按比例换算的量只认同名单位；
表里没有的单位也只认同名。换算用十进制：设定值会原样下发给设备，不能带进浮点误差。

选项型参数（`type: enum`）：值是登记的选项之一（溶剂种类、测试协议名、气氛），原样作为文字下发；
没有单位、不能比大小，所以工位极限写「允许哪些选项」而不是区间，前馈不能作用于它，也不能当用量参数。

程序表参数（`type: program`）：值是一张表（充放电工步、升温程序），列定义与校验见 `domain/program.py`。
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

PARAM_TYPES = {"number": "数值", "integer": "整数", "enum": "选项", "program": "程序表"}
NUMERIC_TYPES = {"number", "integer"}
ENUM = "enum"
PROGRAM = "program"
MAX_OPTIONS = 50
OPTION_LIMIT = 60

# 规范写法 → (量纲, 相对该量纲基准单位的倍数)。倍数为 None 的量不能按比例换算，只认同名单位
_UNITS: dict[str, tuple[str, Decimal | None]] = {
    "kg": ("mass", Decimal("1000")), "g": ("mass", Decimal("1")),
    "mg": ("mass", Decimal("0.001")), "μg": ("mass", Decimal("0.000001")),
    "L": ("volume", Decimal("1000")), "mL": ("volume", Decimal("1")), "μL": ("volume", Decimal("0.001")),
    # 物质的量：合成类方案按 mmol / 当量给用量，经物料登记的摩尔质量、密度、浓度换成设备收的 mg、μL（domain/amounts.py）
    "mol": ("amount", Decimal("1000")), "mmol": ("amount", Decimal("1")), "μmol": ("amount", Decimal("0.001")),
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
    "ug": "μg", "ul": "μL", "ml": "mL", "l": "L", "um": "μm", "°c": "℃", "degc": "℃", "umol": "μmol",
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
        "label": label, "type": kind, "unit": "" if kind in (ENUM, PROGRAM) else canonical_unit(raw.get("unit")),
        "required": raw.get("required", True) is not False,
        "options": list(raw.get("options") or []) if kind == ENUM else [],
        # 程序表：列定义与最多行数（结构见 domain/program.py）
        "columns": list(raw.get("columns") or []) if kind == PROGRAM else [],
        "max_rows": raw.get("max_rows") if kind == PROGRAM else None,
    }


def is_enum(spec: dict[str, Any] | None) -> bool:
    return bool(spec) and spec.get("type") == ENUM


def option_list_issues(label: str, options: Any) -> list[str]:
    """选项表：非空、每项是非空文字、不重复、不太长。"""
    if not isinstance(options, list) or not options:
        return [f"{label}至少要有一个选项"]
    issues: list[str] = []
    seen: set[str] = set()
    for option in options:
        if not isinstance(option, str) or not option.strip():
            issues.append(f"{label}的选项必须是非空文字")
            continue
        text = option.strip()
        if len(text) > OPTION_LIMIT:
            issues.append(f"{label}的选项「{text[:20]}…」超过 {OPTION_LIMIT} 个字")
        if text in seen:
            issues.append(f"{label}的选项「{text}」重复")
        seen.add(text)
    if len(options) > MAX_OPTIONS:
        issues.append(f"{label}最多 {MAX_OPTIONS} 个选项")
    return issues


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
            issues.append(f"参数 {key} 的类型 {spec.get('type')} 不受支持（只能是数值、整数或选项）")
        if not isinstance(spec.get("unit", ""), str):
            issues.append(f"参数 {key} 的单位必须是文字")
        if spec.get("type") == ENUM:
            issues.extend(option_list_issues(f"选项型参数 {key} ", spec.get("options")))
            if canonical_unit(spec.get("unit")):
                issues.append(f"选项型参数 {key} 没有单位")
        elif spec.get("options"):
            issues.append(f"参数 {key} 不是选项型，不写选项")
        if spec.get("type") == PROGRAM:
            from . import program

            issues.extend(program.definition_issues(key, spec))
        elif spec.get("columns"):
            issues.append(f"参数 {key} 不是程序表，不写列定义")
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
        if spec.get("type") == PROGRAM:
            from . import program

            result[key] = program.clean_definition(spec)
            continue
        if spec.get("type") in PARAM_TYPES and spec.get("type") != "number":
            row["type"] = spec["type"]
        if spec.get("type") == ENUM:
            row["options"] = [str(option).strip() for option in spec.get("options") or [] if str(option).strip()]
        elif canonical_unit(spec.get("unit")):
            row["unit"] = canonical_unit(spec.get("unit"))
        if spec.get("required") is False:
            row["required"] = False
        if row:
            result[key] = row
    return result


def value_issues(spec: dict[str, Any], value: Any) -> list[str]:
    """一个已填的参数值是否符合规格：数值 / 整数，或选项型的值是登记的选项之一。"""
    if spec["type"] == ENUM:
        if not isinstance(value, str) or value not in spec["options"]:
            return [f"{spec['label']} 只能是 {'、'.join(spec['options']) or '（没有登记选项）'} 之一（现在是 {value!r}）"]
        return []
    if spec["type"] == PROGRAM:
        from . import program

        return program.value_issues(spec, value, spec["label"])
    number = decimal_of(value)
    if number is None:
        return [f"{spec['label']} 必须是数值"]
    if spec["type"] == "integer" and number != number.to_integral_value():
        return [f"{spec['label']} 必须是整数"]
    return []


def is_numeric_value(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def window_fits(value: Any, window: Any) -> bool:
    """一个设定值落不落在工位极限里：数值按 [下限, 上限]，选项按「允许的选项」，程序表按列极限逐格查。"""
    if isinstance(window, dict):
        from . import program

        return program.fits(value, window)
    if not isinstance(window, (list, tuple)) or not window:
        return False
    if isinstance(value, str):
        return all(isinstance(item, str) for item in window) and value in window
    if not is_numeric_value(value) or len(window) != 2 or not all(is_numeric_value(item) for item in window):
        return False
    return window[0] <= value <= window[1]


def limit_issues(spec: dict[str, Any], window: Any, name: str) -> list[str]:
    """工位极限的写法：数值参数 [下限, 上限]（下限小于上限）；选项型参数是允许的选项，要是登记选项的子集；
    程序表按列写 {列: 极限}。"""
    if spec["type"] == PROGRAM:
        from . import program

        return program.limit_issues(spec, window, name)
    if spec["type"] == ENUM:
        if not isinstance(window, (list, tuple)) or not window or not all(isinstance(item, str) for item in window):
            return [f"{name} 是选项型参数，极限要写允许的选项"]
        unknown = [item for item in window if item not in spec["options"]]
        return [f"{name} 的允许选项 {'、'.join(unknown)} 不是能力登记的选项"] if unknown else []
    if (
        not isinstance(window, (list, tuple)) or len(window) != 2
        or not all(is_numeric_value(item) for item in window) or not window[0] < window[1]
    ):
        return [f"{name} 下限必须小于上限"]
    return []
