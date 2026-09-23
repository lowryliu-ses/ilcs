"""批次闭环：创建 → 排程 → 开跑检查 → 签名下发 → 执行器跑完 → 数据复核。

与改造前的关键差别：批次跑完不再自动把结果判成有效，预留也不再按步骤比例倒扣。
「运行结束」和「数据可用」是两件事，这里分别断言。
"""
import pytest


@pytest.fixture()
def scheduled_batch(operator, reset_runtime):
    created = operator.post("/api/batches", {"plan_id": "EP-205-01", "priority": 2})
    assert created.status_code == 201, created.text
    batch_id = created.json()["id"]
    scheduled = operator.post(f"/api/batches/{batch_id}/schedule", {})
    assert scheduled.status_code == 200, scheduled.text
    return batch_id


def test_create_is_atomic_and_idempotent(operator, reset_runtime):
    key = "test-create-once"
    first = operator.post("/api/batches", {"plan_id": "EP-201-03"}, idempotency_key=key)
    second = operator.post("/api/batches", {"plan_id": "EP-201-03"}, idempotency_key=key)

    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["id"] == second.json()["id"], "同一幂等键不能创建第二个批次"

    detail = operator.get(f"/api/batches/{first.json()['id']}").json()
    assert detail["sample_count"] == 24, "运行分配数 = 条件 × 重复，受方法样品位截断"
    assert len(detail["reservations"]) >= 2, "创建时必须写入 BOM 预留"
    assert detail["snapshot"]["version"] == "1.2.0", "快照在创建时冻结"
    assert detail["task_id"], "批次必须绑定实验任务，不留第二条数据链"


def test_create_without_idempotency_key_is_rejected(operator, reset_runtime):
    """关键写接口缺幂等键直接拒绝——网络重试要复用原键，不是生成新键。"""
    response = operator.post_without_key("/api/batches", {"plan_id": "EP-201-03"})

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "idempotency_key_required"


def test_same_key_with_different_body_conflicts(operator, reset_runtime):
    key = "test-same-key-different-body"
    first = operator.post("/api/batches", {"plan_id": "EP-201-03"}, idempotency_key=key)
    assert first.status_code == 201
    second = operator.post(
        "/api/batches", {"plan_id": "EP-205-01"}, idempotency_key=key
    )

    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "idempotency_conflict"


def test_unlocked_plan_cannot_start_a_batch(operator, reset_runtime):
    rejected = operator.post("/api/batches", {"plan_id": "EP-203-01"})

    assert rejected.status_code == 409
    assert "已锁定" in rejected.json()["detail"]["message"]


def test_locked_but_unapproved_plan_cannot_start_a_batch(operator, researcher, reset_runtime):
    """AC-22 / 10.2：矩阵锁定只是结构冻结，不能当成已审批。"""
    created = researcher.post(
        "/api/plans",
        {
            "name": "锁定但未审批", "recipe_id": "R-205", "plan_type": "matrix", "repeats": 1,
            "factors": [{"name": "注液量", "unit": "μL", "levels": [50, 60]}],
            "required_metrics": ["METRIC-discharge_capacity-v1"],
        },
    )
    assert created.status_code == 201, created.text
    plan_id = created.json()["id"]
    assert researcher.post(f"/api/plans/{plan_id}/lock").status_code == 200

    rejected = operator.post("/api/batches", {"plan_id": plan_id})
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "plan_not_approved"


def test_dispatch_requires_manual_review_and_server_checks(operator, scheduled_batch):
    preflight = operator.get(f"/api/batches/{scheduled_batch}/preflight?manual_review=true").json()
    assert preflight["ok"], preflight["blocked"]
    assert len(preflight["checks"]) == 9
    assert {c["state"] for c in preflight["checks"]} <= {"pass", "blocked", "not_applicable"}

    without_review = operator.post(
        f"/api/batches/{scheduled_batch}/dispatch",
        {"manual_review": False, "signature_id": operator.sign("批准执行", target=scheduled_batch)},
    )
    assert without_review.status_code == 409
    assert [c["key"] for c in without_review.json()["detail"]["blocked"]] == ["authority"]

    dispatched = operator.post(
        f"/api/batches/{scheduled_batch}/dispatch",
        {"manual_review": True, "reason": "托盘与物料复核完成",
         "signature_id": operator.sign("批准执行", target=scheduled_batch)},
    )
    assert dispatched.status_code == 200 and dispatched.json()["state"] == "running"


