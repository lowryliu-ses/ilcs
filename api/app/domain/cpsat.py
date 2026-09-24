"""CP-SAT 多批次排程模型（Google OR-Tools）。

`ortools` 在 requirements.txt 里固定版本，默认启用；`ILCS_SCHEDULER_BACKEND=search` 可关闭。环境里没有
它时 `available()` 为 False，服务层只用内置的顺序搜索（`optimizer.py`）。

模型（时间单位：分钟，整数）：
- 每个步骤一个区间；需要工位的步骤在各候选工位上各有一个可选区间，恰好选一个。
- 工位并行通道：每台工位一个累积约束，容量 = 通道数；已有占用（别的批次、维护、校准）
  作为固定区间计入。
- 依赖图：后继开始 ≥ 前驱结束；前后两个设备步骤落在不同工位时再加转运时长。
- 硬时限：开始 − 最晚前驱结束 ≤ maxGapMin。
- 任务依赖：上游批次全部结束后，下游批次的步骤才能开始；所选之外的上游给出下游的最早开工时刻。
- 目标：总跨度 × 批次数 + 各批次按优先级加权的完成时刻。

刻意的边界：承运工位（AGV）的占用与清洗缓冲不进模型——它们由确定性排程器在落地时精确
安排。所以 CP-SAT 给出的是**批次顺序与工位分配的建议、以及它离已证明最优还差多少（gap）**；真正写进时间线的
时间窗仍由 `scheduling.plan_steps` 按建议顺序生成，约束始终由同一份代码保证。
"""
from __future__ import annotations

from dataclasses import dataclass, field


def available() -> bool:
    try:
        import ortools.sat.python.cp_model  # noqa: F401
    except ImportError:
        return False
    return True


@dataclass(frozen=True)
class StepSpec:
    index: int
    duration: int
    stations: tuple[str, ...] = ()
    preds: tuple[int, ...] = ()
    max_gap: int | None = None


@dataclass(frozen=True)
class JobSpec:
    batch_id: str
    steps: tuple[StepSpec, ...]
    weight: int = 1


@dataclass
class Solution:
    status: str
    order: list[str] = field(default_factory=list)
    span_min: int | None = None
    # 目标值与求解器证明的下界之差占目标值的百分比；0 表示已证明最优
    gap_pct: float | None = None
    starts: dict[str, dict[int, int]] = field(default_factory=dict)
    stations: dict[str, dict[int, str]] = field(default_factory=dict)
    wall_ms: int = 0


