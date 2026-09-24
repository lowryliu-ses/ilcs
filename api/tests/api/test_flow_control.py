"""流程控制的运行时：条件分支与死路剪除、有上限的回环、人工选择出口、业务事件等待、
步骤级超时、运行时跳过、从指定节点重做、子流程展开后执行。

批次都从 EP-205-01（R-205：干燥 s01 → 称重 s02 → 注液组装 s03 → 充放电测试 s04）建出来，
再把快照里的步骤改成要测的流程形状——和 test_graph_workflow 一样，不为每个用例走一遍方法审批。
"""
import time
from datetime import timedelta

from sqlalchemy.orm.attributes import flag_modified

from test_graph_workflow import _dispatch, _graph_batch


def _run(operator, batch_id, executor, rounds=30, until=("done", "fault", "aborted")):
    detail = {}
    for _ in range(rounds):
        executor()
        detail = operator.get(f"/api/batches/{batch_id}").json()
        if detail["state"] in until:
            return detail
        time.sleep(0.05)
    return detail


def _runs(detail, step_id):
    return [row for row in detail["step_runs"] if row["step_id"] == step_id]


def _branch(after, source, cases, **config):
    return {
        "step_id": "b1", "name": "按称重分流", "kind": "branch", "after": list(after), "dur": 0,
        "branch": {"mode": config.pop("mode", "measure"), "source_step_id": source, "field": "mass",
                   "cases": cases, **config},
    }


def _strip_hard(step):
    return {key: value for key, value in step.items() if key != "hard"}


def test_measure_branch_takes_one_path_prunes_the_other_and_returns_its_windows(operator, reset_runtime, db, executor):
    def shape(steps):
        dry, weigh, assemble, test = steps
        reweigh = {**_strip_hard(weigh), "step_id": "r1", "name": "补片重称", "after": ["b1"], "when": {"b1": "light"}}
        return [
            dry, {**weigh, "after": [dry["step_id"]]},
            _branch([weigh["step_id"]], weigh["step_id"], [
                {"key": "ok", "label": "重量合格", "min": 0.01},
                {"key": "light", "label": "偏轻", "max": 0.01},
            ]),
            {**assemble, "after": ["b1"], "when": {"b1": "ok"}},
            reweigh,
            {**test, "after": [assemble["step_id"], "r1"]},
        ]

    batch_id = _graph_batch(operator, db, shape)
    _dispatch(operator, batch_id)
    before = operator.get(f"/api/batches/{batch_id}").json()
    reweigh_index = next(row["index"] for row in before["steps"] if row["step_id"] == "r1")
    assert any(a["step_index"] == reweigh_index for a in before["allocations"]), "两条路都先预约了工位"

    detail = _run(operator, batch_id, executor)
    assert detail["state"] == "done", detail["failure_reason"]
    branch = _runs(detail, "b1")[-1]
    assert branch["state"] == "completed" and branch["conclusion"] == "ok"
    assert [row["state"] for row in _runs(detail, "r1")] == ["not_taken"]
    assert not any(a["step_index"] == reweigh_index for a in detail["allocations"]), "没走的路归还时间窗"
    test_id = detail["snapshot"]["steps"][-1]["step_id"]
    assert _runs(detail, test_id)[-1]["state"] == "completed", "汇合只等走到的那条路"


def test_loop_repeats_up_to_the_limit_then_waits_for_qa(operator, qa, reset_runtime, db, executor):
    def shape(steps):
        dry, weigh, assemble, test = steps
        return [
            dry, {**weigh, "after": [dry["step_id"]]},
            _branch([weigh["step_id"]], weigh["step_id"], [
                {"key": "again", "label": "重称", "min": 0.015, "loop_to": weigh["step_id"]},
                {"key": "pass", "label": "合格", "max": 0.015},
            ], max_loops=2),
            {**_strip_hard(assemble), "after": ["b1"], "when": {"b1": "pass"}},
            {**test, "after": [assemble["step_id"]]},
        ]

    batch_id = _graph_batch(operator, db, shape)
    _dispatch(operator, batch_id)
    detail = _run(operator, batch_id, executor, until=("paused", "done", "fault"))
    assert detail["state"] == "paused", "称重值一直落在回环出口：回环两次后转人工"
    weigh_id = detail["snapshot"]["steps"][1]["step_id"]
    assert [row["state"] for row in _runs(detail, weigh_id)] == ["superseded", "superseded", "completed"]
    held = _runs(detail, "b1")[-1]
    assert held["state"] == "ready" and "上限" in held["reason"]

    denied = operator.post(f"/api/step-runs/{held['id']}/branch-decision", {
        "case": "pass", "reason": "复核", "signature_id": operator.sign("选择", target=held["id"]),
    })
    assert denied.status_code == 403, "判据落到上限的分支要 QA 判定"
    again = qa.post(f"/api/step-runs/{held['id']}/branch-decision", {
        "case": "again", "reason": "再称一次", "signature_id": qa.sign("选择", target=held["id"]),
    })
    assert again.status_code == 409, "回环已到上限，不能再选回环出口"
    decided = qa.post(f"/api/step-runs/{held['id']}/branch-decision", {
        "case": "pass", "reason": "天平复校后确认 15.2 mg 在工艺窗口内",
        "signature_id": qa.sign("分支判定属实", target=held["id"]),
    })
    assert decided.status_code == 200, decided.text
    detail = _run(operator, batch_id, executor)
    assert detail["state"] == "done", detail["failure_reason"]


