"""按样本分流的分支：每个样本按自己孔位上的读数走自己的路，两条路各只处理分到的样本，汇合后又是全部样本。

批次从 EP-205-01 建出来（8 个样本，注液量 50 / 60 μL 两个水平），把快照改成：注液组装按孔位下发注液量 →
按注液量分流（≤ 55 走「低注液量测试」、> 55 走「高注液量复称」）→ 静置汇合。
"""
from sqlalchemy.orm.attributes import flag_modified

from test_flow_control import _run, _runs
from test_graph_workflow import _dispatch, _graph_batch

from app.domain import graph
from app.domain.steps import branch_issues


def _shape(steps):
    dry, weigh, assemble, test = steps
    branch = {
        "step_id": "b1", "name": "按注液量分流", "kind": "branch", "after": [assemble["step_id"]], "dur": 0,
        "branch": {"mode": "measure", "per_sample": True, "source_step_id": assemble["step_id"], "field": "electrolyte",
                   "cases": [{"key": "low", "label": "低注液量", "max": 55}, {"key": "high", "label": "高注液量", "min": 55.01}]},
    }
    low = {**test, "step_id": "t1", "name": "低注液量测试", "after": ["b1"], "when": {"b1": "low"}}
    high = {**{key: value for key, value in weigh.items() if key != "hard"}, "step_id": "t2", "name": "高注液量复称",
            "after": ["b1"], "when": {"b1": "high"}}
    join = {"step_id": "w9", "name": "静置", "kind": "wait", "dur": 0.01, "after": ["t1", "t2"], "wait_for": {"mode": "duration"}}
    return [dry, {**weigh, "after": [dry["step_id"]]}, {**assemble, "after": [weigh["step_id"]]}, branch, low, high, join]


def test_scopes_and_validation_rules():
    steps = _shape([
        {"step_id": "s01", "kind": "device", "cap": "cap.vacuum_dry"}, {"step_id": "s02", "kind": "device", "cap": "cap.weigh"},
        {"step_id": "s03", "kind": "device", "cap": "cap.assemble"}, {"step_id": "s04", "kind": "device", "cap": "cap.test"},
    ])
    ids = [step["step_id"] for step in steps]
    assert graph.sample_scopes(steps, ids.index("t1")) == {"b1": {"low"}}
    assert graph.sample_scopes(steps, ids.index("t2")) == {"b1": {"high"}}
    assert graph.sample_scopes(steps, ids.index("w9")) == {}, "汇合之后不再限定"
    status = {step_id: "completed" for step_id in ids[:4]}
    opened, pruned = graph.frontier(steps, status, {"b1": ["low", "high"]})
    assert [ids[index] for index in opened] == ["t1", "t2"] and pruned == []
    opened, pruned = graph.frontier(steps, status, {"b1": ["high"]})
    assert [ids[index] for index in opened] == ["t2"] and [ids[index] for index in pruned] == ["t1"]
    looped = {**steps[3], "branch": {**steps[3]["branch"], "cases": [*steps[3]["branch"]["cases"][:1],
                                                                       {"key": "again", "label": "重做", "max": 1, "loop_to": "s02"}],
                                     "max_loops": 2}}
    assert any("不能回环" in item for item in branch_issues(looped, steps, 3))
    manual = {**steps[3], "branch": {**steps[3]["branch"], "mode": "manual"}}
    assert any("只能按上游设备的测量值" in item for item in branch_issues(manual, steps, 3))


