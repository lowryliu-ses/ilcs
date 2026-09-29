"""前馈参数：设备步骤的某个参数取自上游步骤的结果（如称重质量 → 注液量）。纯规则，不碰数据库。

绑定写在设备步骤的 `bindings` 上，键是要下发的能力参数：

    "bindings": {"electrolyte": {
        "source_step_id": "s02", "field": "mass", "scope": "sample", "unit": "g",
        "coefficient": {"value": 3900, "unit": "μL/g"},     # 或 {"factor": "注液系数"}：取方案因子
        "expect": [40, 80]}}

- 来源：上游设备步骤（取该步最近检查点回执里的测量值；逐样本时取 `delivered.wells` 里该样本孔位的值），
  或上游人工步骤（取记录表单字段；逐样本时取按样本录入的字段）；
- 计算只做「来源值 × 系数」加单位换算，系数单位写成「目标单位/来源单位」；不写系数就是纯单位换算；
  系数要么写在流程里（随流程审批冻结），要么引用方案里的一个因子（按样本继承的水平取值，随方案审批冻结）；
- `expect` 是计算结果的预期范围（目标参数的单位）：排程时按它匹配工位极限；下发时实际值超出它同样不下发——
  流程批准的就是这个窗口，窗口外的值不是批准过的设定值。

不支持通用表达式：前馈改的是下发给设备的设定值，规则必须能逐项核对。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from .params import canonical_unit, convert, convertible, decimal_of, spec_of, split_ratio
from .steps import DEVICE, MANUAL, kind_of, step_id_of

SCOPES = {"batch": "整批一个值", "sample": "逐样本"}
# 下发给设备的设定值保留 6 位小数，与物料精确数量同一精度
PRECISION = Decimal("0.000001")


def bindings_of(step: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = (step or {}).get("bindings") or {}
    if not isinstance(raw, dict):
        return {}
    return {str(key): value for key, value in raw.items() if isinstance(value, dict)}


def expect_of(binding: dict[str, Any] | None) -> tuple[float, float] | None:
    window = (binding or {}).get("expect")
    if not isinstance(window, (list, tuple)) or len(window) != 2:
        return None
    low, high = (decimal_of(value) for value in window)
    if low is None or high is None or low > high:
        return None
    return float(low), float(high)


def window_holds(window: Any, expect: tuple[float, float] | None) -> bool:
    """工位极限是否覆盖整个预期范围：排程时实际值还不知道，只能按最坏情况挑工位。"""
    if expect is None or not isinstance(window, (list, tuple)) or len(window) != 2:
        return False
    return expect[0] >= window[0] and expect[1] <= window[1]


def upstream(steps: list[dict[str, Any]], index: int) -> set[int]:
    """前馈来源可选的步骤：依赖图模式按祖先；顺序流程就是它前面的各步。"""
    from .graph import ancestors, graph_mode

    if graph_mode(steps):
        return set(ancestors(steps, index))
    return set(range(index))


def ratio_unit_issues(label: str, unit: Any, source_unit: str, target_unit: str) -> list[str]:
    ratio = split_ratio(unit)
    if ratio is None:
        return [f"{label} 的系数单位要写成「目标单位/来源单位」，如 {target_unit or 'μL'}/{source_unit or 'mg'}"]
    numerator, denominator = ratio
    issues: list[str] = []
    if target_unit and not convertible(numerator, target_unit):
        issues.append(f"{label} 的系数单位 {unit} 换算不到参数单位 {target_unit}")
    if source_unit and not convertible(source_unit, denominator):
        issues.append(f"{label} 的系数单位 {unit} 与来源单位 {source_unit} 对不上")
    return issues


def _coefficient_issues(label: str, coefficient: Any, source_unit: str, target_unit: str) -> list[str]:
    if coefficient in (None, {}):
        if source_unit and target_unit and not convertible(source_unit, target_unit):
            return [
                f"{label}：来源单位 {source_unit} 不能直接换算成 {target_unit}，"
                f"请填写系数（单位写成 {target_unit}/{source_unit}）"
            ]
        return []
    if not isinstance(coefficient, dict):
        return [f"{label} 的系数格式不正确"]
    factor = str(coefficient.get("factor") or "").strip()
    has_value = coefficient.get("value") not in (None, "")
    if factor and has_value:
        return [f"{label} 的系数只能二选一：写在流程里的固定值，或引用方案因子"]
    if "factor" in coefficient and not has_value:
        # 因子在方案里，单位与水平由方案校验核对
        return [] if factor else [f"{label} 引用方案因子作系数时必须写明因子名"]
    value = decimal_of(coefficient.get("value"))
    if value is None or value <= 0:
        return [f"{label} 的系数必须是大于 0 的数值"]
    return ratio_unit_issues(label, coefficient.get("unit"), source_unit, target_unit)


def binding_issues(
    step: dict[str, Any], steps: list[dict[str, Any]], index: int, capabilities: dict[str, dict],
) -> list[str]:
    """前馈配置是否完整、能否核对。来源、单位、系数、预期范围缺一不可。"""
    raw = (step or {}).get("bindings")
    if raw in (None, {}):
        return []
    if not isinstance(raw, dict):
        return ["前馈参数格式不正确"]
    if kind_of(step) != DEVICE:
        return ["只有设备步骤可以声明前馈参数"]
    capability = capabilities.get(step.get("cap") or "") or {}
    defined = capability.get("params") or {}
    ids = [step_id_of(row, position) for position, row in enumerate(steps)]
    allowed = upstream(steps, index)
    method_rules = (step.get("method") or {}).get("params") or {}
    issues: list[str] = []
    for param, binding in raw.items():
        if not isinstance(binding, dict):
            issues.append(f"参数 {param} 的前馈配置格式不正确")
            continue
        if param not in defined:
            issues.append(f"前馈参数 {param} 不是能力「{capability.get('name') or step.get('cap')}」的参数")
            continue
        spec = spec_of(capability, param)
        label = spec["label"]
        target_unit = spec["unit"]
        if param in (step.get("params") or {}):
            issues.append(f"{label} 已声明取自上游结果，不能再写固定值")
        if not target_unit:
            issues.append(f"{label} 没有登记单位：前馈要做单位换算，先在能力字典里给它登记单位")
        field = str(binding.get("field") or "").strip()
        scope = binding.get("scope") or "batch"
        if scope not in SCOPES:
            issues.append(f"{label} 的前馈范围只能是整批或逐样本")
        if not field:
            issues.append(f"{label} 必须指定来源字段")
        source = str(binding.get("source_step_id") or "")
        if source not in ids:
            issues.append(f"{label} 的前馈来源步骤 {source or '未选择'} 不在流程里")
        else:
            position = ids.index(source)
            origin = steps[position]
            name = origin.get("name") or source
            kind = kind_of(origin)
            if position not in allowed:
                issues.append(f"{label} 的前馈来源「{name}」必须是本步的上游步骤")
            if kind not in {DEVICE, MANUAL}:
                issues.append(f"{label} 的前馈来源「{name}」只能是设备步骤（回执测量值）或人工步骤（记录字段）")
            elif kind == MANUAL and field:
                fields = {str(row.get("key")): row for row in origin.get("form") or [] if isinstance(row, dict)}
                entry = fields.get(field)
                if entry is None:
                    issues.append(f"{label} 的前馈字段 {field} 不在来源人工步骤「{name}」的记录表单里")
                else:
                    if (entry.get("type") or "text") != "number":
                        issues.append(f"{label} 的前馈字段 {field} 必须是数值字段")
                    if scope == "sample" and not entry.get("per_sample"):
                        issues.append(f"逐样本前馈要求来源字段 {field} 按样本录入")
                    if scope == "batch" and entry.get("per_sample"):
                        issues.append(f"来源字段 {field} 是按样本录入的，前馈范围应选逐样本")
            elif kind == DEVICE and field:
                rules = {
                    str(row.get("key")): row for row in ((origin.get("method") or {}).get("outputs") or [])
                    if isinstance(row, dict) and row.get("key")
                }
                if rules and field not in rules:
                    issues.append(
                        f"{label} 的前馈字段 {field} 不在来源设备方法的输出规则里（可用：{'、'.join(rules)}）"
                    )
                # 回报的数字就是输出规则登记的单位：来源单位必须与它相同（g 写成 mg 就差一千倍），能换算不够
                declared = canonical_unit((rules.get(field) or {}).get("unit"))
                written = canonical_unit(binding.get("unit"))
                if declared and written and written != declared:
                    issues.append(f"{label} 的来源单位 {written} 与输出规则登记的单位 {declared} 不同：回报值按 {declared} 读")
        source_unit = canonical_unit(binding.get("unit"))
        if not source_unit:
            issues.append(f"{label} 必须写明来源值的单位")
        issues.extend(_coefficient_issues(label, binding.get("coefficient"), source_unit, target_unit))
        window = expect_of(binding)
        if window is None:
            issues.append(f"{label} 必须填写预期范围（下限 ≤ 上限）：排程按它匹配工位极限")
        else:
            rule = method_rules.get(param) or {}
            low, high = decimal_of(rule.get("min")), decimal_of(rule.get("max"))
            if (low is not None and window[0] < low) or (high is not None and window[1] > high):
                issues.append(
                    f"{label} 的预期范围 [{window[0]:g}, {window[1]:g}] 超出设备方法允许的 "
                    f"[{rule.get('min', '−∞')}, {rule.get('max', '∞')}]"
                )
    return issues


def factor_references(steps: list[dict[str, Any]]) -> list[tuple[dict[str, Any], str, dict[str, Any]]]:
    """流程里引用方案因子作系数的前馈：(步骤, 参数, 绑定)。"""
    rows = []
    for step in steps or []:
        for param, binding in bindings_of(step).items():
            coefficient = binding.get("coefficient")
            if isinstance(coefficient, dict) and str(coefficient.get("factor") or "").strip():
                rows.append((step, param, binding))
    return rows


def plan_factor_issues(
    steps: list[dict[str, Any]], factors: list[dict[str, Any]], capabilities: dict[str, dict],
) -> list[str]:
    """引用方案因子作系数时，方案里必须有这个因子，水平都是正数，单位写成「目标单位/来源单位」。"""
    by_name = {str(factor.get("name") or ""): factor for factor in factors or []}
    issues: list[str] = []
    for step, param, binding in factor_references(steps):
        spec = spec_of(capabilities.get(step.get("cap") or ""), param)
        label = f"「{step.get('name') or step.get('step_id')}」的 {spec['label']}"
        name = str(binding["coefficient"]["factor"]).strip()
        factor = by_name.get(name)
        if factor is None:
            issues.append(f"{label} 的前馈系数引用了因子「{name}」，方案里没有这个因子")
            continue
        levels = factor.get("levels") or []
        bad = [level for level in levels if (decimal_of(level) or 0) <= 0]
        if not levels or bad:
            issues.append(f"因子「{name}」作前馈系数，水平必须都是大于 0 的数值")
        if factor.get("target"):
            issues.append(f"因子「{name}」作前馈系数，不能同时作用于设备参数")
        issues.extend(ratio_unit_issues(
            f"因子「{name}」", factor.get("unit"), canonical_unit(binding.get("unit")), spec["unit"],
        ))
    return issues


def compute(
    raw: Decimal, source_unit: str, target_unit: str,
    coefficient: Decimal | None = None, coefficient_unit: str = "",
) -> Decimal | None:
    """来源值 × 系数，换算到目标单位；单位对不上返回 None（配置校验已拦下，这里只兜底）。"""
    if coefficient is None:
        value = convert(raw, source_unit, target_unit)
    else:
        ratio = split_ratio(coefficient_unit)
        if ratio is None:
            return None
        numerator, denominator = ratio
        base = convert(raw, source_unit, denominator)
        value = convert(base * coefficient, numerator, target_unit) if base is not None else None
    return value.quantize(PRECISION) if value is not None else None


def decimal_text(value: Decimal) -> str:
    """十进制值的显示与留档写法：不用科学计数法，去掉末尾的 0。"""
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text
