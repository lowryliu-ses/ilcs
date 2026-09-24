"""方法步骤的依赖图（DAG）。

步骤可以声明 `after: [step_id, ...]`（前驱步骤）。规则刻意保持简单，便于编写与审核：

- **没有任何步骤声明 `after` 时是顺序流程**：第 i 步依赖第 i-1 步，行为与之前完全一致。
- **有步骤声明时进入依赖图模式**：声明了的按声明；没声明的仍依赖列表里的上一步。
  所以作者只需要在分叉、汇合的那几步写 `after`，其余照常排列。`after: []` 表示起点。
- **前驱必须排在前面**。列表顺序就是一个拓扑序，环在结构上不可能出现，编辑器里的
  排列与执行顺序对得上。编辑器拖线建依赖时会自动按拓扑序重排列表。

条件分支（`kind: branch`）让出边带条件：后继用 `when: {分支步骤: 出口}` 声明它在哪个出口上。
推进按「死路剪除」判定每个步骤：

- 入边**生效**：前驱已完成 / 已跳过，且（前驱不是分支，或分支选中的出口就是这条边的出口）；
- 入边**失效**：前驱未走（not_taken），或分支选了别的出口；
- 入边还没结论（前驱未开、进行中、失败、结果未知）时本步等着；
- 所有入边都有结论后：至少一条生效就开出，全部失效就标为「未走此分支」并继续向下传播。

所以分支之后的汇合步骤只等真正走到的那条路；并行分叉（没有 when 的边）仍然等全部前驱。
回环不是图里的边：分支的某个出口声明 `loop_to` 时，推进器把回环体里的记录作废后从目标重做，
图本身始终无环。

纯函数，不碰数据库。推进器、排程器、方法校验与前端编辑器（`web/src/features/recipes/rules.ts`）
共用这一套判据。
"""
from __future__ import annotations

from typing import Any

from .steps import BRANCH, branch_config, forward_case_keys, kind_of, loop_cases, step_id_of

# 步骤实例的状态里，对后继来说「已有结论」的三种
COMPLETED = "completed"
SKIPPED = "skipped"
NOT_TAKEN = "not_taken"
PASSED = {COMPLETED, SKIPPED}
RESOLVED = {COMPLETED, SKIPPED, NOT_TAKEN}


def graph_mode(steps: list[dict[str, Any]]) -> bool:
    return any(
        isinstance(step, dict) and ("after" in step or kind_of(step) == BRANCH) for step in steps or []
    )


def when_of(step: dict[str, Any]) -> dict[str, str]:
    when = (step or {}).get("when")
    return {str(k): str(v) for k, v in when.items()} if isinstance(when, dict) else {}


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


def descendants(steps: list[dict[str, Any]], index: int) -> set[int]:
    after = successors(steps)
    seen: set[int] = set()
    stack = list(after[index])
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(after[current])
    return seen


def roots(steps: list[dict[str, Any]]) -> list[int]:
    return [index for index, before in enumerate(predecessors(steps)) if not before]


