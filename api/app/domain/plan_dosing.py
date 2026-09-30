"""流程里「用量由方案给出」的投料步骤，与给出它用量的方案因子怎样对上。

方案检查（锁定、提交前）和建批次的预留必须按同一条规则认因子：检查放过了、预留却算出 0 或按错的参数算，
批次就会在没有预留的情况下下发，真实设备回报的消耗也对不上账。所以两处都走这里，规则只写一遍：

- 只看消耗步骤声明了投料物料（`material`）、而流程 BOM（含子流程合并进来的）没列的物料；
- 给出用量的因子：`material.name` 是这种料、`target.step_id` 是这一步、`target.param` 是这一步的用量参数
  （`dosing.dosing_param` 按步骤与能力推出来的那个）、`material.unit` 写了、`per` > 0；
- 恰好一个这样的因子才算给出了用量。其余带物料的因子（没有作用参数、作用在别的步骤或参数上）只是估算，不预留。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .dosing import dosing_param
from .params import decimal_of
from .steps import DEVICE, kind_of, normalize, step_id_of, step_material


@dataclass(frozen=True)
class DosedStep:
    """一个用量由方案给出的投料步骤。`factor` 是给出用量的因子在方案因子里的序号；对不上时为 None，
    `problem` 写明原因（给人看的整句）。"""

    index: int
    step_id: str
    name: str
    material: str
    factor: int | None
    problem: str


def _listed(bom: list[dict] | None) -> set[str]:
    return {str(item.get("material") or "") for item in bom or []}


def plan_dosed_steps(
    steps: list[dict[str, Any]], bom: list[dict] | None, factors: list[dict] | None,
    capabilities: dict[str, dict] | None,
) -> list[DosedStep]:
    """逐个列出用量由方案给出的投料步骤，并认出给出用量的因子。

    `steps` 应是批次实际要跑的步骤（子流程已展开），`bom` 是合并后的 BOM：锁定检查与建批次看的是同一份流程。
    子流程里的步骤带 `groups`、标识是 `s03.s01` 这种展开后的写法，方案因子作用不到它们，只能由被引用流程的 BOM 给量。
    """
    listed = _listed(bom)
    factors = [f for f in factors or []]
    out: list[DosedStep] = []
    for index, step in enumerate(normalize(steps or [])):
        material = step_material(step)
        if not material or material in listed:
            continue
        step_id = step_id_of(step, index)
        name = str(step.get("name") or step_id)
        label = f"第 {index + 1} 步「{name}」"

        def add(factor: int | None, problem: str) -> None:
            out.append(DosedStep(index, step_id, name, material, factor, problem))

        groups = step.get("groups") or []
        if groups:
            group = groups[-1] or {}
            add(None, (
                f"子流程「{group.get('name') or group.get('step_id') or ''}」里的「{name}」投 {material}，"
                f"被引用流程的 BOM 没列用量；子流程内的步骤不能由方案因子给出用量，请在被引用流程的 BOM 里列出 {material}"
            ))
            continue
        if kind_of(step) != DEVICE:
            add(None, (
                f"{label}是人工步骤，投的 {material} 不在 BOM 里：人工步骤的用量只能按 BOM 预留，"
                f"方案因子给不出，请修订流程把它加进 BOM"
            ))
            continue
        capability = (capabilities or {}).get(str(step.get("cap") or "")) or {}
        qualified: list[int] = []
        reasons: list[str] = []
        for position, factor in enumerate(factors):
            if not isinstance(factor, dict):
                continue
            spec = factor.get("material") or {}
            target = factor.get("target") or {}
            if str(spec.get("name") or "") != material or str(target.get("step_id") or "") != step_id:
                continue
            title = f"因子「{factor.get('name') or '未命名因子'}」"
            unit = str(spec.get("unit") or "").strip()
            per = decimal_of(spec.get("per"))
            param = dosing_param(step, capability, unit) if unit else ""
            aimed = str(target.get("param") or "")
            if not unit:
                reasons.append(f"{title}没写物料单位，建批次无法按单位预留")
            elif not param:
                reasons.append(
                    f"按物料单位 {unit} 推断不出「{name}」的用量参数（能力里没有或不止一个 {unit} 参数），"
                    f"请在流程步骤里指定用量参数"
                )
            elif aimed != param:
                reasons.append(f"{title}作用在 {aimed or '未选择的参数'}，这一步的用量取 {param}")
            elif per is None or per <= Decimal(0):
                reasons.append(f"{title}的每单位用量（per）没填或不大于 0，建批次算不出预留量")
            else:
                qualified.append(position)
        if len(qualified) == 1:
            add(qualified[0], "")
        elif qualified:
            names = "、".join(f"「{factors[p].get('name') or '未命名因子'}」" for p in qualified)
            add(None, f"{label}投 {material}，有 {len(qualified)} 个因子都给出它的用量（{names}），只能有一个")
        elif reasons:
            add(None, f"{label}投 {material}：{'；'.join(reasons)}")
        else:
            add(None, f"{label}投 {material}，流程 BOM 没列用量，方案里也没有给出 {material} 用量的因子")
    return out


def dosing_factors(
    steps: list[dict[str, Any]], bom: list[dict] | None, factors: list[dict] | None,
    capabilities: dict[str, dict] | None,
) -> dict[int, DosedStep]:
    """给出用量的因子序号 → 它给量的步骤。建批次只按这些因子预留，方案页只把这些标成「按方案用量预留」。"""
    return {
        row.factor: row for row in plan_dosed_steps(steps, bom, factors, capabilities) if row.factor is not None
    }
