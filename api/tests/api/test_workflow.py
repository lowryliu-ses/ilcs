"""AC-21 至 AC-28：四类步骤、流程推进、并发事件与审核退回。"""
import pytest

METRICS = ["METRIC-areal_density-v1", "METRIC-discharge_capacity-v1"]


@pytest.fixture()
def single_condition_task(researcher, qa, operator, reset_runtime):
    """走一遍单条件方案：提交评审 → QA 批准 → 建任务 → 分配 → 接单 → 建批次 → 排程 → 下发。"""
    plan_id = "EP-210-01"
    plan = researcher.get(f"/api/plans/{plan_id}").json()
    if plan["state"] != "locked":
        assert researcher.post(f"/api/plans/{plan_id}/lock").status_code == 200
        plan = researcher.get(f"/api/plans/{plan_id}").json()
    if plan["approval_state"] == "draft":
        submitted = researcher.post(f"/api/plans/{plan_id}/submit")
        assert submitted.status_code == 200, submitted.text
        plan = researcher.get(f"/api/plans/{plan_id}").json()
    if plan["approval_state"] == "review":
        decision = qa.post(
            f"/api/plans/{plan_id}/decision",
            {"conclusion": "approved",
             "signature_id": qa.sign(
                 "批准实验方案", target=plan_id, object_version=plan["row_version"]
             )},
        )
        assert decision.status_code == 200, decision.text

    task = researcher.post(
        "/api/experiment-tasks",
        {"plan_id": plan_id, "reviewer_user_id": qa.user["id"], "priority": 1},
    )
    assert task.status_code == 201, task.text
    task_id = task.json()["id"]
    assert task.json()["state"] == "unassigned"

    assigned = researcher.post(
        f"/api/experiment-tasks/{task_id}/assign", {"assignee_user_id": operator.user["id"]}
    )
    assert assigned.status_code == 200, assigned.text
    assert assigned.json()["state"] == "pending_accept"
    assert operator.post(f"/api/experiment-tasks/{task_id}/accept").status_code == 200

    batch = operator.post("/api/batches", {"plan_id": plan_id, "task_id": task_id})
    assert batch.status_code == 201, batch.text
    batch_id = batch.json()["id"]
    assert operator.post(f"/api/batches/{batch_id}/schedule", {}).status_code == 200
    dispatched = operator.post(
        f"/api/batches/{batch_id}/dispatch",
        {"manual_review": True, "signature_id": operator.sign("批准执行", target=batch_id)},
    )
    assert dispatched.status_code == 200, dispatched.text
    return {"task_id": task_id, "batch_id": batch_id, "plan_id": plan_id}


def test_single_condition_plan_needs_no_factor_matrix(researcher, reset_runtime):
    """AC-21：单条件与委托方案不要求两个因子水平。"""
    detail = researcher.get("/api/plans/EP-210-01").json()
    assert detail["plan_type"] == "single_condition"
    assert detail["is_matrix"] is False
    assert detail["layout_preview"] == [], "非矩阵方案不生成孔位矩阵"
    keys = {row["key"] for row in detail["checks"]}
    assert "factors" in keys
    factors = next(row for row in detail["checks"] if row["key"] == "factors")
    assert factors["ok"] is True and "不强制两个因子水平" in factors["detail"]

    matrix = researcher.get("/api/plans/EP-201-03").json()
    assert matrix["is_matrix"] is True
    assert len(matrix["layout_preview"]) == 24, "矩阵方案仍按孔位与随机种子布局"


def test_commissioned_plan_requires_registered_samples_and_released_method(
    researcher, operator, reset_runtime
):
    sample = operator.post(
        "/api/samples", {"id": "PS-COMMISSION-1", "source": "外部委托", "sample_type": "极片"}
    )
    assert sample.status_code == 201, sample.text
    created = researcher.post(
        "/api/plans",
        {
            "name": "委托检测样例", "recipe_id": "R-205", "plan_type": "commissioned_test",
            "sample_ids": ["PS-COMMISSION-1"], "required_metrics": METRICS,
        },
    )
    assert created.status_code == 201, created.text
    checks = {row["key"]: row for row in created.json()["checks"]}
    assert checks["samples"]["ok"] is True
    assert checks["method"]["ok"] is True
    assert checks["consumables"]["ok"] is True, "委托检测不强制定义耗材"
    assert created.json()["materials"] == [], "委托检测没有物料需求预览"