def test_manual_branch_is_an_operator_todo(operator, reset_runtime, db, executor):
    def shape(steps):
        dry, weigh, assemble, test = steps
        return [
            dry,
            {"step_id": "b1", "name": "是否需要称重", "kind": "branch", "after": [dry["step_id"]],
             "branch": {"mode": "manual", "cases": [{"key": "weigh", "label": "称重"}, {"key": "skip", "label": "直接组装"}]}},
            {**weigh, "after": ["b1"], "when": {"b1": "weigh"}},
            {**_strip_hard(assemble), "after": ["b1", weigh["step_id"]], "when": {"b1": "skip"}},
            {**test, "after": [assemble["step_id"]]},
        ]

    batch_id = _graph_batch(operator, db, shape)
    _dispatch(operator, batch_id)
    detail = _run(operator, batch_id, executor, rounds=10, until=("never",))
    choice = _runs(detail, "b1")[-1]
    assert choice["state"] == "ready" and detail["state"] == "running"
    assert any(row["id"] == choice["id"] for row in operator.get("/api/step-runs/branches").json())
    wrong = operator.post(f"/api/step-runs/{choice['id']}/branch-decision", {"case": "nope", "reason": "x"})
    assert wrong.status_code == 422
    chosen = operator.post(f"/api/step-runs/{choice['id']}/branch-decision", {
        "case": "skip", "reason": "极片已在上一批次称过", "row_version": choice["row_version"],
    })
    assert chosen.status_code == 200, chosen.text
    detail = _run(operator, batch_id, executor)
    assert detail["state"] == "done", detail["failure_reason"]
    weigh_id = detail["snapshot"]["steps"][2]["step_id"]
    assert [row["state"] for row in _runs(detail, weigh_id)] == ["not_taken"]


def _event_wait(name="sample_received", **extra):
    return {"step_id": "w1", "name": "等样品送达", "kind": "wait", "dur": 30,
            "wait_for": {"mode": "event", "event": name}, **extra}


def test_event_wait_is_woken_by_a_signal_and_early_signals_are_kept(operator, reset_runtime, db, executor):
    def shape(steps):
        dry, weigh, assemble, test = steps
        return [dry, {**_event_wait(), "after": [dry["step_id"]]}, {**_strip_hard(weigh), "after": ["w1"]}, _strip_hard(assemble), test]

    batch_id = _graph_batch(operator, db, shape)
    _dispatch(operator, batch_id)
    detail = _run(operator, batch_id, executor, rounds=8, until=("never",))
    waiting = _runs(detail, "w1")[-1]
    assert waiting["state"] == "waiting", "没有信号就一直等，不会被定时唤醒"
    sent = operator.post(f"/api/batches/{batch_id}/signals", {"name": "sample_received", "event_id": "LIMS-1"})
    assert sent.status_code == 200, sent.text
    replay = operator.post(f"/api/batches/{batch_id}/signals", {"name": "sample_received", "event_id": "LIMS-1"})
    assert replay.json()["replayed"] is True, "同一事件重发不重复登记"
    detail = _run(operator, batch_id, executor)
    assert detail["state"] == "done", detail["failure_reason"]
    assert _runs(detail, "w1")[-1]["form_data"]["signal"] == "sample_received"

    # 早到的信号：等待节点开出之前先到，开出时直接消费
    early = _graph_batch(operator, db, shape)
    _dispatch(operator, early)
    assert operator.post(f"/api/batches/{early}/signals", {"name": "sample_received", "event_id": "LIMS-2"}).status_code == 200
    detail = _run(operator, early, executor)
    assert detail["state"] == "done", detail["failure_reason"]
    signals = operator.get(f"/api/batches/{early}/signals").json()
    assert signals[0]["consumed_by_run_id"] == _runs(detail, "w1")[-1]["id"]


