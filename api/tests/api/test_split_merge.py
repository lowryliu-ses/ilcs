"""拆分份数按样本取、样本合并：合成后按样本分份，再把同一条件组的份合成一个，谱系指回全部母样。

批次从 EP-205-01 建出来（8 个样本、8 个条件组），把快照改成：干燥 → 称重 → 拆分（份数取方案因子「FEC 含量」的
水平）→ 合并（同一条件组合成一个）→ 充放电测试。
"""
from sqlalchemy.orm.attributes import flag_modified

from test_flow_control import _run, _runs
from test_graph_workflow import _dispatch, _graph_batch

from app.domain.recipe_rules import step_issues
from app.domain.steps import merge_issues, split_issues


def _shape(steps):
    dry, weigh, assemble, test = steps
    split = {"step_id": "x1", "name": "按产率分份", "kind": "split", "after": [weigh["step_id"]], "dur": 0,
             "split": {"count_from": {"factor": "FEC 含量"}, "child_type": "分份"}}
    merge = {"step_id": "m1", "name": "同条件合并", "kind": "merge", "after": ["x1"], "dur": 0,
             "merge": {"by": "condition", "child_type": "合并液"}}
    return [dry, {**weigh, "after": [dry["step_id"]]}, split, merge, {**test, "after": ["m1"]}]


def test_rules_for_runtime_counts_and_merge():
    assert split_issues({"split": {"count_from": {"factor": "FEC 含量"}, "child_type": "分份"}}) == []
    assert split_issues({"split": {"count_from": {"source_step_id": "s02", "field": "aliquots"}, "count": 1,
                                   "child_type": "分份"}}) == []
    assert any("要写方案因子" in item for item in split_issues({"split": {"count_from": {"field": "x"}, "child_type": "a"}}))
    assert split_issues({"split": {"count": 1, "child_type": "a"}}) == ["拆分份数必须是 2–96 的整数"], "固定份数照旧至少 2"
    assert merge_issues({"merge": {"by": "condition", "child_type": "合并液"}}) == []
    assert len(merge_issues({"merge": {"by": "pairs"}})) == 2
    assert step_issues({"kind": "merge", "name": "合并", "dur": 0, "merge": {"by": "all", "child_type": "粗品"}}, {}) == []


def _set_levels(db, batch_id, counts):
    from app.models import Sample

    rows = sorted(db.query(Sample).filter(Sample.batch_id == batch_id).all(), key=lambda row: row.well)
    for row, count in zip(rows, counts):
        levels = list(row.levels or [])
        levels[0] = count
        row.levels = levels
        flag_modified(row, "levels")
    db.commit()
    return rows


def test_split_counts_follow_each_sample_and_merge_rejoins_them(operator, reset_runtime, db, executor):
    from app.models import PhysicalSample, Sample, StepRun

    batch_id = _graph_batch(operator, db, _shape)
    parents = _set_levels(db, batch_id, [1, 2, 3, 1, 2, 3, 1, 2])
    expected = {row.id: count for row, count in zip(parents, [1, 2, 3, 1, 2, 3, 1, 2])}
    _dispatch(operator, batch_id)
    detail = _run(operator, batch_id, executor, rounds=40)
    assert detail["state"] == "done", detail["failure_reason"]

    db.expire_all()
    split = db.query(StepRun).filter(StepRun.batch_id == batch_id, StepRun.step_id == "x1").one()
    assert split.form_data["counts"] == expected and len(split.form_data["children"]) == sum(expected.values())
    assert "按样本分为 1–3 个分份" in split.reason
    merge = db.query(StepRun).filter(StepRun.batch_id == batch_id, StepRun.step_id == "m1").one()
    merged = [db.get(Sample, child_id) for child_id in merge.form_data["children"]]
    assert len(merged) == 8, "8 个条件组各合成一个"
    pieces = [db.get(Sample, child_id) for child_id in split.form_data["children"]]
    assert {row.state for row in pieces} == {"merged"}, "分份并入合并样后不再是处理对象"
    biggest = next(row for row in merged if len(merge.form_data["groups"][row.id]) == 3)
    physical = db.get(PhysicalSample, biggest.physical_sample_id)
    assert len(physical.parent_ids) == 3 and physical.parent_id == physical.parent_ids[0]
    assert biggest.condition_group == db.get(Sample, merge.form_data["groups"][biggest.id][0]).condition_group

    shown = operator.get(f"/api/samples/{physical.id}").json()
    assert sorted(row["id"] for row in shown["parents"]) == sorted(physical.parent_ids)
    second = operator.get(f"/api/samples/{physical.parent_ids[1]}").json()
    assert physical.id in {row["id"] for row in second["children"]}, "不是第一个母样也能找到合并样"
    assert _runs(detail, detail["snapshot"]["steps"][-1]["step_id"])[-1]["state"] == "completed"


def test_counts_that_are_not_whole_numbers_hold_the_split(operator, reset_runtime, db, executor):
    batch_id = _graph_batch(operator, db, _shape)
    _set_levels(db, batch_id, [0, 2, 2, 2, 2, 2, 2, 2])  # 0 份没法拆
    _dispatch(operator, batch_id)
    held = _run(operator, batch_id, executor, rounds=30, until=("paused", "done", "fault"))
    assert held["state"] == "paused" and "拆分份数不是 1–96 的整数" in held["failure_reason"], held["failure_reason"]


def test_counts_from_a_reading_the_device_marked_unreliable_hold_the_split(
    operator, reset_runtime, db, executor, monkeypatch,
):
    """份数取上游设备每孔的读数：读数都是合法整数，但设备回执质量 bad——不拿它定份数，也不退回缺省份数，拆分保持。"""
    from dataclasses import replace

    from app.adapters.drivers.simulation import SimulationAdapter

    def shape(steps):
        shaped = _shape(steps)
        shaped[2] = {**shaped[2], "split": {"count_from": {"source_step_id": steps[1]["step_id"], "field": "aliquots"},
                                            "count": 2, "child_type": "分份"}}
        return shaped

    batch_id = _graph_batch(operator, db, shape)
    weigh_id = operator.get(f"/api/batches/{batch_id}").json()["snapshot"]["steps"][1]["step_id"]
    original = SimulationAdapter.submit

    def submit(self, request):
        result = original(self, request)
        if request.step_id != weigh_id:
            return result
        patched = replace(result, quality="bad", delivered={**result.delivered, "wells": {
            well: {"aliquots": 2} for well in request.wells}})
        self._ledger[request.command_id] = patched
        return patched

    monkeypatch.setattr(SimulationAdapter, "submit", submit)
    _dispatch(operator, batch_id)
    held = _run(operator, batch_id, executor, rounds=30, until=("paused", "done", "fault"))
    assert held["state"] == "paused" and "读数设备标为不可信" in held["failure_reason"], held["failure_reason"]
