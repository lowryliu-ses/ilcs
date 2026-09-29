"""训练数据快照与分析运行：结果后来被更正，按快照导出不变；提案能追到用了哪份数据、哪次运行。"""
import csv
import io
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from test_campaign_proposals import _points, campaign  # noqa: F401  （复用 fixture）


def _rows(response) -> list[dict]:
    assert response.status_code == 200, response.text
    return list(csv.DictReader(io.StringIO(response.text.lstrip("﻿"))))


@pytest.fixture()
def official_results(operator, reset_runtime, db):
    """EP-205-01 的一个新批次：一条正式结果、一条待复核、一条质量无效。"""
    from app.models import ResultValue, Sample

    batch_id = operator.post("/api/batches", {"plan_id": "EP-205-01"}).json()["id"]
    samples = db.query(Sample).filter(Sample.batch_id == batch_id).order_by(Sample.position).all()
    created = {}
    for label, sample, review, quality, value in (
        ("official", samples[0], "approved", "valid", 201.5), ("pending", samples[1], "pending", "unassessed", 199.0),
        ("invalid", samples[2], "approved", "invalid", 150.0),
    ):
        row = ResultValue(
            org_id="ORG-001", analysis_task_id=f"T-SNAP-{sample.id}", physical_sample_id=sample.physical_sample_id,
            assignment_id=sample.id, metric_definition_id="METRIC-discharge_capacity-v1",
            value_num=value, unit="mAh/g", review_state=review, quality=quality,
        )
        db.add(row)
        db.flush()
        created[label] = row.id
    db.commit()
    return {"batch_id": batch_id, **created}


def _correct(db, result_id: str, value: float) -> str:
    """更正一条结果：新版本取代旧版本（与结果更正同一条链）。"""
    from app.models import ResultValue

    old = db.get(ResultValue, result_id)
    new = ResultValue(
        org_id=old.org_id, analysis_task_id=old.analysis_task_id, physical_sample_id=old.physical_sample_id,
        assignment_id=old.assignment_id, metric_definition_id=old.metric_definition_id, value_num=value,
        unit=old.unit, review_state="approved", quality="valid", result_version=old.result_version + 1,
        revises_id=old.id,
    )
    db.add(new)
    db.flush()
    old.superseded_by_id = new.id
    db.commit()
    return new.id


def test_snapshot_export_is_frozen_after_a_result_is_corrected(researcher, official_results, db):
    batch_id = official_results["batch_id"]
    key = f"snap-{uuid.uuid4().hex[:8]}"
    created = researcher.post("/api/plans/EP-205-01/datasets", {"key": key, "note": "第 2 轮训练"})
    assert created.status_code == 201, created.text
    snapshot = created.json()
    assert snapshot["excluded_count"] >= 2 and len(snapshot["digest"]) == 64

    frozen = [row for row in _rows(researcher.get(
        f"/api/plans/EP-205-01/datasets/{snapshot['id']}/export.csv")) if row["batch_id"] == batch_id]
    assert [row["value"] for row in frozen] == ["201.5"], "只纳入复核通过且质量有效的当前版本"

    corrected = _correct(db, official_results["official"], 190.0)
    again = [row for row in _rows(researcher.get(
        f"/api/plans/EP-205-01/datasets/{snapshot['id']}/export.csv")) if row["batch_id"] == batch_id]
    assert again == frozen, "结果更正之后，按快照导出的内容不变"
    live = [row for row in _rows(researcher.get("/api/plans/EP-205-01/dataset.csv")) if row["batch_id"] == batch_id]
    assert [row["value"] for row in live] == ["190.0"] and live[0]["result_value_id"] == corrected

    detail = researcher.get(f"/api/plans/EP-205-01/datasets/{snapshot['id']}").json()
    changed = [row for row in detail["changed"] if row["result_value_id"] == official_results["official"]]
    assert changed and changed[0]["reason"] == "superseded" and changed[0]["superseded_by_id"] == corrected
    reasons = {row["result_value_id"]: row["reason"] for row in detail["exclusions"]}
    assert reasons[official_results["pending"]] == "pending_review"
    assert reasons[official_results["invalid"]] == "invalid"
    listed = next(row for row in researcher.get("/api/plans/EP-205-01/datasets").json() if row["id"] == snapshot["id"])
    assert listed["changed_count"] >= 1

    replay = researcher.post("/api/plans/EP-205-01/datasets", {"key": key})
    assert replay.status_code == 409 and replay.json()["detail"]["code"] == "snapshot_key_conflict", (
        "同号快照数据已变：不能悄悄回放旧快照，也不能覆盖"
    )


