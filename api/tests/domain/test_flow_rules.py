"""流程控制的纯规则：条件分支的死路剪除、回环体封闭、出口匹配、子流程展开、步骤级超时。"""
import pytest

from app.domain import graph
from app.domain.steps import branch_issues, match_case, timeout_issues
from app.domain.subflow import SubflowError, SubflowRecipe, expand, merge_bom


def _branch(step_id="b1", after=("s01",), cases=None, **config):
    return {
        "step_id": step_id, "name": "分支", "kind": "branch", "after": list(after),
        "branch": {
            "mode": config.pop("mode", "measure"), "source_step_id": config.pop("source", "s01"),
            "field": config.pop("field", "mass"),
            "cases": cases or [
                {"key": "high", "label": "偏高", "min": 10},
                {"key": "low", "label": "偏低", "max": 10},
            ],
            **config,
        },
    }


def _device(step_id, after=None, **extra):
    row = {"step_id": step_id, "name": step_id, "kind": "device", "cap": "cap.x", "params": {}, "dur": 10}
    if after is not None:
        row["after"] = list(after)
    return {**row, **extra}


def _xor_flow():
    """s01 → 分支 → (high: s02 | low: s03) → s04 汇合。"""
    return [
        _device("s01"),
        _branch(),
        _device("s02", ["b1"], when={"b1": "high"}),
        _device("s03", ["b1"], when={"b1": "low"}),
        _device("s04", ["s02", "s03"]),
    ]


def test_branch_prunes_the_path_not_taken_and_join_waits_only_for_the_taken_one():
    steps = _xor_flow()
    status = {"s01": "completed", "b1": "completed"}
    opened, pruned = graph.frontier(steps, status, {"b1": "high"})
    assert [steps[i]["step_id"] for i in opened] == ["s02"]
    assert [steps[i]["step_id"] for i in pruned] == ["s03"]

    status.update({"s02": "completed", "s03": "not_taken"})
    opened, pruned = graph.frontier(steps, status, {"b1": "high"})
    assert [steps[i]["step_id"] for i in opened] == ["s04"], "汇合只等真正走到的那条路"
    assert pruned == []


def test_parallel_join_still_waits_for_every_predecessor():
    steps = [_device("s01"), _device("s02", ["s01"]), _device("s03", ["s01"]), _device("s04", ["s02", "s03"])]
    opened, _ = graph.frontier(steps, {"s01": "completed", "s02": "completed", "s03": "running"}, {})
    assert opened == [], "没有 when 的并行汇合要等全部前驱"


def test_pruning_cascades_until_a_live_edge_is_found():
    steps = [
        _device("s01"), _branch(),
        _device("s02", ["b1"], when={"b1": "low"}), _device("s03", ["s02"]),
        _device("s04", ["b1"], when={"b1": "high"}),
    ]
    opened, pruned = graph.frontier(steps, {"s01": "completed", "b1": "completed"}, {"b1": "high"})
    assert {steps[i]["step_id"] for i in pruned} == {"s02", "s03"}, "死路一路向下剪"
    assert [steps[i]["step_id"] for i in opened] == ["s04"]


def test_failed_or_unknown_steps_are_never_reopened_by_the_frontier():
    steps = [_device("s01"), _device("s02", ["s01"])]
    assert graph.frontier(steps, {"s01": "completed", "s02": "failed"}, {}) == ([], [])
    assert graph.frontier(steps, {"s01": "unknown"}, {}) == ([], [])


def test_every_branch_successor_must_say_which_exit_it_is_on():
    steps = _xor_flow()
    del steps[3]["when"]
    issues = graph.graph_issues(steps)
    assert any("必须指定走哪个出口" in text for text in issues[3])
    steps = _xor_flow()
    steps[2]["when"] = {"b1": "nope"}
    assert any("没有可前进的出口" in text for text in graph.graph_issues(steps)[2])
    steps = _xor_flow()
    steps[4]["when"] = {"s02": "high"}
    assert any("不是条件分支" in text for text in graph.graph_issues(steps)[4])