def test_researcher_cannot_dispatch(researcher, operator, scheduled_batch):
    response = researcher.post(
        f"/api/batches/{scheduled_batch}/dispatch",
        {"manual_review": True, "signature_id": researcher.sign("批准执行", target=scheduled_batch)},
    )

    assert response.status_code == 403


def test_full_run_leaves_results_unassessed_until_review(
    operator, qa, scheduled_batch, executor
):
    """AC-31：回传成功、批次完成，但没有复核就不是有效数据。"""
    operator.post(
        f"/api/batches/{scheduled_batch}/dispatch",
        {"manual_review": True, "signature_id": operator.sign("批准执行", target=scheduled_batch)},
    )
    for _ in range(12):
        executor()

    detail = operator.get(f"/api/batches/{scheduled_batch}").json()
    assert detail["state"] == "done", detail["failure_reason"]
    assert len(detail["checkpoints"]) == detail["step_count"], "每个设备步骤一个检查点"
    # 预留不再按步骤比例倒扣：没有库存事件就没有消耗
    assert all(
        r["consumed_qty"] == "0.000000" for r in detail["reservations"]
    ), "实际消耗必须由库存事件入账，不按步骤数量均分"
    assert all(r["state"] == "reserved" for r in detail["reservations"])

    for row in detail["samples"]:
        assert row["legacy_quality"] is None, "批次完成不得自动授予质量结论"

    golden = qa.post(
        "/api/recipes/R-205/golden-batch",
        {"batch_id": scheduled_batch,
         "signature_id": qa.sign("结果复核通过，作为对比基准", target=scheduled_batch)},
    )
    assert golden.status_code == 200 and golden.json()["golden_batch_id"] == scheduled_batch

    export = operator.get(f"/api/results/{scheduled_batch}/export")
    assert export.status_code == 200 and "condition_group" in export.text


def test_repeated_command_delivery_executes_once(operator, scheduled_batch):
    from app.core.context import system_context
    from app.core.db import SessionLocal
    from app.models import AdapterExecution, Checkpoint
    from app.services.execution_service import ExecutionService

    operator.post(
        f"/api/batches/{scheduled_batch}/dispatch",
        {"manual_review": True, "signature_id": operator.sign("批准执行", target=scheduled_batch)},
    )

    with SessionLocal() as db:
        service = ExecutionService(db, system_context("ORG-001", "测试执行器"))
        command = next(c for c in service.commands.for_batch(scheduled_batch) if c.state == "sent")
        assert service.execute(command) is True
        db.commit()
        replay = service.execute(command)
        db.commit()

        assert replay is False, "重复投递必须被适配器台账拦截"
        assert db.query(AdapterExecution).filter(AdapterExecution.command_id == command.id).count() == 1
        assert db.query(Checkpoint).filter(Checkpoint.command_id == command.id).count() == 1


def test_legacy_quality_flag_is_marked_as_historical(operator, researcher, scheduled_batch, executor):
    """过渡期的人工质量标记保留，但响应明确它不等于审核通过。"""
    operator.post(
        f"/api/batches/{scheduled_batch}/dispatch",
        {"manual_review": True, "signature_id": operator.sign("批准执行", target=scheduled_batch)},
    )
    for _ in range(12):
        executor()

    before = operator.get(f"/api/results/{scheduled_batch}").json()
    assert before.get("legacy") is True, "没有类型化结果时回落到历史视图并标注"
    sample_id = before["groups"][0]["samples"][0]["id"]

    flagged = researcher.post(
        f"/api/samples/{sample_id}/flag", {"quality": "invalid", "note": "谱图基线漂移"}
    )
    assert flagged.status_code == 200
    assert "不等于结果审核通过" in flagged.json()["legacy_note"]