def test_each_sample_takes_its_own_path_and_the_paths_only_process_their_samples(operator, reset_runtime, db, executor):
    from app.models import Batch, Command

    batch_id = _graph_batch(operator, db, _shape)
    batch = db.get(Batch, batch_id)
    assemble_id = batch.recipe_snapshot["steps"][2]["step_id"]
    plan = dict(batch.plan_snapshot)
    factors = [dict(row) for row in plan["factors"]]
    factors[1]["target"] = {"step_id": assemble_id, "param": "electrolyte"}
    samples = operator.get(f"/api/batches/{batch_id}").json()["samples"]
    plan["factors"] = factors
    plan["condition_params"] = {assemble_id: {row["well"]: {"electrolyte": row["levels"][1]} for row in samples}}
    batch.plan_snapshot = plan
    flag_modified(batch, "plan_snapshot")
    db.commit()

    _dispatch(operator, batch_id)
    detail = _run(operator, batch_id, executor, rounds=40)
    assert detail["state"] == "done", detail["failure_reason"]
    branch = _runs(detail, "b1")[-1]
    assert branch["state"] == "completed"
    low = {row["id"] for row in samples if row["levels"][1] == 50}
    high = {row["id"] for row in samples if row["levels"][1] == 60}
    db.expire_all()
    from app.models import StepRun

    run = db.query(StepRun).filter(StepRun.batch_id == batch_id, StepRun.step_id == "b1").one()
    assert {case: set(ids) for case, ids in run.form_data["per_sample"].items()} == {"low": low, "high": high}
    assert run.form_data["cases"] == ["low", "high"]
    wells = {row["id"]: row["well"] for row in samples}
    steps = detail["snapshot"]["steps"]
    index = {step["step_id"]: position for position, step in enumerate(steps)}
    commands = db.query(Command).filter(Command.batch_id == batch_id, Command.type == "dispatch").all()
    sent = {command.step_index: set((command.params or {}).get("wells") or {}) for command in commands}
    assert sent[index["t1"]] == {wells[sample] for sample in low}, "低注液量测试只下发分到这条路的孔位"
    assert sent[index["t2"]] == {wells[sample] for sample in high}
    assert _runs(detail, "w9")[-1]["state"] == "completed", "两条路都走完才汇合"


def test_samples_matching_no_exit_hold_the_branch_until_qa_routes_them(operator, qa, reset_runtime, db, executor):
    """60 μL 的样本落在两个出口之间、又没有默认出口：分支保持；QA 给这些样本选出口后，照读数分好的样本不变。"""
    from app.models import Batch, StepRun

    def gap(steps):
        shaped = _shape(steps)
        shaped[3] = {**shaped[3], "branch": {**shaped[3]["branch"], "cases": [
            {"key": "low", "label": "低注液量", "max": 55}, {"key": "high", "label": "高注液量", "min": 65},
        ]}}
        return shaped

    batch_id = _graph_batch(operator, db, gap)
    batch = db.get(Batch, batch_id)
    assemble_id = batch.recipe_snapshot["steps"][2]["step_id"]
    samples = operator.get(f"/api/batches/{batch_id}").json()["samples"]
    plan = dict(batch.plan_snapshot)
    plan["factors"] = [dict(row) for row in plan["factors"]]
    plan["factors"][1]["target"] = {"step_id": assemble_id, "param": "electrolyte"}
    plan["condition_params"] = {assemble_id: {row["well"]: {"electrolyte": row["levels"][1]} for row in samples}}
    batch.plan_snapshot = plan
    flag_modified(batch, "plan_snapshot")
    db.commit()

    _dispatch(operator, batch_id)
    held = _run(operator, batch_id, executor, rounds=30, until=("paused", "done", "fault"))
    assert held["state"] == "paused" and "4 个样本" in held["failure_reason"], held["failure_reason"]
    branch = _runs(held, "b1")[-1]
    decided = qa.post(f"/api/step-runs/{branch['id']}/branch-decision", {
        "case": "high", "reason": "60 μL 按高注液量处理", "row_version": branch["row_version"],
        "signature_id": qa.sign("分支判定属实", target=branch["id"]),
    })
    assert decided.status_code == 200, decided.text
    detail = _run(operator, batch_id, executor, rounds=40)
    assert detail["state"] == "done", detail["failure_reason"]
    db.expire_all()
    run = db.query(StepRun).filter(StepRun.batch_id == batch_id, StepRun.step_id == "b1").one()
    low = {row["id"] for row in samples if row["levels"][1] == 50}
    assert set(run.form_data["per_sample"]["low"]) == low and len(run.form_data["per_sample"]["high"]) == 4
