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


def conditions(factors: list[dict], control: dict | None) -> list[Condition]:
    level_sets = [factor.get("levels") or [] for factor in factors]
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


def layout(factors: list[dict], control: dict | None, repeats: int, plate: int, style: str, seed: int) -> list[WellAssignment]:
    items = [
        (condition, repeat + 1)
        for condition in conditions(factors, control)
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


def material_demand(factors: list[dict], repeats: int) -> list[dict]:
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
