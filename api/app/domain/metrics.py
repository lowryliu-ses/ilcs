"""指标值的类型与规则校验。纯函数，回传与人工录入共用。

曲线（`series`）型指标的值是一组 x–y 点，写法与规则见 `domain/series.py`；指标的单位是 y 的单位。
"""
from __future__ import annotations

import math
from typing import Any

from . import series

VALUE_TYPES = ("number", "text", "enum", "series")


def validate_rules(value_type: str, rules: dict) -> list[str]:
    problems: list[str] = []
    if value_type == "number":
        low, high = rules.get("min"), rules.get("max")
        for name, value in (("min", low), ("max", high)):
            if value is not None and not isinstance(value, (int, float)):
                problems.append(f"规则 {name} 必须是数值")
        if isinstance(low, (int, float)) and isinstance(high, (int, float)) and low > high:
            problems.append("规则 min 不能大于 max")
    if value_type == "enum":
        options = rules.get("options")
        if not isinstance(options, list) or not options:
            problems.append("枚举指标必须在规则里给出 options")
    if value_type == "series":
        problems.extend(series.rule_issues(rules))
    return problems


def check_value(
    value_type: str, unit: str, rules: dict, value: Any, submitted_unit: str,
) -> list[str]:
    """校验一个回传值的格式：类型、单位、枚举、空文本。这些不通过说明值本身不成立，整次拒收。

    超出允许范围不在这里：越界的值是真实测出来的，入库并打标（`dataquality.range_flags`），交审核下结论。
    """
    problems: list[str] = []
    if submitted_unit and unit and submitted_unit != unit:
        problems.append(f"单位 {submitted_unit} 与指标标准单位 {unit} 不一致")
    if value_type == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            problems.append(f"值 {value!r} 不是数值")
        elif not math.isfinite(value):
            problems.append(f"值 {value!r} 不是有限数值")
    elif value_type == "text":
        if not isinstance(value, str) or not value.strip():
            problems.append("文本指标的值不能为空")
    elif value_type == "enum":
        options = rules.get("options") or []
        if value not in options:
            problems.append(f"值 {value!r} 不在可选值 {options} 中")
    elif value_type == "series":
        problems.extend(series.issues(value, rules))
    return problems


def collected(required: list[str], recorded: dict[str, Any]) -> tuple[bool, list[str]]:
    """全部要求指标是否都有合法采集记录。

    缺值不当成 0；声明无法测得的指标算「有记录」，但会带着原因——它不会让
    「全部指标已采集」被悄悄满足，因为原因要写进报告的排除说明。
    """
    missing = [metric_id for metric_id in required if metric_id not in recorded]
    return not missing, missing