def explicit(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把「未声明就依赖上一行」写成显式的 after：展开子流程、编辑器重排前都要先这样做，
    否则插进来的步骤会改变原本隐式依赖的那一行。"""
    ids = [step_id_of(step, index) for index, step in enumerate(steps)]
    return [
        {**step, "after": [ids[parent] for parent in before]}
        for step, before in zip(steps, predecessors(steps))
    ]


def loop_body(steps: list[dict[str, Any]], branch_index: int, target_index: int) -> set[int]:
    """回环体：目标步骤，加上目标的下游里同时是分支上游的那些。分支本身不在内。"""
    return {target_index} | (descendants(steps, target_index) & ancestors(steps, branch_index))


def graph_issues(steps: list[dict[str, Any]]) -> dict[int, list[str]]:
    """依赖声明的问题，按步骤下标给出。"""
    ids = [step_id_of(step, index) for index, step in enumerate(steps)]
    position = {step_id: index for index, step_id in enumerate(ids)}
    issues: dict[int, list[str]] = {}
    if not graph_mode(steps):
        return issues
    for index, extra in branch_graph_issues(steps).items():
        issues.setdefault(index, []).extend(extra)
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


def branch_graph_issues(steps: list[dict[str, Any]]) -> dict[int, list[str]]:
    """条件分支在依赖图上的约束。

    - 分支的每个后继都要用 when 说明自己在哪个出口上，否则不知道该不该走；
    - when 只能引用本步的前驱分支，出口必须是不回环的出口；
    - 判据来源必须在分支的上游依赖链上：否则分支开出时来源可能还没执行；
    - 回环目标必须在上游，且回环体「封闭」：体内步骤的后继只能在体内或是分支本身，
      否则重做回环体时，体外已经开出、甚至已经在设备上的步骤会拿到一个作废的前提。
    """
    ids = [step_id_of(step, index) for index, step in enumerate(steps)]
    position = {step_id: index for index, step_id in enumerate(ids)}
    before = predecessors(steps)
    after = successors(steps)
    issues: dict[int, list[str]] = {}
    for index, step in enumerate(steps):
        raw = (step or {}).get("when")
        if raw is None:
            continue
        if not isinstance(raw, dict):
            issues.setdefault(index, []).append("出口条件（when）格式不正确")
            continue
        for branch_id, case in when_of(step).items():
            at = position.get(branch_id)
            if at is None or at not in before[index]:
                issues.setdefault(index, []).append(f"出口条件引用的 {branch_id} 不是本步的前驱")
                continue
            if kind_of(steps[at]) != BRANCH:
                issues.setdefault(index, []).append(f"前驱 {branch_id} 不是条件分支，不能带出口条件")
                continue
            if case not in forward_case_keys(steps[at]):
                issues.setdefault(index, []).append(
                    f"分支「{steps[at].get('name') or branch_id}」没有可前进的出口 {case}"
                )
    for index, step in enumerate(steps):
        if kind_of(step) != BRANCH:
            continue
        name = step.get("name") or ids[index]
        for child in after[index]:
            if ids[index] not in when_of(steps[child]):
                issues.setdefault(child, []).append(f"本步是分支「{name}」的后继，必须指定走哪个出口")
        config = branch_config(step)
        upstream = ancestors(steps, index)
        source = str(config.get("source_step_id") or "")
        if config.get("mode") in {"measure", "form"} and source in position and position[source] not in upstream:
            issues.setdefault(index, []).append("判据来源必须在分支的上游依赖链上（沿前驱能走到）")
        for case in loop_cases(step):
            target = str(case.get("loop_to") or "")
            if target not in position:
                continue
            target_index = position[target]
            if target_index not in upstream:
                issues.setdefault(index, []).append(f"回环目标 {target} 必须是分支的上游步骤")
                continue
            body = loop_body(steps, index, target_index)
            for member in sorted(body):
                leaks = [child for child in after[member] if child not in body and child != index]
                if leaks:
                    issues.setdefault(index, []).append(
                        f"回环体内第 {member + 1} 步有通往回环外的后继（第 {leaks[0] + 1} 步）："
                        "重做时体外步骤会失去前提，请把它挪到分支之后"
                    )
                    break
    return issues


def frontier(
    steps: list[dict[str, Any]], status: dict[str, str], chosen: dict[str, str],
) -> tuple[list[int], list[int]]:
    """当前可以开出的步骤，与应当剪掉（未走此分支）的步骤。

    `status`：每一步最新一次有效实例的状态（没开过的不在里面）；`chosen`：已完成分支选中的出口。
    没开过、且所有入边都有结论的步骤才会被判定：「从未尝试」刻意排除失败、结果未知、进行中的
    步骤——一个分支完成不能顺手把另一个分支上失败的设备步骤重新开出来，那等于盲目重试物理动作。
    """
    ids = [step_id_of(step, index) for index, step in enumerate(steps)]
    before = predecessors(steps)
    status = dict(status)
    to_open: list[int] = []
    to_prune: list[int] = []
    changed = True
    while changed:
        changed = False
        for index, step_id in enumerate(ids):
            if step_id in status:
                continue
            edges: list[str] = []
            conditions = when_of(steps[index])
            for parent in before[index]:
                parent_state = status.get(ids[parent])
                if parent_state in PASSED:
                    case = conditions.get(ids[parent])
                    if kind_of(steps[parent]) == BRANCH and case is not None:
                        edges.append("active" if chosen.get(ids[parent]) == case else "dead")
                    else:
                        edges.append("active")
                elif parent_state == NOT_TAKEN:
                    edges.append("dead")
                else:
                    edges.append("pending")
            if "pending" in edges:
                continue
            if not edges or "active" in edges:
                to_open.append(index)
                status[step_id] = "opening"
            else:
                to_prune.append(index)
                status[step_id] = NOT_TAKEN
                changed = True
    return to_open, to_prune


def ready_after(
    steps: list[dict[str, Any]], completed: set[str], attempted: set[str],
) -> list[int]:
    """前驱都已完成、且从未尝试过的步骤（没有分支时 frontier 的特例）。"""
    status = {step_id: COMPLETED for step_id in completed}
    status.update({step_id: "attempted" for step_id in attempted if step_id not in completed})
    return frontier(steps, status, {})[0]


def all_resolved(steps: list[dict[str, Any]], status: dict[str, str]) -> bool:
    return all(status.get(step_id_of(step, index)) in RESOLVED for index, step in enumerate(steps))


def all_completed(steps: list[dict[str, Any]], completed: set[str]) -> bool:
    return all(step_id_of(step, index) in completed for index, step in enumerate(steps))



def critical_path_min(steps: list[dict[str, Any]]) -> float:
    """按计划时长算的最长路径（分钟）。并行分支不再把总时长简单相加；条件分支按最长的那条算。"""
    before = predecessors(steps)
    finish: list[float] = []
    for index, step in enumerate(steps):
        start = max((finish[parent] for parent in before[index]), default=0.0)
        finish.append(start + float(step.get("dur") or 0))
    return max(finish, default=0.0)
