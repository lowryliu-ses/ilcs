"""任务树与任务间依赖。

- 拆分：父任务不绑定批次、状态由子任务汇总；顺序拆分时后一份依赖前一份。
- 依赖图不能成环；已下发的批次不能再加上游。
- 完成—开始：上游没排程时下游不能排；下游排在上游计划结束之后；上游没运行结束时下游开跑检查挡住。
- 多批次优化只接受保持依赖顺序的候选。
"""
from datetime import datetime


def _task(researcher, **extra) -> dict:
    created = researcher.post("/api/experiment-tasks", {"plan_id": "EP-205-01", **extra})
    assert created.status_code == 201, created.text
    return created.json()


def _batch(operator, task_id) -> str:
    created = operator.post("/api/batches", {"plan_id": "EP-205-01", "task_id": task_id})
    assert created.status_code == 201, created.text
    return created.json()["id"]


def _work(detail) -> list[dict]:
    return [row for row in detail["allocations"] if row["kind"] == "work"]


def test_decompose_builds_a_tree_and_the_parent_never_runs_itself(researcher, operator, reset_runtime):
    parent = _task(researcher, title="FEC 添加剂筛选订单")
    split = researcher.post(f"/api/experiment-tasks/{parent['id']}/decompose", {"parts": 3, "sequential": True})
    assert split.status_code == 200, split.text
    body = split.json()
    children = body["children"]
    assert len(children) == 3 and body["state"] == "unassigned"
    details = [researcher.get(f"/api/experiment-tasks/{row['id']}").json() for row in children]
    assert details[0]["depends_on"] == [] and details[1]["depends_on"] == [children[0]["id"]]
    assert details[2]["depends_on"] == [children[1]["id"]] and all(row["parent_id"] == parent["id"] for row in details)

    refused = operator.post("/api/batches", {"plan_id": "EP-205-01", "task_id": parent["id"]})
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "task_has_children"
    again = researcher.post(f"/api/experiment-tasks/{parent['id']}/decompose", {"parts": 2})
    assert again.status_code == 409


def test_dependency_cycles_and_self_references_are_rejected(researcher):
    first, second = _task(researcher), _task(researcher)
    assert researcher.put(f"/api/experiment-tasks/{second['id']}/dependencies", {"depends_on": [first["id"]]}).status_code == 200
    cycle = researcher.put(f"/api/experiment-tasks/{first['id']}/dependencies", {"depends_on": [second["id"]]})
    assert cycle.status_code == 409 and cycle.json()["detail"]["code"] == "task_dependency_invalid"
    self_ref = researcher.put(f"/api/experiment-tasks/{first['id']}/dependencies", {"depends_on": [first["id"]]})
    assert self_ref.status_code == 409


def test_downstream_waits_for_upstream_in_scheduling_and_preflight(researcher, operator, reset_runtime):
    upstream, downstream = _task(researcher), _task(researcher)
    assert researcher.put(
        f"/api/experiment-tasks/{downstream['id']}/dependencies", {"depends_on": [upstream["id"]]},
    ).status_code == 200
    up_batch, down_batch = _batch(operator, upstream["id"]), _batch(operator, downstream["id"])

    early = operator.post(f"/api/batches/{down_batch}/schedule", {})
    assert early.status_code == 409 and early.json()["detail"]["code"] == "dependency_unscheduled", "上游没排程，下游定不下开工"

    assert operator.post(f"/api/batches/{up_batch}/schedule", {}).status_code == 200
    assert operator.post(f"/api/batches/{down_batch}/schedule", {}).status_code == 200
    up_end = max(row["ends_at"] for row in _work(operator.get(f"/api/batches/{up_batch}").json()))
    down_start = min(row["starts_at"] for row in _work(operator.get(f"/api/batches/{down_batch}").json()))
    assert datetime.fromisoformat(down_start) >= datetime.fromisoformat(up_end), "完成—开始：上游结束后才开工"

    preflight = operator.get(f"/api/batches/{down_batch}/preflight").json()
    upstream_check = next(row for row in preflight["checks"] if row["key"] == "upstream")
    assert upstream_check["state"] == "blocked" and upstream["id"] in upstream_check["detail"]
    assert researcher.get(f"/api/experiment-tasks/{downstream['id']}").json()["blocked_by"], "任务上也能看到被谁挡住"


def test_optimizer_only_keeps_orders_that_respect_dependencies(researcher, operator, reset_runtime):
    upstream, downstream = _task(researcher), _task(researcher)
    researcher.put(f"/api/experiment-tasks/{downstream['id']}/dependencies", {"depends_on": [upstream["id"]]})
    up_batch, down_batch = _batch(operator, upstream["id"]), _batch(operator, downstream["id"])
    preview = operator.post("/api/schedule/optimize", {"batch_ids": [down_batch, up_batch]})
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["best"]["order"].index(up_batch) < body["best"]["order"].index(down_batch)
    assert body["baseline"]["order"].index(up_batch) < body["baseline"]["order"].index(down_batch)
    plans = body["best"]["plans"]
    up_end = max(row["ends_at"] for row in plans[up_batch] if row["kind"] == "work")
    down_start = min(row["starts_at"] for row in plans[down_batch] if row["kind"] == "work")
    assert down_start >= up_end

    applied = operator.post(
        "/api/schedule/optimize/apply", {"order": body["best"]["order"], "start_from": body["start_from"]},
    )
    assert applied.status_code == 200, applied.text


def test_cancelling_a_parent_cancels_its_children(researcher):
    parent = _task(researcher)
    researcher.post(f"/api/experiment-tasks/{parent['id']}/decompose", {"parts": 2})
    cancelled = researcher.post(f"/api/experiment-tasks/{parent['id']}/cancel", {"reason": "订单撤回"})
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["state"] == "cancelled"
    assert all(row["state"] == "cancelled" for row in cancelled.json()["children"])


def test_upstream_running_late_is_reported_as_a_broken_dependency(researcher, operator, reset_runtime, db):
    from datetime import timedelta

    from app.core.context import system_context
    from app.models import Allocation, Batch
    from app.services.schedule_service import ScheduleService

    upstream, downstream = _task(researcher), _task(researcher)
    researcher.put(f"/api/experiment-tasks/{downstream['id']}/dependencies", {"depends_on": [upstream["id"]]})
    up_batch, down_batch = _batch(operator, upstream["id"]), _batch(operator, downstream["id"])
    assert operator.post(f"/api/batches/{up_batch}/schedule", {}).status_code == 200
    assert operator.post(f"/api/batches/{down_batch}/schedule", {}).status_code == 200
    service = ScheduleService(db, system_context("ORG-001"))
    assert service.dependency_conflicts(db.get(Batch, up_batch)) == []
    # 上游整体顺延 3 小时：下游已排的开工早于上游新的结束
    for row in db.query(Allocation).filter(Allocation.batch_id == up_batch).all():
        row.starts_at += timedelta(hours=3)
        row.ends_at += timedelta(hours=3)
    db.commit()
    conflicts = service.dependency_conflicts(db.get(Batch, up_batch))
    assert conflicts and down_batch in conflicts[0]
