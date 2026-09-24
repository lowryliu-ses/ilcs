"""执行前仿真的图规则：分支路径枚举、不可达节点、回环最坏情况。"""
from app.domain import dryrun


def _device(step_id, after=None, dur=10, **extra):
    row = {"step_id": step_id, "name": step_id, "kind": "device", "cap": "cap.x", "params": {}, "dur": dur}
    if after is not None:
        row["after"] = list(after)
    return {**row, **extra}


def _branch(after, cases, **config):
    return {"step_id": "b1", "name": "分支", "kind": "branch", "after": list(after), "dur": 0,
            "branch": {"mode": "manual", "cases": cases, **config}}


def test_each_exit_is_a_path_with_its_own_duration():
    steps = [
        _device("s01"),
        _branch(["s01"], [{"key": "fast", "label": "快"}, {"key": "slow", "label": "慢"}]),
        _device("s02", ["b1"], dur=5, when={"b1": "fast"}),
        _device("s03", ["b1"], dur=50, when={"b1": "slow"}),
        _device("s04", ["s02", "s03"]),
    ]
    found, truncated = dryrun.paths(steps)
    assert not truncated and len(found) == 2
    durations = sorted(path.duration_min for path in found)
    assert durations == [25, 70], "快路 10+5+10，慢路 10+50+10"
    assert dryrun.unreachable(steps, found) == []


def test_a_step_no_exit_can_reach_is_reported():
    steps = [
        _device("s01"),
        _branch(["s01"], [{"key": "a", "label": "A"}, {"key": "b", "label": "B"}]),
        _device("s02", ["b1"], when={"b1": "a"}),
        _device("s03", ["b1"], when={"b1": "gone"}),
    ]
    found, _ = dryrun.paths(steps)
    assert dryrun.unreachable(steps, found) == [3]


def test_loop_worst_case_counts_the_body_times_the_limit():
    steps = [
        _device("s01", dur=20), _device("s02", ["s01"], dur=10),
        _branch(["s02"], [{"key": "again", "label": "重做", "loop_to": "s01"}, {"key": "ok", "label": "合格"}], max_loops=3),
        _device("s03", ["b1"], when={"b1": "ok"}),
    ]
    rows = dryrun.loop_overhead(steps)
    assert rows[0]["max_loops"] == 3 and rows[0]["extra_min_worst"] == 90