def test_service_identity_needs_the_batch_signals_scope(operator, device, reset_runtime, db, executor):
    from app.models import ServiceIdentity

    def shape(steps):
        dry, weigh, assemble, test = steps
        return [dry, {**_event_wait("qc_released"), "after": [dry["step_id"]]}, {**_strip_hard(weigh), "after": ["w1"]}, _strip_hard(assemble), test]

    batch_id = _graph_batch(operator, db, shape)
    _dispatch(operator, batch_id)
    denied = device.post(f"/api/runtime/batches/{batch_id}/signals", {"name": "qc_released", "event_id": "E-1"})
    assert denied.status_code == 403
    identity = db.query(ServiceIdentity).filter(ServiceIdentity.source == device.source).one()
    original = dict(identity.scopes or {})
    identity.scopes = {**original, "batch_signals": ["qc_released"]}
    flag_modified(identity, "scopes")
    db.commit()
    try:
        allowed = device.post(f"/api/runtime/batches/{batch_id}/signals", {"name": "qc_released", "event_id": "E-1"})
        assert allowed.status_code == 200, allowed.text
        detail = _run(operator, batch_id, executor)
        assert detail["state"] == "done", detail["failure_reason"]
    finally:
        identity.scopes = original
        flag_modified(identity, "scopes")
        db.commit()


def _manual(step_id="m1", **extra):
    return {"step_id": step_id, "name": "人工复核记录", "kind": "manual", "dur": 5, "requires_sample_check": False,
            "form": [{"key": "note", "label": "备注", "type": "text", "required": True}], **extra}


def _expire_deadlines(db, batch_id):
    from app.core.clock import now
    from app.models import StepRun

    db.expire_all()
    for run in db.query(StepRun).filter(StepRun.batch_id == batch_id, StepRun.deadline_at.isnot(None)).all():
        run.deadline_at = now() - timedelta(seconds=1)
    db.commit()


def _timeout_shape(timeout, skippable=False):
    def shape(steps):
        dry, weigh, assemble, test = steps
        return [dry, _manual(timeout=timeout, skippable=skippable), _strip_hard(weigh), _strip_hard(assemble), test]
    return shape


def test_step_timeout_alarm_keeps_the_step_open(operator, reset_runtime, db, executor):
    batch_id = _graph_batch(operator, db, _timeout_shape({"minutes": 1, "action": "alarm"}))
    _dispatch(operator, batch_id)
    _run(operator, batch_id, executor, rounds=6, until=("never",))
    _expire_deadlines(db, batch_id)
    detail = _run(operator, batch_id, executor, rounds=2, until=("never",))
    manual = _runs(detail, "m1")[-1]
    assert manual["state"] == "ready" and manual["timed_out_at"]
    assert any("超过" in alarm["message"] for alarm in detail["alarms"])


def test_step_timeout_fail_sends_the_batch_to_recovery(operator, reset_runtime, db, executor):
    batch_id = _graph_batch(operator, db, _timeout_shape({"minutes": 1, "action": "fail"}))
    _dispatch(operator, batch_id)
    _run(operator, batch_id, executor, rounds=6, until=("never",))
    _expire_deadlines(db, batch_id)
    detail = _run(operator, batch_id, executor, rounds=6, until=("fault",))
    assert detail["state"] == "fault"
    assert _runs(detail, "m1")[-1]["state"] == "failed"


def test_step_timeout_skip_continues_the_flow(operator, reset_runtime, db, executor):
    batch_id = _graph_batch(operator, db, _timeout_shape({"minutes": 1, "action": "skip"}, skippable=True))
    _dispatch(operator, batch_id)
    _run(operator, batch_id, executor, rounds=6, until=("never",))
    _expire_deadlines(db, batch_id)
    detail = _run(operator, batch_id, executor)
    assert detail["state"] == "done", detail["failure_reason"]
    assert _runs(detail, "m1")[-1]["state"] == "skipped"


def _skip_shape(skippable):
    def build(steps):
        dry, weigh, assemble, test = steps
        return [dry, _manual(skippable=skippable), _strip_hard(weigh), _strip_hard(assemble), test]
    return build


