"""合成类实验的用量：方案按物质的量（mmol）或当量（eq）给，设备按质量 / 体积收（mg、μL）。纯函数。

物料主数据里登记的换算（`materials.conversions`：1 单位折合多少基础单位）就是桥：
- 摩尔质量：基础单位 g 的固体登记 `{"mmol": "0.1529"}`（1 mmol = 0.1529 g，即 152.9 g/mol）；
- 密度：`{"mL": "1.2"}`（1 mL = 1.2 g）；
- 溶液浓度：基础单位 mL 的溶液登记 `{"mmol": "2"}`（1 mmol = 2 mL，即 0.5 mol/L）。
同量纲的单位按比例换（mmol ↔ μmol、mL ↔ μL），跨量纲只能经物料登记的换算——没登记就明确说缺哪项，
不猜摩尔质量。

方案因子的水平单位（`factor.unit`）和它作用的设备参数单位不同时，建批次时算出换算（`dose_spec`）冻结进快照：
- 普通量：设备值 = 水平 × ratio；
- 当量（`unit: "eq"`）：以另一个因子（限量试剂的物质的量）或固定的量为基准，设备值 = 水平 × 基准物质的量 × ratio。
样本上记的水平仍是化学家写的数（mmol、eq），统计与闭环看的也是它；只有下发给设备的参数是换算后的。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from .params import canonical_unit, convert, convertible, decimal_of

AMOUNT = "mmol"  # 物质的量的基准写法
EQ = "eq"
QUANTUM = Decimal("0.000001")


def amount_ratio(unit: Any) -> Decimal | None:
    """1 unit = ? mmol（mol / mmol / μmol）；不是物质的量单位返回 None。"""
    unit = canonical_unit(unit)
    if not unit or not convertible(unit, AMOUNT):
        return None
    return convert(Decimal(1), unit, AMOUNT)


def material_ratio(material: dict | None, source: Any, target: Any) -> Decimal | None:
    """这种物料 1 source 折合多少 target。同量纲直接按比例；跨量纲经物料的基础单位与登记的换算。换不了返回 None。"""
    source, target = canonical_unit(source), canonical_unit(target)
    if not source or not target:
        return None
    if convertible(source, target):
        return convert(Decimal(1), source, target)
    if not material:
        return None
    base = canonical_unit(material.get("base_unit"))
    conversions: dict[str, Decimal] = {}
    for unit, factor in (material.get("conversions") or {}).items():
        value = decimal_of(factor)
        if value is not None and value > 0:
            conversions[canonical_unit(unit)] = value

    def to_base(unit: str) -> Decimal | None:
        if base and convertible(unit, base):
            return convert(Decimal(1), unit, base)
        for registered, factor in conversions.items():
            if convertible(unit, registered):
                return convert(Decimal(1), unit, registered) * factor
        return None

    a, b = to_base(source), to_base(target)
    if a is None or b is None or b == 0:
        return None
    return a / b


def _need(material_name: str, source: str, target: str) -> str:
    hints = {"mass": "摩尔质量（1 mmol = ? g）", "volume": "密度或溶液浓度（1 mL = ? g / 1 mmol = ? mL）"}
    hint = hints["volume"] if target in {"L", "mL", "μL"} or source in {"L", "mL", "μL"} else hints["mass"]
    return f"物料 {material_name or '（未指定）'} 没有登记 {source} 与 {target} 之间的换算：请在物料主数据里登记{hint}"


def dose_spec(factor: dict, factors: list[dict], param_unit: Any, materials: dict[str, dict]) -> tuple[dict | None, str]:
    """一个作用在设备参数上的因子，水平怎么换成参数单位。单位相同（或没写单位）返回 (None, "")：水平原样下发。

    `materials` 是 {物料名: {base_unit, conversions}}。返回的换算写进批次快照（`factor["dose"]`），十进制写成字符串。
    """
    unit = canonical_unit(factor.get("unit"))
    target = canonical_unit(param_unit)
    name = factor.get("name") or "未命名因子"
    if not unit or not target or unit == target:
        return None, ""
    material_name = str((factor.get("material") or {}).get("name") or "")
    material = materials.get(material_name)
    if unit == EQ:
        basis = factor.get("basis") or {}
        ratio = material_ratio(material, AMOUNT, target)
        if ratio is None:
            return None, f"因子「{name}」按当量给：{_need(material_name, AMOUNT, target)}"
        if basis.get("factor"):
            position = next((index for index, row in enumerate(factors) if row.get("name") == basis["factor"]), None)
            if position is None or factors[position] is factor:
                return None, f"因子「{name}」的当量基准「{basis['factor']}」不是本方案的另一个因子"
            ref = factors[position]
            ref_unit = canonical_unit(ref.get("unit"))
            ref_ratio = amount_ratio(ref_unit)
            if ref_ratio is None:
                ref_material = materials.get(str((ref.get("material") or {}).get("name") or ""))
                ref_ratio = material_ratio(ref_material, ref_unit, AMOUNT) if ref_unit else None
            if ref_ratio is None:
                return None, (f"因子「{name}」的当量基准「{ref.get('name')}」的单位 {ref_unit or '（未填）'} 换不成物质的量："
                              f"基准因子要按 mmol 给，或它的物料登记了摩尔质量")
            return {"ratio": str(ratio), "basis": position, "basis_ratio": str(ref_ratio), "from": EQ, "to": target}, ""
        amount = decimal_of(basis.get("amount"))
        basis_ratio = amount_ratio(basis.get("unit") or AMOUNT)
        if amount is None or amount <= 0 or basis_ratio is None:
            return None, f"因子「{name}」按当量给，要指明基准：另一个因子（限量试剂），或一个固定的物质的量（如 0.5 mmol）"
        return {"ratio": str(ratio), "basis_amount": str(amount * basis_ratio), "from": EQ, "to": target}, ""
    ratio = material_ratio(material, unit, target)
    if ratio is None:
        return None, f"因子「{name}」的单位 {unit} 换不成设备参数的单位 {target}：{_need(material_name, unit, target)}"
    return {"ratio": str(ratio), "from": unit, "to": target}, ""


def dosed_level(factor: dict, levels: list, position: int) -> Any:
    """样本在这个因子上的水平换成设备参数单位（快照里冻结了 `dose` 才换）；不是数的水平（选项）原样返回。"""
    level = levels[position] if position < len(levels) else None
    dose = factor.get("dose")
    number = decimal_of(level)
    if not dose or number is None:
        return level
    value = number * Decimal(str(dose["ratio"]))
    if "basis" in dose:
        index = int(dose["basis"])
        basis = decimal_of(levels[index]) if index < len(levels) else None
        if basis is None:
            return None
        value *= basis * Decimal(str(dose["basis_ratio"]))
    elif "basis_amount" in dose:
        value *= Decimal(str(dose["basis_amount"]))
    return float(value.quantize(QUANTUM))


def describe(dose: dict | None) -> str:
    """给人看的换算说明：「1 mmol = 152.9 mg」「eq × 基准 × 127.4 μL/mmol」。"""
    if not dose:
        return ""
    ratio = Decimal(str(dose["ratio"])).normalize()
    if dose.get("from") == EQ:
        basis = f"基准 {Decimal(str(dose['basis_amount'])).normalize():f} mmol" if "basis_amount" in dose else "基准因子的物质的量"
        return f"当量 × {basis} × {ratio:f} {dose.get('to')}/mmol"
    return f"1 {dose.get('from')} = {ratio:f} {dose.get('to')}"


def attach_doses(factors: list[dict], steps: list[dict], capabilities: dict[str, dict],
                 materials: dict[str, dict]) -> tuple[list[dict], list[str]]:
    """给方案因子算好换算（建批次冻结进快照；方案页预览物料需求也用）。返回 (带 `dose` 的因子, 换不过去的说明)。

    只有作用在数值型设备参数上、单位与参数不同的因子才换；选项、程序表参数与没写单位的照旧原样。"""
    from .params import spec_of
    from .steps import normalize, step_id_of

    by_id = {step_id_of(step, index): step for index, step in enumerate(normalize(steps or []))}
    out: list[dict] = []
    problems: list[str] = []
    for factor in factors or []:
        clean = {key: value for key, value in factor.items() if key != "dose"}
        target = factor.get("target") or {}
        step = by_id.get(str(target.get("step_id") or ""))
        param = str(target.get("param") or "")
        if step is None or not param:
            out.append(clean)
            continue
        spec = spec_of((capabilities or {}).get(str(step.get("cap") or "")), param)
        if spec["type"] in ("enum", "program"):
            out.append(clean)
            continue
        dose, problem = dose_spec(factor, factors, spec["unit"], materials)
        if problem:
            problems.append(problem)
        out.append({**clean, "dose": dose} if dose else clean)
    return out, problems