def test_loop_body_must_be_closed():
    """回环体内的步骤不能有通往回环外的后继：重做时体外步骤会失去前提。"""
    steps = [
        _device("s01"), _device("s02", ["s01"]),
        _branch(after=("s02",), source="s02", cases=[
            {"key": "again", "label": "重做", "min": 10, "loop_to": "s01"},
            {"key": "ok", "label": "合格", "max": 10},
        ], max_loops=3),
        _device("s03", ["b1"], when={"b1": "ok"}),
    ]
    assert graph.graph_issues(steps) == {}
    leaky = [*steps, _device("s09", ["s01"])]
    issues = graph.graph_issues(leaky)
    assert any("回环外的后继" in text for text in issues.get(2, []))


def test_branch_configuration_rules():
    steps = [_device("s01"), _branch()]
    assert branch_issues(steps[1], steps, 1) == []
    one_case = _branch(cases=[{"key": "only", "label": "唯一", "min": 1}])
    assert any("至少要有两个出口" in text for text in branch_issues(one_case, [steps[0], one_case], 1))
    looping = _branch(cases=[
        {"key": "a", "label": "A", "min": 1, "loop_to": "s01"}, {"key": "b", "label": "B", "max": 1, "loop_to": "s01"},
    ])
    problems = branch_issues(looping, [steps[0], looping], 1)
    assert any("最多循环次数" in text for text in problems)
    assert any("不回环的出口" in text for text in problems)
    bad_default = _branch(default="a", cases=[
        {"key": "a", "label": "A", "min": 1, "loop_to": "s01"}, {"key": "b", "label": "B", "max": 1},
    ], max_loops=2)
    assert any("默认出口不能是回环" in text for text in branch_issues(bad_default, [steps[0], bad_default], 1))
    wrong_source = [{"step_id": "s01", "name": "人工", "kind": "manual", "form": [{"key": "x", "label": "x"}]}, _branch()]
    assert any("来源必须是设备步骤" in text for text in branch_issues(wrong_source[1], wrong_source, 1))


def test_case_matching_in_order_with_default_and_missing_values():
    step = _branch(default="low")
    assert match_case(step, 12) == "high"
    assert match_case(step, 3) == "low"
    assert match_case(step, None) is None, "取不到判据不走默认出口：缺数据要人来判断"
    no_default = _branch()
    assert match_case(no_default, "abc") is None
    enum = _branch(mode="form", cases=[{"key": "y", "label": "是", "equals": "合格"}, {"key": "n", "label": "否", "equals": "不合格"}])
    assert match_case(enum, "不合格") == "n"


def test_timeout_rules_keep_physical_steps_alarm_only():
    device = _device("s01", timeout={"minutes": 5, "action": "fail"})
    assert any("只能报警" in text for text in timeout_issues(device))
    assert timeout_issues(_device("s01", timeout={"minutes": 5, "action": "alarm"})) == []
    manual = {"kind": "manual", "timeout": {"minutes": 5, "action": "skip"}}
    assert any("允许跳过" in text for text in timeout_issues(manual))
    assert timeout_issues({**manual, "skippable": True}) == []
    fixed_wait = {"kind": "wait", "dur": 5, "wait_for": {"mode": "duration"}, "timeout": {"minutes": 5}}
    assert any("不需要超时" in text for text in timeout_issues(fixed_wait))


# ---------- 子流程 ----------


def _resolver(**recipes):
    def resolve(recipe_id):
        return recipes.get(recipe_id)

    return resolve


def _sub(recipe_id, steps, state="released", bom=None):
    return SubflowRecipe(id=recipe_id, name=f"子方法 {recipe_id}", version="1.0.0", state=state, steps=steps, bom=bom or [])