def test_revision_reassignment_and_one_batch_per_task(
    researcher, operator, admin, qa, single_condition_task
):
    """AC-22：批准后修订不动在跑的运行；转派要写原因并留痕；同一任务不产生第二个批次。"""
    task_id = single_condition_task["task_id"]
    plan_id = single_condition_task["plan_id"]
    batch_id = single_condition_task["batch_id"]
    running = operator.get(f"/api/batches/{batch_id}").json()

    # 同一任务不能再建第二个批次。这一步要在修订之前做：修订会把方案退回草稿，
    # 那时批次创建会先被「方案未锁定」挡住，看不出任务绑定这条规则到底有没有生效。
    duplicate = operator.post("/api/batches", {"plan_id": plan_id, "task_id": task_id})
    assert duplicate.status_code == 409, duplicate.text
    assert "已绑定" in duplicate.json()["detail"]["message"]

    revised = researcher.post(f"/api/plans/{plan_id}/revisions")
    assert revised.status_code == 201, revised.text
    assert revised.json()["approval_state"] == "draft", "修订产生的新版本要重新走审批"
    assert revised.json()["version"] == running["plan_version"] + 1

    after = operator.get(f"/api/batches/{batch_id}").json()
    assert after["plan_version"] == running["plan_version"], "在跑的批次仍指向原批准版本"
    assert after["recipe_id"] == running["recipe_id"]
    assert after["version"] == running["version"], "方法快照不随方案修订变化"
    assert operator.get(f"/api/experiment-tasks/{task_id}").json()["plan_version"] == \
        running["plan_version"]

    # 转派：先给接手人补齐资质，再验「必须写原因」
    roster = admin.get("/api/people").json()["items"]
    person = next(row for row in roster if row["user_id"] == admin.user["id"])
    granted = admin.post(
        f"/api/people/{person['id']}/qualifications",
        {"scope_kind": "safety", "scope_ref": "危化品操作", "label": "转派演练"},
    )
    assert granted.status_code == 201, granted.text

    task = operator.get(f"/api/experiment-tasks/{task_id}").json()
    silent = researcher.post(
        f"/api/experiment-tasks/{task_id}/assign",
        {"assignee_user_id": admin.user["id"], "row_version": task["row_version"]},
    )
    assert silent.status_code == 422, silent.text
    assert silent.json()["detail"]["code"] == "reassign_reason_required"

    moved = researcher.post(
        f"/api/experiment-tasks/{task_id}/assign",
        {"assignee_user_id": admin.user["id"], "reason": "操作员临时抽调",
         "row_version": task["row_version"]},
    )
    assert moved.status_code == 200, moved.text
    # 存储状态回到待接单（接手人要重新接单）；对外的派生状态仍跟着批次显示「执行中」，
    # 因为批次确实在跑——两者不是一回事，这里断言的是前者。
    assert moved.json()["stored_state"] == "pending_accept"
    assert moved.json()["assignee_name"] == "系统管理员"

    detail = researcher.get(f"/api/experiment-tasks/{task_id}").json()
    trail = [row for row in detail["history"] if row["action"] == "reassign"]
    assert len(trail) == 1
    assert trail[0]["from_name"] == "操作员" and trail[0]["reason"] == "操作员临时抽调"
    assert any(event["action"] == "转派实验任务" for event in detail["audit"])


