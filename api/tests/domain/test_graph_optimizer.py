"""依赖图规则、依赖图排程与多批次顺序搜索。"""
from datetime import datetime, timedelta, timezone

from app.domain import graph
from app.domain.capability import StationSpec
from app.domain.optimizer import Candidate, search
from app.domain.recipe_rules import validate_steps
from app.domain.scheduling import WORK, SchedulingContext, plan_steps

T0 = datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)
LINEAR = [{"step_id": f"s{i}", "name": f"步 {i}", "dur": 10} for i in range(1, 4)]


def test_linear_recipe_keeps_the_old_chain():
    assert not graph.graph_mode(LINEAR)
    assert graph.predecessors(LINEAR) == [[], [0], [1]]


def test_after_declares_forks_and_joins_undeclared_steps_follow_the_previous_row():
    steps = [
        {"step_id": "a", "dur": 10},
        {"step_id": "b", "dur": 20},             # 未声明：依赖上一行 a
        {"step_id": "c", "dur": 40, "after": ["a"]},
        {"step_id": "d", "dur": 5, "after": ["b", "c"]},
    ]
    assert graph.predecessors(steps) == [[], [0], [0], [1, 2]]
    assert graph.successors(steps)[0] == [1, 2]
    assert graph.critical_path_min(steps) == 55
    assert graph.ready_after(steps, {"a"}, {"a"}) == [1, 2]
    assert graph.ready_after(steps, {"a", "b"}, {"a", "b", "c"}) == [], "c 进行中，汇合 d 要等"
    assert graph.ready_after(steps, {"a", "b", "c"}, {"a", "b", "c"}) == [3]


def test_failed_branch_is_never_reopened_by_another_branch_completing():
    steps = [{"step_id": "a"}, {"step_id": "b", "after": ["a"]}, {"step_id": "c", "after": ["a"]}]
    # b 失败（已尝试、未完成），c 完成：不能把 b 重新开出来
    assert graph.ready_after(steps, {"a", "c"}, {"a", "b", "c"}) == []


def test_graph_issues_reject_forward_self_and_unknown_references():
    steps = [
        {"step_id": "a", "after": ["b"]},
        {"step_id": "b", "after": ["b", "zz"]},
    ]
    issues = graph.graph_issues(steps)
    assert "排在本步之后" in issues[0][0]
    assert issues[1] == ["步骤不能依赖自己", "前驱步骤 zz 不存在"]
    rows = validate_steps(
        [{"step_id": "a", "name": "人工", "kind": "manual", "dur": 5, "form": [{"key": "x", "label": "x"}],
          "after": ["q"]}],
        [], {},
    )
    assert "前驱步骤 q 不存在" in rows[0]["issues"]


def test_parallel_branches_are_scheduled_side_by_side_and_the_join_waits():
    stations = [
        StationSpec(id="A", limits={"cap.a": {}}),
        StationSpec(id="B", limits={"cap.b": {}}),
    ]
    steps = [
        {"step_id": "s1", "name": "起点", "cap": "cap.a", "dur": 10},
        {"step_id": "s2", "name": "分支 A", "cap": "cap.a", "dur": 20},
        {"step_id": "s3", "name": "分支 B", "cap": "cap.b", "dur": 40, "after": ["s1"]},
        {"step_id": "s4", "name": "汇合", "cap": "cap.a", "dur": 5, "after": ["s2", "s3"]},
    ]
    context = SchedulingContext(stations=stations, transfer_min=10, clean_min=0)
    work = {a.step_index: a for a in plan_steps(steps, T0, context) if a.kind == WORK}
    assert work[1].starts_at == T0 + timedelta(minutes=10)
    assert work[2].starts_at == T0 + timedelta(minutes=20), "换到 B 要先转运 10 min，但不等分支 A"
    assert work[3].starts_at == T0 + timedelta(minutes=70), "汇合等最晚的分支 B（60）并转运回 A（+10）"


def _toy(durations: dict[str, int]):
    """单工位：跨度与顺序无关，加权完成时间让短的先做更好。"""
    def evaluate(order):
        clock, weighted = 0, 0
        for batch in order:
            clock += durations[batch]
            weighted += clock
        return Candidate(tuple(order), True, clock, weighted)
    return evaluate


def test_search_is_exhaustive_for_few_batches():
    report = search(["a", "b", "c"], _toy({"a": 30, "b": 10, "c": 20}))
    assert report.method == "exhaustive" and report.evaluated == 6
    assert report.best.order == ("b", "c", "a")


def test_local_search_improves_on_the_seed_for_many_batches():
    durations = {f"b{i}": d for i, d in enumerate([50, 40, 30, 20, 10, 60, 5, 45], start=1)}
    worst = tuple(sorted(durations, key=lambda b: -durations[b]))
    evaluate = _toy(durations)
    report = search(list(durations), evaluate, seeds=[worst], budget_sec=2, max_evaluations=3000)
    assert report.method == "local_search"
    assert report.best.weighted_min < evaluate(worst).weighted_min
    assert report.best.order == tuple(sorted(durations, key=lambda b: durations[b])), "单机加权完成时间最优是最短优先"


def test_infeasible_candidates_never_win():
    def evaluate(order):
        if order[0] == "x":
            return Candidate(tuple(order), False, reason="硬时限")
        return Candidate(tuple(order), True, 10, len(order))
    assert search(["x", "y"], evaluate).best.order == ("y", "x")
