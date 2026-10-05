"""测试环境的管理员级联强制删除（ILCS_ADMIN_FORCE_DELETE）。"""
import sys
import uuid

import pytest

CAPACITY = "METRIC-discharge_capacity-v1"


@pytest.fixture()
def force_delete_on(monkeypatch):
    monkeypatch.setattr(sys.modules["app.core.config"].settings, "admin_force_delete", True)


def _approved_plan(researcher, qa) -> str:
    created = researcher.post("/api/plans", {
        "name": f"强制删除 {uuid.uuid4().hex[:4]}", "recipe_id": "R-205", "plan_type": "single_condition",
        "sample_count": 2, "required_metrics": [CAPACITY],
    })
    assert created.status_code == 201, created.text
    plan_id = created.json()["id"]
    assert researcher.post(f"/api/plans/{plan_id}/lock").status_code == 200
    assert researcher.post(f"/api/plans/{plan_id}/submit").status_code == 200
    plan = researcher.get(f"/api/plans/{plan_id}").json()
    decided = qa.post(f"/api/plans/{plan_id}/decision", {
        "conclusion": "approved",
        "signature_id": qa.sign("批准方案", target=plan_id, object_version=plan["row_version"]),
    })
    assert decided.status_code == 200, decided.text
    return plan_id


def _dispatched_batch(researcher, qa, operator) -> tuple[str, str, str]:
    plan_id = _approved_plan(researcher, qa)
    task = researcher.post("/api/experiment-tasks", {"plan_id": plan_id})
    assert task.status_code == 201, task.text
    created = operator.post("/api/batches", {"plan_id": plan_id, "task_id": task.json()["id"]})
    assert created.status_code == 201, created.text
    batch_id = created.json()["id"]
    assert operator.post(f"/api/batches/{batch_id}/schedule", {}).status_code == 200
    dispatched = operator.post(
        f"/api/batches/{batch_id}/dispatch",
        {"manual_review": True, "signature_id": operator.sign("批准执行", target=batch_id)},
    )
    assert dispatched.status_code == 200, dispatched.text
    return plan_id, task.json()["id"], batch_id


def _force(admin, kind: str, object_id: str, reason: str = "联调数据清理"):
    return admin.post(f"/api/admin/force-delete/{kind}/{object_id}", {
        "reason": reason, "signature_id": admin.sign("强制删除", target=f"{kind}:{object_id}"),
    })


def test_force_delete_is_off_unless_switched_on(admin, monkeypatch):
    assert admin.get("/api/auth/me").json()["admin_force_delete"] is False
    assert admin.get("/api/admin/force-delete/plan/EP-205-01").status_code == 403

    settings = sys.modules["app.core.config"].settings
    monkeypatch.setattr(settings, "admin_force_delete", True)
    monkeypatch.setattr(settings, "environment", "production")
    assert admin.get("/api/auth/me").json()["admin_force_delete"] is False, "正式环境开了开关也不放行"
    assert admin.get("/api/admin/force-delete/plan/EP-205-01").status_code == 403


def test_only_admin_may_force_delete(operator, force_delete_on):
    assert operator.get("/api/admin/force-delete/plan/EP-205-01").status_code == 403


def test_plan_force_delete_takes_tasks_batches_and_run_data(
    admin, researcher, qa, operator, executor, reset_runtime, force_delete_on, db,
):
    from app.models import AuditEvent, Command, PhysicalSample, Sample

    plan_id, task_id, batch_id = _dispatched_batch(researcher, qa, operator)
    for _ in range(12):
        executor()
    assert operator.get(f"/api/batches/{batch_id}").json()["state"] == "done"
    physical = [row.physical_sample_id for row in db.query(Sample).filter(Sample.batch_id == batch_id)]
    # 正常删除：方案已批准、有批次，拒绝
    assert researcher.client.delete(f"/api/plans/{plan_id}", headers=researcher.headers).status_code == 409

    preview = admin.get(f"/api/admin/force-delete/plan/{plan_id}")
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["blockers"] == []
    cascade = {row["kind"]: row["ids"] for row in body["cascade"]}
    assert cascade["task"] == [task_id] and cascade["batch"] == [batch_id]
    counts = {row["label"]: row["count"] for row in body["counts"]}
    assert counts["实验方案"] == 1 and counts["批次"] == 1 and counts["指令"] >= 1 and counts["样品"] == 2
    # 预览不落库
    assert operator.get(f"/api/batches/{batch_id}").status_code == 200

    assert admin.post(f"/api/admin/force-delete/plan/{plan_id}", {
        "reason": " ", "signature_id": admin.sign("强制删除", target=f"plan:{plan_id}"),
    }).status_code == 422
    wrong_target = admin.post(f"/api/admin/force-delete/plan/{plan_id}", {
        "reason": "清理", "signature_id": admin.sign("强制删除", target="plan:EP-205-01"),
    })
    assert wrong_target.status_code in (400, 403, 409, 422) and "签名" in wrong_target.text

    done = _force(admin, "plan", plan_id)
    assert done.status_code == 200, done.text
    assert {row["label"]: row["count"] for row in done.json()["counts"]} == counts

    assert researcher.get(f"/api/plans/{plan_id}").status_code == 404
    assert operator.get(f"/api/batches/{batch_id}").status_code == 404
    assert researcher.get(f"/api/experiment-tasks/{task_id}").status_code == 404
    assert researcher.get("/api/recipes/R-205").status_code == 200, "方案引用的流程不受影响"
    db.expire_all()
    assert db.query(Command).filter(Command.batch_id == batch_id).count() == 0
    assert db.query(PhysicalSample).filter(PhysicalSample.id.in_(physical)).count() == 0
    audit = db.query(AuditEvent).filter(AuditEvent.action == "强制删除", AuditEvent.target == f"实验方案 {plan_id}").one()
    assert audit.sign and "联调数据清理" in audit.detail