def test_subflow_expands_in_place_with_prefixed_ids_and_rewired_edges():
    inner = [
        _device("s01"), _device("s02", ["s01"]), _device("s03", ["s01"]),
        {"step_id": "s04", "name": "关卡", "kind": "gate", "after": ["s02"],
         "gate": {"source_step_id": "s02", "rework_to": "s01", "field": "x", "min": 1, "on_fail": "rework", "max_rework": 1}},
    ]
    parent = [
        _device("s01"),
        {"step_id": "s02", "name": "前处理子流程", "kind": "subflow", "subflow": {"recipe_id": "R-SUB"}},
        _device("s03"),
    ]
    steps, bom = expand(parent, _resolver(**{"R-SUB": _sub("R-SUB", inner, bom=[{"material": "A", "qty": 1, "unit": "g"}])}), ("R-P",))
    ids = [step["step_id"] for step in steps]
    assert ids == ["s01", "s02.s01", "s02.s02", "s02.s03", "s02.s04", "s03"]
    by_id = {step["step_id"]: step for step in steps}
    assert by_id["s02.s01"]["after"] == ["s01"], "子方法起点接到子流程节点的前驱上"
    assert sorted(by_id["s03"]["after"]) == ["s02.s03", "s02.s04"], "后继等子方法的全部终点"
    assert by_id["s02.s04"]["gate"]["source_step_id"] == "s02.s02"
    assert by_id["s02.s04"]["gate"]["rework_to"] == "s02.s01"
    assert by_id["s02.s02"]["groups"][0]["recipe_id"] == "R-SUB"
    assert bom == [{"material": "A", "qty": 1, "unit": "g"}]
    assert graph.graph_issues(steps) == {}


def test_subflow_after_a_branch_keeps_the_exit_condition_on_its_roots():
    parent = [
        _device("s01"), _branch(),
        {"step_id": "s02", "name": "补做", "kind": "subflow", "after": ["b1"], "when": {"b1": "low"},
         "subflow": {"recipe_id": "R-SUB"}},
        _device("s03", ["b1"], when={"b1": "high"}),
    ]
    steps, _ = expand(parent, _resolver(**{"R-SUB": _sub("R-SUB", [_device("s01")])}), ("R-P",))
    root = next(step for step in steps if step["step_id"] == "s02.s01")
    assert root["after"] == ["b1"] and root["when"] == {"b1": "low"}


def test_subflow_references_are_checked():
    parent = [{"step_id": "s01", "name": "子", "kind": "subflow", "subflow": {"recipe_id": "R-A"}}]
    with pytest.raises(SubflowError, match="不存在"):
        expand(parent, _resolver(), ("R-P",))
    with pytest.raises(SubflowError, match="已发布"):
        expand(parent, _resolver(**{"R-A": _sub("R-A", [_device("s01")], state="draft")}), ("R-P",))
    cyclic = {"R-A": _sub("R-A", [{"step_id": "s01", "name": "回", "kind": "subflow", "subflow": {"recipe_id": "R-P"}}])}
    with pytest.raises(SubflowError, match="循环引用"):
        expand(parent, _resolver(**cyclic), ("R-P",))
    chain = {
        f"R-{n}": _sub(f"R-{n}", [{"step_id": "s01", "name": "下", "kind": "subflow", "subflow": {"recipe_id": f"R-{n + 1}"}}])
        for n in range(1, 6)
    }
    deep = [{"step_id": "s01", "name": "子", "kind": "subflow", "subflow": {"recipe_id": "R-1"}}]
    with pytest.raises(SubflowError, match="嵌套超过"):
        expand(deep, _resolver(**chain), ("R-P",))


def test_bom_merge_adds_quantities_exactly():
    merged = merge_bom(
        [{"material": "LP57", "qty": 0.1, "unit": "mL"}],
        [{"material": "LP57", "qty": 0.2, "unit": "mL"}, {"material": "NMP", "qty": 1, "unit": "L"}],
    )
    assert merged == [{"material": "LP57", "qty": 0.3, "unit": "mL"}, {"material": "NMP", "qty": 1, "unit": "L"}]