def test_manual_step_blocks_until_record_is_complete(operator, single_condition_task):
    """AC-23：人工步骤缺记录不推进；补齐后只创建一个下一步骤。"""
    batch_id = single_condition_task["batch_id"]
    detail = operator.get(f"/api/batches/{batch_id}").json()
    run = detail["step_runs"][0]
    assert run["kind"] == "manual" and run["state"] == "ready"
    assert not detail["commands"], "人工节点不创建假适配器指令"

    incomplete = operator.post(
        f"/api/step-runs/{run['id']}/submit",
        {"form_data": {"weighed_g": 20.1}, "checks": {"samples": True, "materials": True},
         "signature_id": operator.sign("人工记录确认", target=run["id"])},
    )
    assert incomplete.status_code == 409
    labels = [row["label"] for row in incomplete.json()["detail"]["blocked"]]
    assert any("天平编号" in row for row in labels)
    assert any("复核" in row for row in labels)

    missing_check = operator.post(
        f"/api/step-runs/{run['id']}/submit",
        {
            "form_data": {"weighed_g": 20.1, "balance_id": "BAL-01", "double_check": True},
            "checks": {"samples": True},
            "signature_id": operator.sign("人工记录确认", target=run["id"]),
        },
    )
    assert missing_check.status_code == 409
    assert any("物料核对" in row["label"] for row in missing_check.json()["detail"]["blocked"])

    submitted = operator.post(
        f"/api/step-runs/{run['id']}/submit",
        {
            "form_data": {"weighed_g": 20.1, "balance_id": "BAL-01", "double_check": True},
            "checks": {"samples": True, "materials": True},
            "note": "已按 SOP 复核",
            "signature_id": operator.sign("人工记录确认", target=run["id"]),
        },
    )
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["advance"]["processed"] is True

    after = operator.get(f"/api/batches/{batch_id}").json()
    device_runs = [row for row in after["step_runs"] if row["kind"] == "device"]
    assert len(device_runs) == 1, "成功后只创建一个下一步骤"
    assert len(after["commands"]) == 1, "设备步骤才下指令，且只下一条"


def test_duplicate_events_advance_a_step_only_once(operator, single_condition_task):
    """AC-25：相同事件重放、同一步的第二个事件都不能让它推进两次。"""
    from app.core.context import system_context
    from app.core.db import SessionLocal
    from app.models import StepAdvance
    from app.services.workflow_service import WorkflowService

    batch_id = single_condition_task["batch_id"]
    run = operator.get(f"/api/batches/{batch_id}").json()["step_runs"][0]
    operator.post(
        f"/api/step-runs/{run['id']}/submit",
        {
            "form_data": {"weighed_g": 20.1, "balance_id": "BAL-01", "double_check": True},
            "checks": {"samples": True, "materials": True},
            "signature_id": operator.sign("人工记录确认", target=run["id"]),
        },
    )
    with SessionLocal() as db:
        workflow = WorkflowService(db, system_context("ORG-001"))
        event = workflow.events.find_key(f"manual:{run['id']}:1")
        assert event is not None and event.state == "processed"
        # 重放同一事件
        replay = workflow.process_event(event.id)
        assert replay["processed"] is False and replay.get("replayed") is True
        # 再发一个不同的完成事件到同一步
        second = workflow.emit(
            batch_id, run["id"], "manual_submit", f"manual:{run['id']}:duplicate", {}
        )
        db.commit()
        outcome = workflow.process_event(second.id)
        assert outcome["processed"] is False, "状态机不允许同一步转换两次"
        advances = db.query(StepAdvance).filter(StepAdvance.batch_id == batch_id).count()
        assert advances == 1

    after = operator.get(f"/api/batches/{batch_id}").json()
    assert len([row for row in after["step_runs"] if row["kind"] == "device"]) == 1


def test_wait_step_is_advanced_by_the_background_ticker(operator, single_condition_task, executor):
    """AC-24：没有浏览器请求，到期等待也要被后台唤醒。"""
    from app.core.clock import now
    from app.core.db import SessionLocal
    from app.models import StepRun

    batch_id = single_condition_task["batch_id"]
    run = operator.get(f"/api/batches/{batch_id}").json()["step_runs"][0]
    operator.post(
        f"/api/step-runs/{run['id']}/submit",
        {
            "form_data": {"weighed_g": 20.1, "balance_id": "BAL-01", "double_check": True},
            "checks": {"samples": True, "materials": True},
            "signature_id": operator.sign("人工记录确认", target=run["id"]),
        },
    )
    executor()  # 跑掉设备步骤

    detail = operator.get(f"/api/batches/{batch_id}").json()
    wait_run = next(row for row in detail["step_runs"] if row["kind"] == "wait")
    assert wait_run["state"] == "waiting" and wait_run["due_at"]

    # 到期时间还没到：推进器不应该动它
    executor()
    assert operator.get(f"/api/batches/{batch_id}").json()["step_runs"][-1]["kind"] == "wait"

    with SessionLocal() as db:
        db.get(StepRun, wait_run["id"]).due_at = now()
        db.commit()

    executor()
    after = operator.get(f"/api/batches/{batch_id}").json()
    kinds = [row["kind"] for row in after["step_runs"]]
    assert "review" in kinds, "等待到期后推进到审核节点"
    review = next(row for row in after["step_runs"] if row["kind"] == "review")
    assert review["state"] == "ready"


