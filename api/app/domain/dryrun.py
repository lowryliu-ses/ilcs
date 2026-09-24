"""执行前仿真的纯规则：条件分支的路径枚举、不可达节点、各路径时长、回环的最坏情况。

仿真不等于排程：排程只回答「这一份时间窗排不排得下」；仿真要回答「这个流程在所有可能的走法下
能不能走完、最长要多久、有没有永远走不到的节点、回环最坏会拖多久」。资源与时间线由服务层用同一个
排程器（`scheduling.plan_steps`）去验，这里只管图本身。
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Any

from . import graph
from .steps import BRANCH, branch_cases, forward_case_keys, kind_of, loop_cases, step_id_of

MAX_PATHS = 64


@dataclass(frozen=True)
class Path:
    choices: dict[str, str]
    executed: list[int]
    duration_min: float


def _walk(steps: list[dict[str, Any]], choices: dict[str, str]) -> list[int]:
    """按给定的分支出口把流程走一遍：返回会执行的步骤下标（其余被剪掉）。"""
    ids = [step_id_of(step, index) for index, step in enumerate(steps)]
    status: dict[str, str] = {}
    executed: list[int] = []
    for _ in range(len(steps) + 1):
        to_open, to_prune = graph.frontier(steps, status, choices)
        for index in to_prune:
            status[ids[index]] = graph.NOT_TAKEN
        for index in to_open:
            status[ids[index]] = graph.COMPLETED
            executed.append(index)
        if not to_open and not to_prune:
            break
    return sorted(executed)


def _duration(steps: list[dict[str, Any]], executed: list[int], durations: list[float]) -> float:
    """只数走到的步骤的最长路径。"""
    before = graph.predecessors(steps)
    alive = set(executed)
    finish: dict[int, float] = {}
    for index in range(len(steps)):
        if index not in alive:
            continue
        start = max((finish[parent] for parent in before[index] if parent in finish), default=0.0)
        finish[index] = start + durations[index]
    return max(finish.values(), default=0.0)


def paths(steps: list[dict[str, Any]], durations: list[float] | None = None) -> tuple[list[Path], bool]:
    """枚举分支出口的组合（每个分支取一个前进出口）。组合太多时截断，第二个返回值说明是否截断。"""
    durations = durations if durations is not None else [float(step.get("dur") or 0) for step in steps]
    ids = [step_id_of(step, index) for index, step in enumerate(steps)]
    branches = [(ids[index], forward_case_keys(step)) for index, step in enumerate(steps) if kind_of(step) == BRANCH]
    options = [[(branch_id, key) for key in keys] or [(branch_id, "")] for branch_id, keys in branches]
    result: list[Path] = []
    truncated = False
    for combo in itertools.product(*options) if options else [()]:
        if len(result) >= MAX_PATHS:
            truncated = True
            break
        choices = {branch_id: key for branch_id, key in combo if key}
        executed = _walk(steps, choices)
        result.append(Path(choices, executed, round(_duration(steps, executed, durations), 1)))
    return result, truncated


def unreachable(steps: list[dict[str, Any]], found: list[Path]) -> list[int]:
    reached = {index for path in found for index in path.executed}
    return [index for index in range(len(steps)) if index not in reached]


def loop_overhead(steps: list[dict[str, Any]], durations: list[float] | None = None) -> list[dict[str, Any]]:
    """每个回环出口在最坏情况下（回满 max_loops 次）额外增加的时长。"""
    durations = durations if durations is not None else [float(step.get("dur") or 0) for step in steps]
    ids = [step_id_of(step, index) for index, step in enumerate(steps)]
    rows = []
    for index, step in enumerate(steps):
        if kind_of(step) != BRANCH:
            continue
        rounds = int((step.get("branch") or {}).get("max_loops") or 0)
        for case in loop_cases(step):
            target = str(case.get("loop_to") or "")
            if target not in ids:
                continue
            body = graph.loop_body(steps, index, ids.index(target))
            body_steps = sorted(body)
            # 回环体本身的最长路径：只数体内步骤
            once = _duration(steps, body_steps, durations) if body_steps else 0.0
            rows.append({
                "branch_step_id": ids[index], "case": str(case.get("key")), "label": case.get("label") or case.get("key"),
                "max_loops": rounds, "body_steps": [ids[at] for at in body_steps],
                "extra_min_worst": round(once * rounds, 1),
            })
    return rows


def case_labels(steps: list[dict[str, Any]], choices: dict[str, str]) -> list[str]:
    ids = [step_id_of(step, index) for index, step in enumerate(steps)]
    labels = []
    for branch_id, key in choices.items():
        step = steps[ids.index(branch_id)]
        label = next((c.get("label") for c in branch_cases(step) if str(c.get("key")) == key), key)
        labels.append(f"{step.get('name') or branch_id}：{label}")
    return labels
