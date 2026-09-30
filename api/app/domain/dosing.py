"""投料步骤的用量：取哪个能力参数、按什么单位、这条指令下发了多少。

执行器给驱动带「投哪种料、用量取哪个参数」，内置模拟按它回报消耗，消耗对账按它算这一步本该投多少，
方案检查按它核对因子是不是真的作用在用量参数上——几处必须是同一条规则，所以放在领域层这一个地方。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from .params import canonical_unit, decimal_of, spec_of

QUANTUM = Decimal("0.000001")


def dosing_param(step: dict[str, Any] | None, capability: dict | None, unit: str) -> str:
    """这一步的用量参数。步骤写了 `material_param` 就用它；没写就在能力参数里找登记单位等于物料单位的，
    恰好一个才用。推断不出（没有或有歧义）返回空串：宁可不回报、不对账，也不拿错参数去算。

    `capability` 是 `{"params": {...}, "param_specs": {...}}`（能力登记的原样）。
    """
    explicit = str((step or {}).get("material_param") or "").strip()
    if explicit:
        return explicit
    target = canonical_unit(unit)
    if not target:
        return ""
    matches = [key for key in (capability or {}).get("params") or {} if spec_of(capability, key)["unit"] == target]
    return matches[0] if len(matches) == 1 else ""


def param_unit(capability: dict | None, param: str) -> str:
    """用量参数登记的单位（规范写法）；没登记返回空串。下发的数值就是这个单位的量。"""
    return spec_of(capability, param)["unit"] if param else ""


def commanded_quantity(params: dict[str, Any] | None, param: str) -> Decimal:
    """一条指令对这个参数下发的总量：有孔位时各孔 `wells[w][param]` 之和（某孔没写就用顶层值），否则取顶层值。

    每孔 0 表示这一瓶跳过这种料（上位机的语义），照加 0。十进制求和，6 位小数。孔位只含下发时仍在用的样本，
    所以中途判废的瓶子不会算进来。
    """
    top = decimal_of((params or {}).get(param)) or Decimal(0)
    wells = (params or {}).get("wells")
    if isinstance(wells, dict) and wells:
        total = Decimal(0)
        for values in wells.values():
            value = decimal_of((values or {}).get(param)) if isinstance(values, dict) else None
            total += top if value is None else value
    else:
        total = top
    return total.quantize(QUANTUM)