def test_steps_not_marked_skippable_cannot_be_skipped(operator, reset_runtime, db, executor):
    batch_id = _graph_batch(operator, db, _skip_shape(False))
    _dispatch(operator, batch_id)
    _run(operator, batch_id, executor, rounds=6, until=("never",))
    refused = operator.post(f"/api/batches/{batch_id}/skip", {
        "step_id": "m1", "reason": "赶进度", "signature_id": operator.sign("跳过", target=batch_id),
    })
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "step_not_skippable"


def test_operator_can_skip_a_step_the_method_allows(operator, reset_runtime, db, executor):
    batch_id = _graph_batch(operator, db, _skip_shape(True))
    _dispatch(operator, batch_id)
    _run(operator, batch_id, executor, rounds=6, until=("never",))
    skipped = operator.post(f"/api/batches/{batch_id}/skip", {
        "step_id": "m1", "reason": "记录已由上游系统采集", "signature_id": operator.sign("跳过", target=batch_id),
    })
    assert skipped.status_code == 200, skipped.text
    detail = _run(operator, batch_id, executor)
    assert detail["state"] == "done", detail["failure_reason"]
    assert _runs(detail, "m1")[-1]["state"] == "skipped"
    assert any(row["action"] == "跳过步骤" and row["sign"] for row in detail["audit"])


def test_rerun_from_an_earlier_step_voids_downstream_records(operator, reset_runtime, db, executor):
    def shape(steps):
        dry, weigh, assemble, test = steps
        return [dry, _strip_hard(weigh), _manual(timeout={"minutes": 1, "action": "fail"}), _strip_hard(assemble), test]

    batch_id = _graph_batch(operator, db, shape)
    _dispatch(operator, batch_id)
    _run(operator, batch_id, executor, rounds=8, until=("never",))
    _expire_deadlines(db, batch_id)
    detail = _run(operator, batch_id, executor, rounds=6, until=("fault",))
    assert detail["state"] == "fault"
    weigh_id = detail["snapshot"]["steps"][1]["step_id"]
    options = operator.get(f"/api/batches/{batch_id}/recovery-options").json()
    assert weigh_id in [row["step_id"] for row in options["rerun_targets"]]

    rerun = operator.post(f"/api/batches/{batch_id}/rerun", {
        "step_id": weigh_id, "reason": "天平漂移，从称重重做", "signature_id": operator.sign("重做", target=batch_id),
    })
    assert rerun.status_code == 200, rerun.text
    detail = _run(operator, batch_id, executor, rounds=8, until=("never",))
    assert [row["state"] for row in _runs(detail, weigh_id)] == ["superseded", "completed"]
    manual_runs = _runs(detail, "m1")
    assert manual_runs[0]["state"] == "superseded" and manual_runs[-1]["state"] == "ready"
    audit = next(row for row in detail["audit"] if row["action"] == "从指定节点重做")
    assert "重新执行的设备步骤" in audit["detail"]


