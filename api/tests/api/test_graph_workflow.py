"""依赖图流程的运行时与多批次优化。

- 分叉：一步完成同时开出多个后继；汇合等全部前驱完成，只开一次；全部步骤完成才结束。
- 绑定载具时并行分支上的两个设备步骤不同时占用一块板。
- 优化预览与应用结果一致（应用时先清掉所选批次的旧时间窗）。
"""
import time

from sqlalchemy.orm.attributes import flag_modified

from test_labware_transfer import _register, clean_labware  # noqa: F401


def _graph_batch(operator, db, make_steps) -> str:
    from app.models import Batch

    created = operator.post("/api/batches", {"plan_id": "EP-205-01"})
    assert created.status_code == 201, created.text
    batch_id = created.json()["id"]
    batch = db.get(Batch, batch_id)
    steps = [dict(step) for step in batch.recipe_snapshot["steps"]]
    snapshot = dict(batch.recipe_snapshot)
    snapshot["steps"] = make_steps(steps)
    batch.recipe_snapshot = snapshot
    flag_modified(batch, "recipe_snapshot")
    db.commit()
    return batch_id


def _run(operator, batch_id, executor, rounds: int = 30) -> dict:
    for _ in range(rounds):
        executor()
        detail = operator.get(f"/api/batches/{batch_id}").json()
        if detail["state"] in {"done", "fault", "aborted"}:
            return detail
        time.sleep(0.1)
    return operator.get(f"/api/batches/{batch_id}").json()


def _dispatch(operator, batch_id):
    assert operator.post(f"/api/batches/{batch_id}/schedule", {}).status_code == 200
    dispatched = operator.post(
        f"/api/batches/{batch_id}/dispatch",
        {"manual_review": True, "signature_id": operator.sign("批准执行", target=batch_id)},
    )
    assert dispatched.status_code == 200, dispatched.text


def test_fork_and_join_open_each_step_once_and_finish_only_when_all_done(operator, reset_runtime, db, executor):
    def graph(steps):
        dry, weigh, assemble, test = steps
        wait = {"step_id": "w9", "name": "静置", "kind": "wait", "dur": 0.01, "after": [dry["step_id"]],
                "wait_for": {"mode": "duration"}}
        weigh = {**weigh, "after": [dry["step_id"]]}
        assemble = {**assemble, "after": [weigh["step_id"], "w9"]}
        return [dry, weigh, wait, assemble, test]

    batch_id = _graph_batch(operator, db, graph)
    _dispatch(operator, batch_id)
    detail = _run(operator, batch_id, executor)
    assert detail["state"] == "done", detail["failure_reason"]
    runs = detail["step_runs"]
    by_step = {}
    for row in runs:
        by_step.setdefault(row["step_id"], []).append(row)
    assert all(len(rows) == 1 for rows in by_step.values()), "每一步只开一次，汇合也不例外"
    weigh_id = detail["snapshot"]["steps"][1]["step_id"]
    wait_run, weigh_run = by_step["w9"][0], by_step[weigh_id][0]
    assemble_run = by_step[detail["snapshot"]["steps"][3]["step_id"]][0]
    assert wait_run["started_at"] <= weigh_run["ended_at"], "静置与称重并行：称重完成前静置已开始"
    assert assemble_run["started_at"] >= max(wait_run["ended_at"], weigh_run["ended_at"]), "汇合等两个前驱"


def test_parallel_device_branches_share_one_plate_one_at_a_time(
    operator, clean_labware, db, executor,  # noqa: F811
):
    from app.models import Command

    def graph(steps):
        dry, weigh, assemble, test = steps
        weigh = {**weigh, "after": [dry["step_id"]]}
        assemble = {**{k: v for k, v in assemble.items() if k != "hard"}, "after": [dry["step_id"]]}
        test = {**test, "after": [weigh["step_id"], assemble["step_id"]]}
        return [dry, weigh, assemble, test]

    batch_id = _graph_batch(operator, db, graph)
    labware = _register(operator, "HOTEL-01/S03")
    assert operator.post(f"/api/batches/{batch_id}/labware", {"labware_id": labware["id"]}).status_code == 200
    _dispatch(operator, batch_id)
    detail = _run(operator, batch_id, executor, rounds=40)
    assert detail["state"] == "done", detail["failure_reason"]

    db.expire_all()
    actions = (
        db.query(Command).filter(Command.batch_id == batch_id, Command.type.in_(["dispatch", "transfer"]))
        .order_by(Command.started_at).all()
    )
    for first, second in zip(actions, actions[1:]):
        assert first.updated_at <= second.started_at, "同一块板上的动作（含转运）不能重叠"
    moves = operator.get(f"/api/labware/{labware['id']}/moves").json()
    assert [row["to"] for row in reversed(moves)][-1] == "ST-07/N1"


def test_optimize_preview_matches_what_apply_writes(operator, reset_runtime):
    ids = []
    for _ in range(3):
        created = operator.post("/api/batches", {"plan_id": "EP-205-01"})
        assert created.status_code == 201, created.text
        ids.append(created.json()["id"])
        # 先各自排过：应用优化时它们的旧时间窗必须先让出来，否则第一个批次按旧占用排
        assert operator.post(f"/api/batches/{ids[-1]}/schedule", {}).status_code == 200
    preview = operator.post("/api/schedule/optimize", {"batch_ids": list(reversed(ids))})
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["method"] == "exhaustive" and body["evaluated"] == 6
    assert body["best"]["span_min"] <= body["baseline"]["span_min"]

    applied = operator.post("/api/schedule/optimize/apply", {"order": body["best"]["order"]})
    assert applied.status_code == 200, applied.text
    first = body["best"]["order"][0]
    planned = [row for row in body["best"]["plans"][first] if row["kind"] == "work"]
    written = [
        row for row in operator.get(f"/api/batches/{first}").json()["allocations"] if row["kind"] == "work"
    ]
    assert [(row["station_id"], row["starts_at"][:16]) for row in written] == [
        (row["station_id"], row["starts_at"][:16]) for row in planned
    ], "应用写入的时间窗与操作员确认的预览一致"