def solve(
    jobs: list[JobSpec],
    channels: dict[str, int],
    busy: dict[str, list[tuple[int, int]]],
    *,
    transfer_min: int = 10,
    time_limit_sec: float = 5.0,
    workers: int = 4,
    precedence: list[tuple[str, str]] | None = None,
    release: dict[str, int] | None = None,
) -> Solution:
    from ortools.sat.python import cp_model

    model = cp_model.CpModel()
    horizon = sum(step.duration + transfer_min for job in jobs for step in job.steps)
    horizon += max((end for rows in busy.values() for _, end in rows), default=0) + 1
    horizon += max((release or {}).values(), default=0)
    starts: dict[tuple[str, int], cp_model.IntVar] = {}
    ends: dict[tuple[str, int], cp_model.IntVar] = {}
    choice: dict[tuple[str, int], dict[str, cp_model.IntVar]] = {}
    per_station: dict[str, list] = {station: [] for station in channels}

    for job in jobs:
        for step in job.steps:
            key = (job.batch_id, step.index)
            start = model.NewIntVar(0, horizon, f"s_{job.batch_id}_{step.index}")
            end = model.NewIntVar(0, horizon, f"e_{job.batch_id}_{step.index}")
            model.Add(end == start + step.duration)
            starts[key], ends[key] = start, end
            if step.stations:
                literals = {}
                for station in step.stations:
                    literal = model.NewBoolVar(f"x_{job.batch_id}_{step.index}_{station}")
                    interval = model.NewOptionalIntervalVar(
                        start, step.duration, end, literal, f"i_{job.batch_id}_{step.index}_{station}"
                    )
                    per_station.setdefault(station, []).append(interval)
                    literals[station] = literal
                model.AddExactlyOne(literals.values())
                choice[key] = literals

    for station, intervals in per_station.items():
        fixed = [
            model.NewIntervalVar(begin, end - begin, end, f"busy_{station}_{number}")
            for number, (begin, end) in enumerate(busy.get(station, [])) if end > begin
        ]
        items = intervals + fixed
        if items:
            model.AddCumulative(items, [1] * len(items), max(1, channels.get(station, 1)))

    completions = []
    for job in jobs:
        for step in job.steps:
            key = (job.batch_id, step.index)
            for parent in step.preds:
                parent_key = (job.batch_id, parent)
                if key in choice and parent_key in choice:
                    shared = [
                        station for station in choice[key] if station in choice[parent_key]
                    ]
                    same_terms = []
                    for station in shared:
                        both = model.NewBoolVar(f"same_{job.batch_id}_{parent}_{step.index}_{station}")
                        model.AddBoolAnd([choice[key][station], choice[parent_key][station]]).OnlyEnforceIf(both)
                        model.AddBoolOr([choice[key][station].Not(), choice[parent_key][station].Not()]).OnlyEnforceIf(both.Not())
                        same_terms.append(both)
                    same = sum(same_terms) if same_terms else 0
                    model.Add(starts[key] >= ends[parent_key] + transfer_min - transfer_min * same)
                else:
                    model.Add(starts[key] >= ends[parent_key])
            if step.max_gap is not None and step.preds:
                latest = model.NewIntVar(0, horizon, f"latest_{job.batch_id}_{step.index}")
                model.AddMaxEquality(latest, [ends[(job.batch_id, parent)] for parent in step.preds])
                model.Add(starts[key] - latest <= step.max_gap)
        completion = model.NewIntVar(0, horizon, f"done_{job.batch_id}")
        model.AddMaxEquality(completion, [ends[(job.batch_id, step.index)] for step in job.steps])
        completions.append((job, completion))

    done = {job.batch_id: completion for job, completion in completions}
    for before, after in precedence or []:
        if before in done and after in done:
            for step in next(job for job in jobs if job.batch_id == after).steps:
                model.Add(starts[(after, step.index)] >= done[before])
    for batch_id, floor in (release or {}).items():
        job = next((row for row in jobs if row.batch_id == batch_id), None)
        for step in job.steps if job else ():
            model.Add(starts[(batch_id, step.index)] >= floor)

    span = model.NewIntVar(0, horizon, "span")
    model.AddMaxEquality(span, [completion for _, completion in completions])
    model.Minimize(span * max(1, len(jobs)) + sum(job.weight * completion for job, completion in completions))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_sec
    solver.parameters.num_search_workers = workers
    solver.parameters.random_seed = 20260923
    status = solver.Solve(model)
    name = solver.StatusName(status).lower()
    result = Solution(status=name, wall_ms=round(solver.WallTime() * 1000))
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return result
    for job in jobs:
        result.starts[job.batch_id] = {step.index: solver.Value(starts[(job.batch_id, step.index)]) for step in job.steps}
        result.stations[job.batch_id] = {
            step.index: next(station for station, literal in choice[(job.batch_id, step.index)].items()
                             if solver.Value(literal))
            for step in job.steps if (job.batch_id, step.index) in choice
        }
    first_start = {
        job.batch_id: min(result.starts[job.batch_id].values(), default=0) for job in jobs
    }
    result.order = sorted(first_start, key=lambda batch_id: (first_start[batch_id], batch_id))
    result.span_min = solver.Value(span)
    objective, bound = solver.ObjectiveValue(), solver.BestObjectiveBound()
    result.gap_pct = round(100 * max(0.0, objective - bound) / objective, 1) if objective else 0.0
    return result
