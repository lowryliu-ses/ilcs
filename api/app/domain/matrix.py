"""条件矩阵与孔位布局。随机化用计划里的种子，任何时候重放结果一致。"""
import itertools
from dataclasses import dataclass
from typing import Any

from ..core.rng import seeded

COLUMN_LETTERS = "ABCDEFGH"
# 孔板最多 8 行（A–H）。≤8 孔按 4 列、≤48 孔按 6 列排（与历史批次的孔位编号保持一致），
# 更大的板按标准 96 孔的 12 列排。
MAX_WELLS = 96


@dataclass(frozen=True)
class Condition:
    group: str
    levels: list[Any]
    label: str
    is_control: bool


@dataclass(frozen=True)
class WellAssignment:
    well: str
    group: str
    repeat: int
    levels: list[Any]
    label: str
    is_control: bool


def well_grid(plate: int) -> list[str]:
    if plate > MAX_WELLS:
        raise ValueError(f"孔板位数 {plate} 超过支持的上限 {MAX_WELLS}")
    cols = 4 if plate <= 8 else 6 if plate <= 48 else 12
    return [f"{COLUMN_LETTERS[index // cols]}{index % cols + 1}" for index in range(plate)]


def conditions(factors: list[dict], control: dict | None, points: list | None = None) -> list[Condition]:
    """条件组。给了显式设计点就用这些点（闭环提案），否则做全因子组合。"""
    level_sets = [factor.get("levels") or [] for factor in factors]
    if points:
        combos = [tuple(point) for point in points]
    else:
        combos = list(itertools.product(*level_sets)) if level_sets and all(level_sets) else [()]
    control_levels = (control or {}).get("cond") or []
    rows = []
    for index, combo in enumerate(combos):
        levels = list(combo)
        label = " · ".join(
            f"{factor['name']} {value}{factor.get('unit', '')}" for factor, value in zip(factors, levels)
        ) or "标准条件"
        is_control = bool(control_levels) and len(control_levels) == len(levels) and all(
            a == b for a, b in zip(control_levels, levels)
        )
        rows.append(Condition(f"C{index + 1:02d}", levels, label, is_control))
    return rows


def layout(
    factors: list[dict], control: dict | None, repeats: int, plate: int, style: str, seed: int,
    points: list | None = None,
) -> list[WellAssignment]:
    items = [
        (condition, repeat + 1)
        for condition in conditions(factors, control, points)
        for repeat in range(max(1, repeats))
    ]
    wells = well_grid(plate)
    order = list(range(len(wells)))
    if style == "randomized":
        nxt = seeded(seed)
        for index in range(len(order) - 1, 0, -1):
            swap = int(nxt() * (index + 1))
            order[index], order[swap] = order[swap], order[index]

    assignments = [
        WellAssignment(wells[order[position]], condition.group, repeat, condition.levels, condition.label, condition.is_control)
        for position, (condition, repeat) in enumerate(items[:plate])
    ]
    return sorted(assignments, key=lambda a: wells.index(a.well))


def material_demand(factors: list[dict], repeats: int, points: list | None = None) -> list[dict]:
    """因子水平换算到物料需求，用于计划页的物料预览。

    全因子矩阵里，一个因子的每个水平会出现在「其他因子水平数之积」个条件里：
    2×3 的矩阵中，第一个因子的每个水平各出现 3 次，不是 1 次。
    """
    rows = []
    level_counts = [len(factor.get("levels") or []) for factor in factors]
    for position, factor in enumerate(factors):
        material = factor.get("material")
        if not material:
            continue
        per = float(material.get("per", 0) or 0)
        if points:
            # 显式设计点：逐点累加该因子的水平
            total = sum(float(point[position]) * per for point in points if position < len(point)) * max(1, repeats)
        else:
            others = 1
            for index, count in enumerate(level_counts):
                if index != position and count:
                    others *= count
            total = (
                sum(float(level) * per for level in factor.get("levels") or [])
                * others * max(1, repeats)
            )
        rows.append(
            {
                "factor": factor.get("name"),
                "material": material.get("name"),
                "unit": material.get("unit"),
                "qty": round(total, 4),
            }
        )
    return rows


