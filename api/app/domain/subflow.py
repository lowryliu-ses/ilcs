"""子流程展开。

子流程节点（`kind: subflow`）引用一个**已发布**的方法。建批次时把它原地展开进快照：

- 子方法的步骤标识加上前缀「子流程步骤标识.」，内部引用（前驱、出口条件、关卡来源与返工目标、
  分支来源与回环目标）一起改名，与父流程的标识不会撞；
- 子方法的起点接到子流程节点的前驱上（连同它在分支上的出口条件）；依赖子流程节点的后继，
  改为依赖子方法的所有终点（没有后继的步骤）——子流程整体完成，后继才开始；
- 子方法的 BOM 并入批次 BOM，按「物料 + 单位」合计。

展开之后执行器、排程器、推进器只看见普通步骤，所以子流程不需要自己的运行时。每个展开出来的
步骤带 `groups`（由外到内的子流程路径），界面据此把它们框在一起显示。

引用在展开时校验：不存在、未发布或待修订、自己引用自己（含间接）、嵌套超过 3 层都拒绝。
子方法修订发布后旧版本退役，父方法的引用随之失效——换成新版本要改父方法并重新评审，
不会悄悄换掉一个已批准流程里的一段。

纯函数：方法怎么取由调用方传入的 `resolve` 决定。
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable

from .graph import explicit, successors
from .steps import SUBFLOW, kind_of, normalize

MAX_DEPTH = 3


@dataclass(frozen=True)
class SubflowRecipe:
    id: str
    name: str
    version: str
    state: str
    steps: list[dict[str, Any]]
    bom: list[dict[str, Any]] = field(default_factory=list)
    needs_revision: bool = False


class SubflowError(Exception):
    def __init__(self, message: str, step_id: str = ""):
        super().__init__(message)
        self.message = message
        self.step_id = step_id


Resolver = Callable[[str], "SubflowRecipe | None"]


def references(steps: list[dict[str, Any]]) -> list[str]:
    """方法里直接引用的子方法编号（不含间接）。"""
    return [
        str((step.get("subflow") or {}).get("recipe_id") or "")
        for step in normalize(steps) if kind_of(step) == SUBFLOW
    ]


def has_subflow(steps: list[dict[str, Any]]) -> bool:
    return any(kind_of(step) == SUBFLOW for step in steps or [] if isinstance(step, dict))


def _rename(value: str, prefix: str, local: set[str]) -> str:
    return f"{prefix}{value}" if value in local else value


def _prefixed(step: dict[str, Any], prefix: str, local: set[str]) -> dict[str, Any]:
    row = copy.deepcopy(step)
    row["step_id"] = f"{prefix}{step['step_id']}"
    row["after"] = [_rename(ref, prefix, local) for ref in step.get("after") or []]
    if isinstance(step.get("when"), dict):
        row["when"] = {_rename(str(k), prefix, local): v for k, v in step["when"].items()}
    gate = row.get("gate")
    if isinstance(gate, dict):
        for key in ("source_step_id", "rework_to"):
            if gate.get(key):
                gate[key] = _rename(str(gate[key]), prefix, local)
    branch = row.get("branch")
    if isinstance(branch, dict):
        if branch.get("source_step_id"):
            branch["source_step_id"] = _rename(str(branch["source_step_id"]), prefix, local)
        for case in branch.get("cases") or []:
            if isinstance(case, dict) and case.get("loop_to"):
                case["loop_to"] = _rename(str(case["loop_to"]), prefix, local)
    return row


def resolve_checked(recipe_id: str, resolve: Resolver, stack: tuple[str, ...], step_id: str = "") -> SubflowRecipe:
    if not recipe_id:
        raise SubflowError("子流程没有选择引用的方法", step_id)
    if recipe_id in stack:
        raise SubflowError(f"子流程循环引用：{' → '.join((*stack, recipe_id))}", step_id)
    if len(stack) > MAX_DEPTH:
        raise SubflowError(f"子流程嵌套超过 {MAX_DEPTH} 层", step_id)
    sub = resolve(recipe_id)
    if sub is None:
        raise SubflowError(f"子流程引用的方法 {recipe_id} 不存在", step_id)
    if sub.state != "released" or sub.needs_revision:
        raise SubflowError(
            f"子流程引用的方法 {recipe_id}（{sub.name}）不是有效的已发布版本：只能引用已发布、无需修订的方法",
            step_id,
        )
    return sub


def expand(
    steps: list[dict[str, Any]], resolve: Resolver, stack: tuple[str, ...] = (),
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """展开子流程，返回（展开后的步骤, 子方法带来的 BOM 行）。没有子流程时原样返回。"""
    rows = normalize(steps)
    if not has_subflow(rows):
        return rows, []
    rows = explicit(rows)
    out: list[dict[str, Any]] = []
    extra_bom: list[dict[str, Any]] = []
    # 父流程每一步「完成」由哪些展开后的步骤代表：普通步骤是它自己，子流程是子方法的终点
    ends: dict[str, list[str]] = {}
    for row in rows:
        step_id = row["step_id"]
        mapped = list(dict.fromkeys(ref for parent in row["after"] for ref in ends.get(parent, [parent])))
        if kind_of(row) != SUBFLOW:
            out.append({**row, "after": mapped})
            ends[step_id] = [step_id]
            continue
        recipe_id = str((row.get("subflow") or {}).get("recipe_id") or "")
        sub = resolve_checked(recipe_id, resolve, stack, step_id)
        inner, inner_bom = expand(sub.steps, resolve, (*stack, sub.id))
        inner = explicit(inner)
        if not inner:
            raise SubflowError(f"子流程引用的方法 {sub.id} 没有步骤", step_id)
        prefix = f"{step_id}."
        local = {child["step_id"] for child in inner}
        group = {
            "step_id": step_id, "name": row.get("name") or sub.name, "recipe_id": sub.id,
            "recipe_name": sub.name, "version": sub.version,
        }
        conditions = row.get("when") if isinstance(row.get("when"), dict) else None
        following = successors(inner)
        sinks: list[str] = []
        for position, child in enumerate(inner):
            renamed = _prefixed(child, prefix, local)
            if not child.get("after"):
                renamed["after"] = list(mapped)
                if conditions:
                    renamed["when"] = dict(conditions)
            renamed["groups"] = [group, *(child.get("groups") or [])]
            out.append(renamed)
            if not following[position]:
                sinks.append(renamed["step_id"])
        ends[step_id] = sinks
        extra_bom.extend(copy.deepcopy(sub.bom or []))
        extra_bom.extend(inner_bom)
    return out, extra_bom


def merge_bom(base: list[dict[str, Any]], extra: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按「物料 + 单位」合计。用十进制相加，不让 0.1 + 0.2 这种误差进物料预留。"""
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[tuple[str, str]] = []
    for item in [*(base or []), *(extra or [])]:
        key = (str(item.get("material") or ""), str(item.get("unit") or ""))
        if key not in merged:
            merged[key] = {**item, "qty": Decimal(str(item.get("qty") or 0))}
            order.append(key)
        else:
            merged[key]["qty"] += Decimal(str(item.get("qty") or 0))
    rows = []
    for key in order:
        row = merged[key]
        qty: Decimal = row["qty"]
        rows.append({**row, "qty": int(qty) if qty == qty.to_integral_value() else float(str(qty))})
    return rows


def step_problems(step: dict[str, Any], resolve: Resolver, root_id: str) -> list[str]:
    """一个子流程节点的引用问题（给方法校验用）。展开到底，循环引用与嵌套深度一并查出。"""
    recipe_id = str((step.get("subflow") or {}).get("recipe_id") or "")
    try:
        sub = resolve_checked(recipe_id, resolve, (root_id,) if root_id else (), step.get("step_id", ""))
        expand(sub.steps, resolve, (root_id, sub.id) if root_id else (sub.id,))
    except SubflowError as error:
        return [error.message]
    return []