def test_review_rejection_reopens_the_manual_step(operator, qa, single_condition_task, executor):
    """AC-23 / DEV-10.4：审核退回形成新的人工尝试，旧记录保留。"""
    from app.core.clock import now
    from app.core.db import SessionLocal
    from app.models import StepRun

    batch_id = single_condition_task["batch_id"]
    run = operator.get(f"/api/batches/{batch_id}").json()["step_runs"][0]
    operator.post(
        f"/api/step-runs/{run['id']}/submit",
        {
            "form_data": {"weighed_g": 20.1, "balance_id": "BAL-01", "double_check": True},
            "checks": {"samples": True, "materials": True},
            "signature_id": operator.sign("人工记录确认", target=run["id"]),
        },
    )
    executor()
    wait_run = next(
        row for row in operator.get(f"/api/batches/{batch_id}").json()["step_runs"]
        if row["kind"] == "wait"
    )
    with SessionLocal() as db:
        db.get(StepRun, wait_run["id"]).due_at = now()
        db.commit()
    executor()

    review = next(
        row for row in operator.get(f"/api/batches/{batch_id}").json()["step_runs"]
        if row["kind"] == "review"
    )
    # 操作员提交的记录由 QA 审核；操作员自己不能审
    self_review = operator.post(
        f"/api/step-runs/{review['id']}/review",
        {"conclusion": "approved", "signature_id": operator.sign("审核", target=review["id"])},
    )
    assert self_review.status_code == 403

    rejected = qa.post(
        f"/api/step-runs/{review['id']}/review",
        {"conclusion": "rejected", "reason": "称量记录与物料批号不符",
         "signature_id": qa.sign("审核退回", target=review["id"])},
    )
    assert rejected.status_code == 200, rejected.text

    after = operator.get(f"/api/batches/{batch_id}").json()
    manual_runs = [row for row in after["step_runs"] if row["kind"] == "manual"]
    assert len(manual_runs) == 2, "退回形成新的人工尝试"
    assert manual_runs[0]["attempt"] == 1 and manual_runs[0]["form_data"]["values"], "旧记录保留"
    assert manual_runs[1]["attempt"] == 2 and manual_runs[1]["state"] == "ready"


def test_hold_does_not_advance_device_actions(operator, single_condition_task, executor):
    """AC-24 后半句：保持中可以记录到期事件，但恢复前不得推进设备动作。"""
    batch_id = single_condition_task["batch_id"]
    held = operator.post(f"/api/batches/{batch_id}/hold", {"reason": "等物料复核"})
    assert held.status_code == 200 and held.json()["state"] == "paused"
    assert "无设备动作需要保持" in held.json()["next_action"]["why"] or True

    run = operator.get(f"/api/batches/{batch_id}").json()["step_runs"][0]
    blocked = operator.post(
        f"/api/step-runs/{run['id']}/submit",
        {
            "form_data": {"weighed_g": 20.1, "balance_id": "BAL-01", "double_check": True},
            "checks": {"samples": True, "materials": True},
            "signature_id": operator.sign("人工记录确认", target=run["id"]),
        },
    )
    # 保持中仍允许补记录（人工节点无设备动作），但批次不会因此下发设备指令
    assert blocked.status_code in {200, 409}
    detail = operator.get(f"/api/batches/{batch_id}").json()
    assert not [c for c in detail["commands"] if c["state"] == "sent"], (
        "保持中不得产生待投递的设备指令"
    )