def target_issues(factors: list[dict], steps: list[dict], stations) -> list[str]:
    """因子声明的「作用参数」能否真的下发到设备。

    因子可以写 `target: {"step_id": "s03", "param": "electrolyte"}`：这个因子的水平会按孔位
    覆盖该设备步骤的参数。这里逐项核对步骤存在且是设备步骤、同一参数不被两个因子争用、
    每个水平都在至少一个可承接工位的参数范围内——否则批次开跑后才会被设备拒绝。
    """
    from .capability import station_fits
    from .steps import DEVICE, kind_of, step_id_of

    by_id = {step_id_of(step, index): step for index, step in enumerate(steps)}
    issues: list[str] = []
    claimed: dict[tuple[str, str], str] = {}
    for factor in factors:
        target = factor.get("target") or {}
        if not target:
            continue
        name = factor.get("name") or "未命名因子"
        step_id, param = str(target.get("step_id") or ""), str(target.get("param") or "")
        step = by_id.get(step_id)
        if step is None:
            issues.append(f"因子「{name}」作用的步骤 {step_id or '未选择'} 不在方法里")
            continue
        if kind_of(step) != DEVICE:
            issues.append(f"因子「{name}」作用的步骤「{step.get('name')}」不是设备步骤，参数无法下发")
            continue
        if not param:
            issues.append(f"因子「{name}」没有选择作用的参数")
            continue
        key = (step_id, param)
        if key in claimed:
            issues.append(f"因子「{name}」与「{claimed[key]}」作用于同一参数 {step.get('name')}.{param}")
            continue
        claimed[key] = name
        for level in factor.get("levels") or []:
            if not isinstance(level, (int, float)) or isinstance(level, bool):
                issues.append(f"因子「{name}」的水平 {level!r} 不是数值，不能作为设备参数")
                continue
            trial = {**step, "params": {**(step.get("params") or {}), param: level}}
            if not any(station_fits(station, trial) for station in stations):
                issues.append(
                    f"因子「{name}」的水平 {level} 超出所有可承接「{step.get('name')}」工位的 {param} 范围"
                )
    return issues


def condition_params(factors: list[dict], rows: list[dict]) -> dict[str, dict[str, dict]]:
    """按孔位展开作用参数：{step_id: {孔位: {参数: 水平}}}。建批次时冻结进快照。"""
    result: dict[str, dict[str, dict]] = {}
    for position, factor in enumerate(factors):
        target = factor.get("target") or {}
        step_id, param = target.get("step_id"), target.get("param")
        if not step_id or not param:
            continue
        for row in rows:
            levels = row.get("levels") or []
            if position >= len(levels):
                continue
            result.setdefault(step_id, {}).setdefault(row["well"], {})[param] = levels[position]
    return result


def point_issues(factors: list[dict], points: list, design_space: dict) -> list[str]:
    """外部提案里的设计点是否落在已批准的设计空间内。逐点给出原因，不合格的点不会悄悄丢掉。"""
    names = [factor.get("name") for factor in factors]
    bounds = (design_space or {}).get("bounds") or {}
    forbidden = (design_space or {}).get("forbidden") or []
    limit = (design_space or {}).get("max_points")
    issues: list[str] = []
    if not points:
        return ["提案没有任何设计点"]
    if isinstance(limit, int) and len(points) > limit:
        issues.append(f"提案 {len(points)} 个点超过设计空间允许的 {limit} 个")
    seen: set[tuple] = set()
    for number, point in enumerate(points, start=1):
        if len(point) != len(names):
            issues.append(f"第 {number} 个点有 {len(point)} 个水平，方案有 {len(names)} 个因子")
            continue
        key = tuple(point)
        if key in seen:
            issues.append(f"第 {number} 个点与前面的点重复")
        seen.add(key)
        for name, value in zip(names, point):
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                issues.append(f"第 {number} 个点的 {name} = {value!r} 不是数值")
                continue
            bound = bounds.get(name) or {}
            low, high = bound.get("min"), bound.get("max")
            if isinstance(low, (int, float)) and value < low:
                issues.append(f"第 {number} 个点的 {name} = {value} 低于设计空间下限 {low}")
            if isinstance(high, (int, float)) and value > high:
                issues.append(f"第 {number} 个点的 {name} = {value} 高于设计空间上限 {high}")
        values = dict(zip(names, point))
        for rule in forbidden:
            if rule and all(values.get(name) == expected for name, expected in rule.items()):
                combo = "、".join(f"{name}={expected}" for name, expected in rule.items())
                issues.append(f"第 {number} 个点命中禁止组合（{combo}）")
    return issues
