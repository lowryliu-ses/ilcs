"""多批次排程优化：找一个让整体更早完成的批次投产顺序。

排程器（`scheduling.plan_steps`）是确定性的：给定批次顺序，它逐批贪心占用工位时间线，
得到一份满足全部约束（能力、参数范围、并行通道、资产容量与预约、转运、清洗、硬时限、
依赖图）的时间窗。所以「优化」归结为搜索顺序——每个候选顺序都交给排程器解码，
约束永远由同一份代码保证，优化器只比较结果，不另造一套约束。

- 批次不多（≤ 6）时穷举全部顺序，结果是这个解码器下的最优。
- 批次多时用迭代局部搜索：几种构造规则起步，反复做「取出一个批次插到别处」的改进，
  卡住后随机扰动再搜，直到时间或次数预算用完。随机种子固定，同样的输入给同样的结果。

目标：先比总跨度（最后一个设备步骤结束），再比加权完成时间（优先级高的批次早完成更好）。
可选的 CP-SAT 求解器（`domain/cpsat.py`）另给一个下界与候选顺序，见服务层。
"""
from __future__ import annotations

import itertools
import random
import time
from dataclasses import dataclass, field
from typing import Callable

EXHAUSTIVE_LIMIT = 6


@dataclass(frozen=True)
class Candidate:
    order: tuple[str, ...]
    ok: bool
    span_min: float = float("inf")
    weighted_min: float = float("inf")
    reason: str = ""
    payload: dict = field(default_factory=dict, compare=False, hash=False)

    @property
    def key(self) -> tuple[float, float]:
        return (self.span_min, self.weighted_min) if self.ok else (float("inf"), float("inf"))


Evaluate = Callable[[tuple[str, ...]], Candidate]


@dataclass
class SearchReport:
    best: Candidate
    evaluated: int
    method: str
    elapsed_ms: int
    starts: list[Candidate] = field(default_factory=list)


def _insertions(order: tuple[str, ...]):
    for source in range(len(order)):
        moved = order[source]
        rest = order[:source] + order[source + 1:]
        for target in range(len(order)):
            if target == source:
                continue
            yield rest[:target] + (moved,) + rest[target:]


def search(
    batch_ids: list[str],
    evaluate: Evaluate,
    *,
    seeds: list[tuple[str, ...]] | None = None,
    budget_sec: float = 3.0,
    max_evaluations: int = 4000,
    random_seed: int = 20260923,
) -> SearchReport:
    started = time.monotonic()
    cache: dict[tuple[str, ...], Candidate] = {}

    def score(order: tuple[str, ...]) -> Candidate:
        if order not in cache:
            cache[order] = evaluate(order)
        return cache[order]

    def out_of_budget() -> bool:
        return len(cache) >= max_evaluations or time.monotonic() - started > budget_sec

    ids = tuple(batch_ids)
    if len(ids) <= EXHAUSTIVE_LIMIT:
        best = min((score(order) for order in itertools.permutations(ids)), key=lambda c: c.key)
        return SearchReport(best, len(cache), "exhaustive", round((time.monotonic() - started) * 1000))

    starts = [score(tuple(seed)) for seed in (seeds or [ids]) if sorted(seed) == sorted(ids)]
    best = min(starts, key=lambda c: c.key)
    current = best
    rng = random.Random(random_seed)
    while not out_of_budget():
        improved = False
        for neighbour in _insertions(current.order):
            if out_of_budget():
                break
            candidate = score(neighbour)
            if candidate.key < current.key:
                current, improved = candidate, True
                break  # 首次改进即接受，重新从新顺序展开
        if current.key < best.key:
            best = current
        if not improved:
            # 局部最优：从最好解出发做几次随机交换再继续，跳出这个坑
            order = list(best.order)
            for _ in range(max(2, len(order) // 3)):
                a, b = rng.sample(range(len(order)), 2)
                order[a], order[b] = order[b], order[a]
            current = score(tuple(order))
    return SearchReport(best, len(cache), "local_search", round((time.monotonic() - started) * 1000), starts)