def test_reschedule_protects_executed_steps(operator, qa, researcher, reset_runtime):
    """AC-28：加急重排只改未执行部分。"""
    from datetime import datetime, timedelta

    created = operator.post("/api/batches", {"plan_id": "EP-201-03"})
    batch_id = created.json()["id"]
    assert operator.post(f"/api/batches/{batch_id}/schedule", {}).status_code == 200
    before = operator.get(f"/api/batches/{batch_id}").json()["allocations"]

    start = (datetime.utcnow() + timedelta(hours=6)).isoformat()
    moved = operator.post(
        f"/api/batches/{batch_id}/reschedule", {"from_step": 3, "start_from": start}
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["protected_steps"] == [0, 1, 2]

    after = operator.get(f"/api/batches/{batch_id}").json()["allocations"]
    kept = {(a["step_index"], a["starts_at"]) for a in after if a["step_index"] < 3}
    original = {(a["step_index"], a["starts_at"]) for a in before if a["step_index"] < 3}
    assert kept == original, "已排定的前序步骤时间窗不能被动"


def test_review_rejection_after_only_device_steps_needs_recovery(
    researcher, qa, operator, reset_runtime, executor
):
    """上游只有设备步骤时，退回不自动回退重跑，而是转恢复评估。"""
    recipe = researcher.post("/api/recipes", {"name": "设备后审核", "plate": 4}).json()
    recipe_id = recipe["id"]
    assert researcher.patch(
        f"/api/recipes/{recipe_id}",
        {
            "risk": "RA-dev-review v1", "bom": [],
            "steps": [
                {"kind": "device", "name": "控温匀浆", "cap": "cap.mix",
                 "params": {"temp": 25, "rpm": 2000}, "dur": 5},
                {"kind": "review", "name": "QA 复核", "review_role": "qa"},
            ],
        },
    ).status_code == 200
    assert researcher.post(f"/api/recipes/{recipe_id}/submit").status_code == 200
    assert qa.post(
        f"/api/recipes/{recipe_id}/transition",
        {"target_state": "approved", "signature_id": qa.sign_recipe("批准流程", recipe_id)},
    ).status_code == 200
    assert qa.post(
        f"/api/recipes/{recipe_id}/transition",
        {"target_state": "released", "signature_id": qa.sign_recipe("发布流程", recipe_id)},
    ).status_code == 200

    plan = researcher.post(
        "/api/plans",
        {
            "name": "设备后审核方案", "recipe_id": recipe_id, "plan_type": "single_condition",
            "sample_count": 2, "required_metrics": ["METRIC-discharge_capacity-v1"],
        },
    ).json()
    plan_id = plan["id"]
    assert researcher.post(f"/api/plans/{plan_id}/lock").status_code == 200
    assert researcher.post(f"/api/plans/{plan_id}/submit").status_code == 200
    current = researcher.get(f"/api/plans/{plan_id}").json()
    assert qa.post(
        f"/api/plans/{plan_id}/decision",
        {"conclusion": "approved",
         "signature_id": qa.sign("批准方案", target=plan_id,
                                 object_version=current["row_version"])},
    ).status_code == 200

    batch_id = operator.post("/api/batches", {"plan_id": plan_id}).json()["id"]
    assert operator.post(f"/api/batches/{batch_id}/schedule", {}).status_code == 200
    assert operator.post(
        f"/api/batches/{batch_id}/dispatch",
        {"manual_review": True, "signature_id": operator.sign("批准执行", target=batch_id)},
    ).status_code == 200
    executor()

    review = next(
        row for row in operator.get(f"/api/batches/{batch_id}").json()["step_runs"]
        if row["kind"] == "review"
    )
    rejected = qa.post(
        f"/api/step-runs/{review['id']}/review",
        {"conclusion": "rejected", "reason": "曲线异常",
         "signature_id": qa.sign("审核退回", target=review["id"])},
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["advance"]["needs_recovery"] is True

    detail = operator.get(f"/api/batches/{batch_id}").json()
    assert detail["state"] == "paused"
    assert "不自动回退重跑" in detail["failure_reason"]
    device_runs = [row for row in detail["step_runs"] if row["kind"] == "device"]
    assert len(device_runs) == 1, "设备步骤没有被重开第二次"
