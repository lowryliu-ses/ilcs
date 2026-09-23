"""方法步骤的依赖图（DAG）。

步骤可以声明 `after: [step_id, ...]`（前驱步骤）。规则刻意保持简单，便于编写与审核：

- **没有任何步骤声明 `after` 时是顺序流程**：第 i 步依赖第 i-1 步，行为与之前完全一致。
- **有步骤声明时进入依赖图模式**：声明了的按声明；没声明的仍依赖列表里的上一步。
  所以作者只需要在分叉、汇合的那几步写 `after`，其余照常排列。`after: []` 表示起点。
- **前驱必须排在前面**。列表顺序就是一个拓扑序，环在结构上不可能出现，编辑器里的
  排列与执行顺序对得上。

纯函数，不碰数据库。推进器、排程器、方法校验与前端编辑器（`web/src/features/recipes/rules.ts`）
共用这一套判据。
"""
from __future__ import annotations

from typing import Any

from .steps import step_id_of


def graph_mode(steps: list[dict[str, Any]]) -> bool:
    return any(isinstance(step, dict) and "after" in step for step in steps or [])


def predecessors(steps: list[dict[str, Any]]) -> list[list[int]]:
    """每一步的前驱下标。引用不存在或排在后面的步骤被忽略（校验会单独报出来）。"""
    ids = [step_id_of(step, index) for index, step in enumerate(steps)]
    position = {step_id: index for index, step_id in enumerate(ids)}
    linear = not graph_mode(steps)
    found: list[list[int]] = []
    for index, step in enumerate(steps):
        if linear or "after" not in step:
            found.append([index - 1] if index > 0 else [])
            continue
        refs = step.get("after") or []
        found.append(sorted({position[ref] for ref in refs if ref in position and position[ref] < index}))
    return found


def successors(steps: list[dict[str, Any]]) -> list[list[int]]:
    result: list[list[int]] = [[] for _ in steps]
    for index, before in enumerate(predecessors(steps)):
        for parent in before:
            result[parent].append(index)
    return result


def ancestors(steps: list[dict[str, Any]], index: int) -> set[int]:
    before = predecessors(steps)
    seen: set[int] = set()
    stack = list(before[index])
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(before[current])
    return seen


def roots(steps: list[dict[str, Any]]) -> list[int]:
    return [index for index, before in enumerate(predecessors(steps)) if not before]


def graph_issues(steps: list[dict[str, Any]]) -> dict[int, list[str]]:
    """依赖声明的问题，按步骤下标给出。"""
    ids = [step_id_of(step, index) for index, step in enumerate(steps)]
    position = {step_id: index for index, step_id in enumerate(ids)}
    issues: dict[int, list[str]] = {}
    if not graph_mode(steps):
        return issues
    for index, step in enumerate(steps):
        if "after" not in step:
            continue
        refs = step.get("after")
        if not isinstance(refs, list):
            issues.setdefault(index, []).append("前驱步骤必须是步骤标识列表")
            continue
        for ref in refs:
            if ref == ids[index]:
                issues.setdefault(index, []).append("步骤不能依赖自己")
            elif ref not in position:
                issues.setdefault(index, []).append(f"前驱步骤 {ref} 不存在")
            elif position[ref] > index:
                issues.setdefault(index, []).append(
                    f"前驱步骤 {ref}（第 {position[ref] + 1} 步）排在本步之后：请把它移到前面"
                )
    return issues


def ready_after(
    steps: list[dict[str, Any]], completed: set[str], attempted: set[str],
) -> list[int]:
    """前驱都已完成、且从未尝试过的步骤。

    「从未尝试」刻意排除失败、结果未知、进行中的步骤：一个分支完成不能顺手把另一个分支上
    失败的设备步骤重新开出来——那等于盲目重试物理动作。返工与审核退回有自己的显式入口。
    """
    ids = [step_id_of(step, index) for index, step in enumerate(steps)]
    before = predecessors(steps)
    return [
        index for index, step_id in enumerate(ids)
        if step_id not in completed and step_id not in attempted
        and all(ids[parent] in completed for parent in before[index])
    ]


def all_completed(steps: list[dict[str, Any]], completed: set[str]) -> bool:
    return all(step_id_of(step, index) in completed for index, step in enumerate(steps))


def critical_path_min(steps: list[dict[str, Any]]) -> float:
    """按计划时长算的最长路径（分钟）。并行分支不再把总时长简单相加。"""
    before = predecessors(steps)
    finish: list[float] = []
    for index, step in enumerate(steps):
        start = max((finish[parent] for parent in before[index]), default=0.0)
        finish.append(start + float(step.get("dur") or 0))
    return max(finish, default=0.0)