def test_subflow_is_expanded_into_the_batch_snapshot_and_runs(operator, reset_runtime, db, executor):
    from app.core.context import system_context
    from app.models import Recipe
    from app.services.batch_service import BatchService
    from app.services.recipe_service import RecipeService

    if db.get(Recipe, "R-990") is None:
        db.add(Recipe(
            id="R-990", org_id="ORG-001", name="极片前处理", version="1.0.0", state="released", owner="研究员", updated="2026-09-24",
            plate=8, risk="RA-990", design="", bom=[{"material": "电解液 LP57", "qty": 0.5, "unit": "mL"}],
            steps=[
                {"step_id": "s01", "name": "真空干燥", "kind": "device", "cap": "cap.vacuum_dry",
                 "params": {"temp": 120, "vacuum": 1}, "dur": 30},
                {"step_id": "s02", "name": "称重", "kind": "device", "cap": "cap.weigh", "params": {"mass": 0.0152}, "dur": 10},
            ],
            history=[], diff=[],
        ))
        db.add(Recipe(
            id="R-991", org_id="ORG-001", name="组装（引用前处理）", version="0.1.0", state="draft", owner="研究员", updated="2026-09-24",
            plate=8, risk="RA-991", design="", bom=[{"material": "电解液 LP57", "qty": 1, "unit": "mL"}],
            steps=[
                {"step_id": "s01", "name": "前处理", "kind": "subflow", "subflow": {"recipe_id": "R-990"}},
                {"step_id": "s02", "name": "注液封口组装", "kind": "device", "cap": "cap.assemble",
                 "params": {"electrolyte": 60}, "dur": 30},
            ],
            history=[], diff=[],
        ))
        db.commit()
    ctx = system_context("ORG-001")
    parent = db.get(Recipe, "R-991")
    detail = RecipeService(db, ctx).to_dict(parent, detail=True)
    assert all(row["ok"] for row in detail["validation"]), detail["validation"]
    assert detail["critical_path_min"] == 70, "关键路径按展开后的步骤算"

    snapshot = BatchService(db, ctx)._freeze_recipe(parent)
    assert [step["step_id"] for step in snapshot["steps"]] == ["s01.s01", "s01.s02", "s02"]
    assert snapshot["steps"][2]["after"] == ["s01.s02"]
    assert snapshot["bom"] == [{"material": "电解液 LP57", "qty": 1.5, "unit": "mL"}]
    assert snapshot["subflows"][0]["recipe_id"] == "R-990"

    draft = db.get(Recipe, "R-990")
    draft.state = "draft"
    db.commit()
    try:
        broken = RecipeService(db, ctx).to_dict(db.get(Recipe, "R-991"), detail=True)
        assert any("已发布" in issue for row in broken["validation"] for issue in row["issues"])
    finally:
        draft.state = "released"
        db.commit()

    def shape(steps):
        dry, weigh, assemble, test = steps
        return [*snapshot["steps"][:2], {**_strip_hard(assemble), "after": ["s01.s02"]}, {**test, "after": [assemble["step_id"]]}]

    batch_id = _graph_batch(operator, db, shape)
    _dispatch(operator, batch_id)
    result = _run(operator, batch_id, executor)
    assert result["state"] == "done", result["failure_reason"]
    assert result["steps"][0]["groups"][0]["name"] == "前处理"


def test_review_timeout_fail_is_a_fault_not_a_rejection(operator, reset_runtime, db, executor):
    """审核超时判失败走恢复评估；不能被当成 QA 退回、悄悄重开上游人工步骤。"""
    def shape(steps):
        dry, weigh, assemble, test = steps
        review = {"step_id": "r1", "name": "QA 审核", "kind": "review", "review_role": "qa",
                  "timeout": {"minutes": 1, "action": "fail"}}
        return [_manual(), review, _strip_hard(weigh)]

    batch_id = _graph_batch(operator, db, shape)
    _dispatch(operator, batch_id)
    manual = _runs(operator.get(f"/api/batches/{batch_id}").json(), "m1")[-1]
    submitted = operator.post(f"/api/step-runs/{manual['id']}/submit", {
        "form_data": {"note": "已核对"}, "checks": {"samples": True, "materials": True}, "row_version": manual["row_version"],
    })
    assert submitted.status_code == 200, submitted.text
    _run(operator, batch_id, executor, rounds=3, until=("never",))
    _expire_deadlines(db, batch_id)
    detail = _run(operator, batch_id, executor, rounds=6, until=("fault",))
    assert detail["state"] == "fault"
    assert _runs(detail, "r1")[-1]["state"] == "failed"
    assert len(_runs(detail, "m1")) == 1, "上游人工步骤没有被当成退回重开"


def test_a_second_signal_is_kept_for_the_next_wait(operator, reset_runtime, db, executor):
    """第一个等待已绑定信号、推进事件还没处理时到来的第二条同名信号，要留给下一个等待节点。"""
    def shape(steps):
        dry, weigh, assemble, test = steps
        return [_event_wait("tray_ready"), {**_event_wait("tray_ready"), "step_id": "w2", "name": "等第二盘"}, _strip_hard(weigh)]

    batch_id = _graph_batch(operator, db, shape)
    assert operator.post(f"/api/batches/{batch_id}/signals", {"name": "tray_ready", "event_id": "T-1"}).status_code == 200
    _dispatch(operator, batch_id)
    second = operator.post(f"/api/batches/{batch_id}/signals", {"name": "tray_ready", "event_id": "T-2"})
    assert second.status_code == 200, second.text
    detail = _run(operator, batch_id, executor)
    assert detail["state"] == "done", detail["failure_reason"]
    signals = {row["name"] + row["id"]: row for row in operator.get(f"/api/batches/{batch_id}/signals").json()}
    consumers = {row["consumed_by_run_id"] for row in signals.values()}
    assert consumers == {_runs(detail, "w1")[-1]["id"], _runs(detail, "w2")[-1]["id"]}, "两条信号各唤醒一个等待"