def test_snapshot_rows_cannot_be_updated_or_deleted(researcher, db):
    snapshot = researcher.post("/api/plans/EP-205-01/datasets", {}).json()
    for statement in ("UPDATE dataset_snapshots SET note = 'x' WHERE id = :id", "DELETE FROM dataset_snapshots WHERE id = :id"):
        with pytest.raises(DBAPIError):
            db.execute(text(statement), {"id": snapshot["id"]})
        db.rollback()


def test_analysis_run_links_snapshot_to_proposal(researcher, campaign):
    snapshot = researcher.post(f"/api/plans/{campaign}/datasets", {}).json()
    body = {
        "run_id": f"run-{uuid.uuid4().hex[:8]}", "snapshot_id": snapshot["id"], "program": "bo-loop",
        "program_version": "1.4.0", "model_version": "gp-matern-3", "params": {"acquisition": "EI"},
        "seed": 42, "outputs": {"suggested": 2},
    }
    run = researcher.post(f"/api/plans/{campaign}/analysis-runs", body)
    assert run.status_code == 201, run.text
    replay = researcher.post(f"/api/plans/{campaign}/analysis-runs", body)
    assert replay.status_code == 201 and replay.json()["replayed"] is True
    conflict = researcher.post(f"/api/plans/{campaign}/analysis-runs", {**body, "seed": 7})
    assert conflict.status_code == 409

    proposal = researcher.post(f"/api/plans/{campaign}/proposals", {
        "proposal_id": f"p-{uuid.uuid4().hex[:8]}", "source": "bo-loop", "points": _points((2, 60), (5, 70)),
        "analysis_run_id": run.json()["id"],
    })
    assert proposal.status_code == 201, proposal.text
    assert proposal.json()["analysis_run_id"] == run.json()["id"]
    runs = researcher.get(f"/api/plans/{campaign}/analysis-runs").json()
    assert any(row["id"] == run.json()["id"] and row["snapshot_id"] == snapshot["id"] for row in runs)

    missing = researcher.post(f"/api/plans/{campaign}/proposals", {
        "proposal_id": f"p-{uuid.uuid4().hex[:8]}", "points": _points((2, 60)), "analysis_run_id": "nope",
    })
    assert missing.status_code == 404


def test_service_identity_snapshot_needs_plan_scope(device, db):
    from app.models import ServiceIdentity

    denied = device.post("/api/runtime/plans/EP-205-01/datasets", {})
    assert denied.status_code == 403
    identity = db.query(ServiceIdentity).filter(ServiceIdentity.source == "executor-sim").one()
    original = dict(identity.scopes or {})
    identity.scopes = {**original, "plan_proposals": ["EP-205-01"]}
    db.commit()
    try:
        created = device.post("/api/runtime/plans/EP-205-01/datasets", {"key": f"svc-{uuid.uuid4().hex[:8]}"})
        assert created.status_code == 201, created.text
        first = device.get(f"/api/runtime/plans/EP-205-01/datasets/{created.json()['id']}/export.csv")
        second = device.get(f"/api/runtime/plans/EP-205-01/datasets/{created.json()['id']}/export.csv")
        assert first.status_code == 200 and first.text == second.text, "按快照读取完全一致"
    finally:
        db.expire_all()
        db.query(ServiceIdentity).filter(ServiceIdentity.source == "executor-sim").one().scopes = original
        db.commit()
