"""闭环实验：外部优化器提案 → 设计空间校验 → 方案草稿；训练数据只导出正式结果。"""
import csv
import io
import uuid

import pytest

DESIGN_SPACE = {
    "bounds": {"FEC 含量": {"min": 0, "max": 10}, "注液量": {"min": 40, "max": 80}},
    "forbidden": [{"FEC 含量": 10, "注液量": 40}],
    "max_points": 8,
}


@pytest.fixture()
def campaign(db):
    from app.models import Plan

    plan = db.get(Plan, "EP-205-01")
    assert plan.approval_state == "approved"
    original = plan.design_space
    plan.design_space = DESIGN_SPACE
    db.commit()
    yield plan.id
    db.expire_all()
    db.get(Plan, "EP-205-01").design_space = original
    db.commit()


def _points(*pairs):
    return [{"FEC 含量": fec, "注液量": volume} for fec, volume in pairs]


def test_accepted_proposal_becomes_a_draft_plan_needing_approval(researcher, campaign):
    key = f"bo-{uuid.uuid4().hex[:8]}"
    submitted = researcher.post(f"/api/plans/{campaign}/proposals", {
        "proposal_id": key, "source": "BO-GP", "model_version": "gp-2026.09",
        "rationale": "EI 最大的 3 个点", "points": _points((1.5, 55), (3, 62), (7.5, 70)),
    })
    assert submitted.status_code == 201, submitted.text
    body = submitted.json()
    assert body["state"] == "accepted" and body["created_plan_id"]

    draft = researcher.get(f"/api/plans/{body['created_plan_id']}").json()
    assert draft["approval_state"] == "draft", "提案不直接变成可执行的实验"
    assert draft["parent_plan_id"] == campaign and draft["round_no"] == 2
    assert draft["design_points"] == [[1.5, 55], [3, 62], [7.5, 70]]
    assert draft["condition_count"] == 3, "条件就是提案的点，不做全因子组合"
    assert {row["key"]: row for row in draft["checks"]}["design_space"]["ok"]

    replay = researcher.post(f"/api/plans/{campaign}/proposals", {
        "proposal_id": key, "source": "BO-GP", "model_version": "gp-2026.09",
        "rationale": "EI 最大的 3 个点", "points": _points((1.5, 55), (3, 62), (7.5, 70)),
    })
    assert replay.json()["replayed"] is True and replay.json()["created_plan_id"] == body["created_plan_id"]
    changed = researcher.post(f"/api/plans/{campaign}/proposals", {
        "proposal_id": key, "source": "BO-GP", "points": _points((2, 50)),
    })
    assert changed.status_code == 409


def test_out_of_space_proposal_is_rejected_and_kept(researcher, campaign):
    key = f"bo-{uuid.uuid4().hex[:8]}"
    rejected = researcher.post(f"/api/plans/{campaign}/proposals", {
        "proposal_id": key, "source": "BO-GP",
        "points": _points((12, 60), (10, 40), (5, 60)),
    })
    assert rejected.status_code == 422
    issues = "；".join(rejected.json()["detail"]["issues"])
    assert "高于设计空间上限 10" in issues and "禁止组合" in issues
    stored = {row["proposal_id"]: row for row in researcher.get(f"/api/plans/{campaign}/proposals").json()}
    assert stored[key]["state"] == "rejected", "被拒绝的提案也留档"


def test_optimizer_service_needs_plan_scope(device, campaign, db):
    from app.models import ServiceIdentity

    body = {"proposal_id": f"svc-{uuid.uuid4().hex[:8]}", "source": "optimizer", "points": _points((2, 60))}
    denied = device.post(f"/api/runtime/plans/{campaign}/proposals", body)
    assert denied.status_code == 403

    identity = db.query(ServiceIdentity).filter(ServiceIdentity.source == "executor-sim").one()
    original = dict(identity.scopes or {})
    identity.scopes = {**original, "plan_proposals": [campaign]}
    db.commit()
    try:
        accepted = device.post(f"/api/runtime/plans/{campaign}/proposals", body)
        assert accepted.status_code == 201, accepted.text
    finally:
        db.expire_all()
        db.query(ServiceIdentity).filter(ServiceIdentity.source == "executor-sim").one().scopes = original
        db.commit()


def test_dataset_exports_only_official_results(operator, researcher, reset_runtime, db):
    from app.models import ResultValue, Sample

    batch_id = operator.post("/api/batches", {"plan_id": "EP-205-01"}).json()["id"]
    samples = db.query(Sample).filter(Sample.batch_id == batch_id).order_by(Sample.position).all()
    official, pending, invalid = samples[:3]
    for sample, review, quality, value in (
        (official, "approved", "valid", 201.5), (pending, "pending", "unassessed", 199.0),
        (invalid, "approved", "invalid", 150.0),
    ):
        db.add(ResultValue(
            org_id="ORG-001", analysis_task_id=f"T-DATASET-{sample.id}", physical_sample_id=sample.physical_sample_id,
            assignment_id=sample.id, metric_definition_id="METRIC-discharge_capacity-v1",
            value_num=value, unit="mAh/g", review_state=review, quality=quality,
        ))
    db.commit()

    exported = researcher.get("/api/plans/EP-205-01/dataset.csv")
    assert exported.status_code == 200
    rows = list(csv.DictReader(io.StringIO(exported.text.lstrip("﻿"))))
    mine = [row for row in rows if row["batch_id"] == batch_id]
    assert [row["sample_id"] for row in mine] == [official.id], "只导出复核通过且质量有效的结果"
    assert mine[0]["factor:注液量"] != "" and mine[0]["value"] == "201.5"
