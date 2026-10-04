"""数据质量的纯规则：越界打标、设备输出核对、前后逻辑校验。

采集到的值分三种处理：
- **格式错**（不是数值、单位不对、枚举不在可选值里、文本为空）：值本身不成立，整次回传拒收；
- **越界**（超出指标允许范围、超出设备方法输出规则的范围）：值是真实测出来的，照常入库并打标，
  质量置为「可疑」，交数据审核下结论——拒收只会让真实但异常的数据消失；
- **逻辑冲突**（前后指标之间的约束，如放电容量不能大于充电容量）：按规则的级别打标或拒收。

打标（flag）是 `{code, message, rule_id?}`，挂在结果值或步骤执行上；不改变值本身。

设备回执自带的质量（`quality`：good / bad / uncertain）是另一回事：设备说这次读数不可信，值再落在范围里也不能
自动拿去判定（`receipt_quality`）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

OPS = {
    "<": lambda a, b: a < b, "<=": lambda a, b: a <= b, ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b, "==": lambda a, b: abs(a - b) <= 1e-9 * max(1.0, abs(a), abs(b)),
    "!=": lambda a, b: abs(a - b) > 1e-9 * max(1.0, abs(a), abs(b)),
}
SEVERITIES = ("flag", "reject")


def _num(value: Any) -> float | None:
    """转成有限数值；NaN / 无穷当作「不是数值」（传感器故障常报 NaN，比较运算对它永远为假）。"""
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def flag(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"code": code, "message": message, **extra}


def receipt_quality(payload: dict[str, Any] | None) -> tuple[str, dict[str, str]]:
    """检查点（设备回执）的质量：整条的，加上按样本拆开下发时每个孔位那一条的（`delivered.runs[].quality`）。

    good 以外（bad、uncertain；现场核实写入的检查点也是 uncertain）的读数不能自动拿去判定：质检关卡、测量条件分支、
    按读数定拆分份数都转人工，与前馈参数同一口径；写成的检测结果打「设备质量」标记、置可疑。
    孔位查不到自己那一条时按整条的（拆开时整条取各条里最差的）。
    """
    payload = payload or {}
    overall = str(payload.get("quality") or "good")
    per_well: dict[str, str] = {}
    delivered = payload.get("delivered") if isinstance(payload.get("delivered"), dict) else {}
    for run in delivered.get("runs") or []:
        if isinstance(run, dict) and run.get("quality"):
            for well in run.get("wells") or []:
                per_well[str(well)] = str(run["quality"])
    return overall, per_well


def range_flags(value_type: str, rules: dict, value: Any, label: str = "") -> list[dict]:
    """指标允许范围：越界打标，不拒收。"""
    if value_type != "number":
        return []
    number = _num(value)
    if number is None:
        return []
    low, high = _num((rules or {}).get("min")), _num((rules or {}).get("max"))
    prefix = f"{label} " if label else ""
    if low is not None and number < low:
        return [flag("out_of_range", f"{prefix}值 {number:g} 低于允许范围下限 {low:g}")]
    if high is not None and number > high:
        return [flag("out_of_range", f"{prefix}值 {number:g} 高于允许范围上限 {high:g}")]
    return []


def output_flags(outputs: list[dict], delivered: dict) -> list[dict]:
    """设备回执对照设备方法的输出规则：必报项缺失、数值越界都打标（逐孔位的逐个核对）。"""
    flags: list[dict] = []
    if not outputs:
        return flags
    wells = delivered.get("wells") if isinstance(delivered.get("wells"), dict) else None
    scopes: list[tuple[str, dict]] = (
        [(str(well), row) for well, row in wells.items() if isinstance(row, dict)] if wells else [("", delivered)]
    )
    for rule in outputs:
        key = str(rule.get("key") or "")
        if not key:
            continue
        label = rule.get("label") or key
        low, high = _num(rule.get("lo")), _num(rule.get("hi"))
        unit = f" {rule['unit']}" if rule.get("unit") else ""
        for well, values in scopes:
            where = f"孔位 {well} " if well else ""
            # 孔位里没有就看整批的回报（有些设备只报整批值）
            raw = values.get(key, delivered.get(key) if well else None)
            if raw is None:
                if rule.get("required"):
                    flags.append(flag("output_missing", f"{where}必报输出 {label} 设备没有回报", key=key, well=well))
                continue
            if rule.get("kind") == "series":
                flags.extend(_curve_flags(raw, where, label, low, high, unit, key, well))
                continue
            number = _num(raw)
            if number is None:
                flags.append(flag("output_invalid", f"{where}{label} 回报值 {raw!r} 不是数值", key=key, well=well))
                continue
            if low is not None and number < low:
                flags.append(flag("out_of_range", f"{where}{label} {number:g}{unit} 低于方法下限 {low:g}", key=key, well=well))
            elif high is not None and number > high:
                flags.append(flag("out_of_range", f"{where}{label} {number:g}{unit} 高于方法上限 {high:g}", key=key, well=well))
    return flags


def _curve_flags(raw: Any, where: str, label: str, low: float | None, high: float | None, unit: str,
                 key: str, well: str) -> list[dict]:
    """曲线型输出：写法不成立打标；给了上下限就看 y 有没有出界（报出界的点数与最值）。"""
    from .series import normalize

    curve, problems = normalize(raw)
    if problems:
        return [flag("output_invalid", f"{where}{label} 回报的不是曲线：{problems[0]}", key=key, well=well)]
    ys = [value for trace in curve["traces"] for value in trace["y"]]
    if low is not None and min(ys) < low:
        outside = sum(1 for value in ys if value < low)
        return [flag("out_of_range", f"{where}{label} 有 {outside} 个点低于方法下限 {low:g}{unit}（最低 {min(ys):g}）", key=key, well=well)]
    if high is not None and max(ys) > high:
        outside = sum(1 for value in ys if value > high)
        return [flag("out_of_range", f"{where}{label} 有 {outside} 个点高于方法上限 {high:g}{unit}（最高 {max(ys):g}）", key=key, well=well)]
    return []


@dataclass(frozen=True)
class LogicRule:
    """前后逻辑校验：左指标 op 右指标 × factor + offset（或右侧是常数）。同一检测任务（同一样本）内比较。"""

    id: str
    name: str
    left: str
    op: str
    right: str = ""
    value: float | None = None
    factor: float = 1.0
    offset: float = 0.0
    severity: str = "flag"


def rule_issues(left: str, op: str, right: str, value: Any, severity: str) -> list[str]:
    issues = []
    if not left:
        issues.append("左侧指标必填")
    if op not in OPS:
        issues.append(f"比较符 {op} 不受支持（可用：{' '.join(OPS)}）")
    if not right and _num(value) is None:
        issues.append("右侧要么是另一个指标，要么是一个常数")
    if right and right == left:
        issues.append("左右两侧是同一个指标")
    if severity not in SEVERITIES:
        issues.append("级别只能是 flag（打标）或 reject（拒收）")
    return issues


def violations(rules: list[LogicRule], values: dict[str, float]) -> list[tuple[LogicRule, str]]:
    """按指标代码取值，返回不满足的规则与说明。任一侧没有值的规则不判（缺值不是违规）。"""
    found = []
    for rule in rules:
        left = values.get(rule.left)
        if left is None:
            continue
        if rule.right:
            base = values.get(rule.right)
            if base is None:
                continue
            target = base * rule.factor + rule.offset
            described = (
                f"{rule.right}={base:g}"
                + (f" × {rule.factor:g}" if rule.factor != 1 else "")
                + (f" {'+' if rule.offset >= 0 else '−'} {abs(rule.offset):g}" if rule.offset else "")
            )
        else:
            if rule.value is None:
                continue
            target = rule.value
            described = f"{target:g}"
        check = OPS.get(rule.op)
        if check is not None and not check(left, target):
            found.append((rule, f"{rule.name}：{rule.left}={left:g} 不满足 {rule.op} {described}"))
    return found
