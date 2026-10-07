"""同一指标被两个设备步骤关联（2026-10-07 通用中控评审 F2）：发布与建批时拦下，同一步重做、互斥分支上的两步不算。"""
from app.domain.graph import branch_reach, exclusive_paths
from app.domain.methods import shared_metric_problems


def _device(step_id: str, *metrics: str, **extra) -> dict:
    outputs = [{"key": f"k{index}", "metric_id": metric} for index, metric in enumerate(metrics)]
    return {"step_id": step_id, "name": step_id.upper(), "kind": "device", "cap": "cap.x",
            "method": {"code": "M", "version": 1, "outputs": outputs}, **extra}


def test_the_later_of_two_steps_measuring_the_same_metric_is_flagged():
    steps = [_device("s01", "ph"), {"step_id": "s02", "name": "加热", "kind": "manual"}, _device("s03", "ph", "cond")]
    problems = shared_metric_problems(steps, names={"ph": "pH"})
    assert list(problems) == ["s03"] and "指标 pH 已由第 1 步「S01」" in problems["s03"][0]
    assert shared_metric_problems([_device("s01", "ph"), _device("s02", "cond")]) == {}


def test_a_curve_and_the_number_it_derives_count_as_the_same_metric():
    steps = [_device("s01", "curve"), _device("s02", "cap_end")]
    problems = shared_metric_problems(steps, derived={"curve": ["cap_end"]}, names={"cap_end": "截止容量"})
    assert "指标 截止容量 已由第 1 步" in problems["s02"][0]


def test_steps_on_different_exits_of_one_branch_never_measure_the_same_sample():
    gate = {"step_id": "b1", "name": "分流", "kind": "branch", "after": []}
    left = _device("s02", "ph", after=["b1"], when={"b1": "small"})
    right = _device("s03", "ph", after=["b1"], when={"b1": "large"})
    joined = _device("s04", "ph", after=["s02", "s03"])
    steps = [gate, left, right, joined]
    assert branch_reach(steps, 3) == {"b1": {"small", "large"}}
    assert exclusive_paths(steps, 1, 2) and not exclusive_paths(steps, 1, 3)
    problems = shared_metric_problems(steps)
    assert list(problems) == ["s04"], "两个出口各测一次不冲突；汇合之后再测就和两边都冲突"


def test_parallel_forks_both_run_so_they_clash():
    steps = [{"step_id": "s01", "name": "起点", "kind": "manual", "after": []},
             _device("s02", "ph", after=["s01"]), _device("s03", "ph", after=["s01"])]
    assert not exclusive_paths(steps, 1, 2)
    assert list(shared_metric_problems(steps)) == ["s03"]