def test_running_batch_must_be_aborted_first(admin, researcher, qa, operator, reset_runtime, force_delete_on):
    _, _, batch_id = _dispatched_batch(researcher, qa, operator)
    assert operator.get(f"/api/batches/{batch_id}").json()["state"] == "running"

    preview = admin.get(f"/api/admin/force-delete/batch/{batch_id}").json()
    assert preview["blockers"] and "先终止" in preview["blockers"][0]
    refused = _force(admin, "batch", batch_id)
    assert refused.status_code == 409
    assert operator.get(f"/api/batches/{batch_id}").status_code == 200


def test_capability_force_delete_drops_qualifications_and_station_limits(admin, force_delete_on, db):
    from app.models import Capability, Qualification, Station

    capability_id = f"cap.force_{uuid.uuid4().hex[:6]}"
    created = admin.post("/api/capabilities", {
        "id": capability_id, "name": "强制删除用例", "params": {},
        "recovery": {"maxHoldMin": 0, "pausable": False, "retryable": True, "hold": "无", "sideEffect": "无", "verify": []},
        "signature_id": admin.sign("能力模型变更批准", target=capability_id),
    })
    assert created.status_code in (200, 201), created.text
    station = db.get(Station, "ST-02")
    station.limits = {**(station.limits or {}), capability_id: {}}
    db.commit()
    person = next(row for row in admin.get("/api/people").json()["items"] if row["code"] == "P-003")
    granted = admin.post(f"/api/people/{person['id']}/qualifications", {"scope_kind": "capability", "scope_ref": capability_id})
    assert granted.status_code == 201, granted.text
    # 正常删除：有工位声明实现，拒绝
    assert admin.client.delete(f"/api/capabilities/{capability_id}", headers=admin.headers).status_code == 409

    done = _force(admin, "capability", capability_id)
    assert done.status_code == 200, done.text
    counts = {row["label"]: row["count"] for row in done.json()["counts"]}
    assert counts == {"能力资质": 1, "能力": 1}
    db.expire_all()
    assert db.get(Capability, capability_id) is None
    assert capability_id not in (db.get(Station, "ST-02").limits or {})
    assert db.query(Qualification).filter(Qualification.scope_ref == capability_id).count() == 0


def test_recipe_force_delete_takes_plans_built_on_it(admin, researcher, force_delete_on):
    draft = researcher.post("/api/recipes", {"name": "强制删除流程", "plate": 8, "copy_from": "R-205"})
    assert draft.status_code == 201, draft.text
    recipe_id = draft.json()["id"]
    plan = researcher.post("/api/plans", {"name": "引用它的方案", "recipe_id": recipe_id})
    assert plan.status_code == 201, plan.text
    assert researcher.client.delete(f"/api/recipes/{recipe_id}", headers=researcher.headers).status_code == 409

    done = _force(admin, "recipe", recipe_id)
    assert done.status_code == 200, done.text
    assert {row["kind"]: row["ids"] for row in done.json()["cascade"]}["plan"] == [plan.json()["id"]]
    assert researcher.get(f"/api/recipes/{recipe_id}").status_code == 404
    assert researcher.get(f"/api/plans/{plan.json()['id']}").status_code == 404
    assert researcher.get("/api/recipes/R-205").status_code == 200


def test_approved_plan_lists_why_it_cannot_be_deleted(researcher, qa):
    plan_id = _approved_plan(researcher, qa)
    blockers = researcher.get(f"/api/plans/{plan_id}").json()["delete_blockers"]
    assert any("已批准" in text for text in blockers), "列表上的删除按钮要和删除接口一致：已批准的不能删"


def test_alarm_numbers_do_not_come_back_after_alarms_are_deleted(db):
    """删掉的报警编号不再发：审计里的「触发报警」记着用过的编号。"""
    from app.core.context import system_context
    from app.models import Alarm
    from app.services.alarm_service import AlarmService

    service = AlarmService(db, system_context("ORG-001", "用例"))
    first = service.raise_alarm(4, "station", "ST-02", "编号用例：先报一条")
    db.commit()
    number = int(first.id[2:])
    db.query(Alarm).filter(Alarm.id == first.id).delete(synchronize_session=False)
    db.commit()
    second = service.raise_alarm(4, "station", "ST-02", "编号用例：删掉以后再报一条")
    db.commit()
    assert int(second.id[2:]) > number, (first.id, second.id)
    db.query(Alarm).filter(Alarm.id == second.id).delete(synchronize_session=False)
    db.commit()
