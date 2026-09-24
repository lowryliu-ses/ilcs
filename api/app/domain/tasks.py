"""任务树与任务间依赖的纯规则。

- 依赖是「完成—开始」：上游任务的批次运行结束，下游任务的批次才能下发；排程把下游的开工
  放在上游计划结束之后。这里只判断图本身（成环、自依赖），满足与否由服务层按批次状态判。
- 父任务的状态由子任务汇总：全部结束才结束，有任何一个在跑就是执行中。
"""
from __future__ import annotations

ORDER = ("unassigned", "pending_accept", "accepted", "running", "data_review", "reporting", "done")
# 上游任务到了这些状态，就算「运行已结束」：数据复核与报告不挡下游开工
RUN_FINISHED = {"data_review", "reporting", "done"}


def dependency_issues(task_id: str, wanted: list[str], edges: dict[str, list[str]]) -> list[str]:
    """给 task_id 设上游 wanted 会不会出问题。`edges` 是现有的「任务 → 它的上游」。"""
    issues: list[str] = []
    if task_id in wanted:
        issues.append("任务不能依赖自己")
    graph = {key: list(value) for key, value in edges.items()}
    graph[task_id] = [ref for ref in wanted if ref != task_id]
    # 从每个上游往上走，走回自己就是环
    for start in graph[task_id]:
        stack, seen = [start], set()
        while stack:
            current = stack.pop()
            if current == task_id:
                issues.append(f"依赖 {start} 会形成环：{start} 已经直接或间接依赖本任务")
                stack = []
                break
            if current in seen:
                continue
            seen.add(current)
            stack.extend(graph.get(current, []))
    return issues


def aggregate(states: list[str]) -> str:
    """父任务状态。全部取消 → 取消；全部结束且至少一个完成 → 完成；有进展 → 执行中；否则取最靠前的。"""
    live = [state for state in states if state != "cancelled"]
    if not states:
        return "unassigned"
    if not live:
        return "cancelled"
    if all(state == "done" for state in live):
        return "done"
    if any(ORDER.index(state) >= ORDER.index("running") for state in live if state in ORDER):
        stages = [state for state in live if state in ORDER]
        if all(ORDER.index(state) >= ORDER.index("data_review") for state in stages):
            return min(stages, key=ORDER.index)
        return "running"
    return min((state for state in live if state in ORDER), key=ORDER.index, default="unassigned")


def chunks(items: list[str], size: int) -> list[list[str]]:
    if size <= 0:
        raise ValueError("每份数量必须大于 0")
    return [items[start:start + size] for start in range(0, len(items), size)]


def topo_order(ids: list[str], upstream: dict[str, list[str]]) -> list[str]:
    """稳定拓扑序：在原顺序的基础上把上游挪到下游前面。成环时原样返回。"""
    position = {value: index for index, value in enumerate(ids)}
    indegree = {value: len([ref for ref in upstream.get(value, []) if ref in position]) for value in ids}
    ready = [value for value in ids if indegree[value] == 0]
    out: list[str] = []
    while ready:
        ready.sort(key=position.get)
        current = ready.pop(0)
        out.append(current)
        for other in ids:
            if current in upstream.get(other, []):
                indegree[other] -= 1
                if indegree[other] == 0:
                    ready.append(other)
    return out if len(out) == len(ids) else ids


def respects(order: tuple[str, ...] | list[str], upstream: dict[str, list[str]]) -> bool:
    seen: set[str] = set()
    members = set(order)
    for value in order:
        if any(ref in members and ref not in seen for ref in upstream.get(value, [])):
            return False
        seen.add(value)
    return True
