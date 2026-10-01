"""曲线型检测值：登记曲线指标（声明派生的数值指标）→ 回传 / 录入曲线 → 缩略与完整数据 → 更正后重新派生 →
复核 → 结果分析叠加 → 报告里的「曲线」一节；设备回报的曲线按孔位写成结果。"""
from uuid import uuid4

import pytest

from test_reports import approve_plan

CURVE = [[0, 4.2], [40, 4.0], [90, 3.8], [150, 3.4], [180, 2.8]]


@pytest.fixture()
def finished_batch(researcher, qa, operator, reset_runtime, executor):
    approve_plan(researcher, qa, "EP-205-01")
    batch_id = operator.post("/api/batches", {"plan_id": "EP-205-01"}).json()["id"]
    assert operator.post(f"/api/batches/{batch_id}/schedule", {}).status_code == 200
    assert operator.post(
        f"/api/batches/{batch_id}/dispatch",
        {"manual_review": True, "signature_id": operator.sign("批准执行", target=batch_id)},
    ).status_code == 200
    for _ in range(12):
        executor()
    detail = operator.get(f"/api/batches/{batch_id}").json()
    assert detail["state"] == "done", detail["failure_reason"]
    return detail


def _metrics(admin) -> tuple[dict, dict]:
    suffix = uuid4().hex[:6]
    capacity = admin.post("/api/metrics", {"code": f"cap_cut_{suffix}", "name": "截止比容量", "value_type": "number",
                                           "unit": "mAh/g"})
    assert capacity.status_code == 201, capacity.text
    # 派生的取法与单位对不上（取 y 却要写成 mAh/g 的指标）当场拒绝
    wrong = admin.post("/api/metrics", {
        "code": f"bad_curve_{suffix}", "name": "错的曲线", "value_type": "series", "unit": "V",
        "rules": {"x_unit": "mAh/g", "derived": [{"metric": capacity.json()["code"], "of": "last_y"}]},
    })
    assert wrong.status_code == 422 and "对不上" in wrong.text, wrong.text
    missing = admin.post("/api/metrics", {
        "code": f"bad2_curve_{suffix}", "name": "错的曲线", "value_type": "series", "unit": "V",
        "rules": {"derived": [{"metric": "nope_metric", "of": "last_x"}]},
    })
    assert missing.status_code == 422 and "还没登记" in missing.text
    curve = admin.post("/api/metrics", {
        "code": f"dis_curve_{suffix}", "name": "放电曲线", "value_type": "series", "unit": "V",
        "rules": {"x_label": "比容量", "x_unit": "mAh/g", "max_points": 1000,
                  "derived": [{"metric": capacity.json()["code"], "of": "last_x"}]},
    })
    assert curve.status_code == 201, curve.text
    return curve.json(), capacity.json()


def test_curve_results_from_ingest_to_report(admin, researcher, qa, lims, finished_batch):
    curve, capacity = _metrics(admin)
    batch_id = finished_batch["id"]
    sample = finished_batch["samples"][0]
    task = researcher.post("/api/analysis-tasks", {
        "sample_id": sample["id"], "physical_sample_id": sample["physical_sample_id"], "method": "充放电测试",
        "method_version": "CYC-01", "required_metrics": [curve["id"], capacity["id"]],
    })
    assert task.status_code == 201, task.text
    task_id = task.json()["id"]

    # 写法不成立整次拒收，不留部分结果
    broken = lims.post("/api/integrations/results", {
        "event_id": f"curve-bad-{uuid4().hex[:6]}", "task_id": task_id,
        "metrics": [{"metric_version_id": curve["id"], "value": {"x": [0, 1, 2], "y": [4.2, 3.9]}, "unit": "V"}],
    })
    assert broken.status_code == 409 and "长短不一" in broken.text, broken.text
    too_long = lims.post("/api/integrations/results", {
        "event_id": f"curve-long-{uuid4().hex[:6]}", "task_id": task_id,
        "metrics": [{"metric_version_id": curve["id"], "value": {"x": list(range(1001)), "y": list(range(1001))}}],
    })
    assert too_long.status_code == 409 and "超过指标允许的 1000 点" in too_long.text

    ingested = lims.post("/api/integrations/results", {
        "event_id": f"curve-{uuid4().hex[:6]}", "task_id": task_id, "parser_version": "cyc-csv 1",
        "metrics": [{"metric_version_id": curve["id"], "value": CURVE, "unit": "V"}],
    })
    assert ingested.status_code == 200, ingested.text
    body = ingested.json()
    assert body["task_state"] == "collected", "曲线 + 派生的截止比容量：要求指标齐了"
    by_metric = {row["metric_definition_id"]: row for row in body["results"]}
    assert set(by_metric) == {curve["id"], capacity["id"]} and body["derived_notes"] == []
    derived = by_metric[capacity["id"]]
    assert derived["quality"] == "unassessed", "「由曲线派生」是来历说明，不让值变可疑"
    assert [flag["code"] for flag in derived["flags"]] == ["derived"]

    detail = researcher.get(f"/api/analysis-tasks/{task_id}").json()
    values = {row["metric_definition_id"]: row for row in detail["values"]}
    stored = values[curve["id"]]
    assert stored["value"] is None and stored["display"] == "曲线 5 点（x 0–180 mAh/g，y 2.8–4.2 V）"
    assert stored["series"]["points"] == 5 and stored["series"]["x_unit"] == "mAh/g"
    assert stored["series"]["preview"][0]["x"] == [0, 40, 90, 150, 180]
    assert values[capacity["id"]]["value"] == 180.0
    required = {row["id"]: row for row in detail["required_metrics"]}
    assert required[curve["id"]]["value_type"] == "series" and required[curve["id"]]["x_label"] == "比容量"
    full = researcher.get(f"/api/result-values/{stored['id']}/series")
    assert full.status_code == 200 and full.json()["traces"][0]["y"] == [4.2, 4.0, 3.8, 3.4, 2.8]
    assert researcher.get(f"/api/result-values/{values[capacity['id']]['id']}/series").status_code == 409

    # 更正曲线：派生的截止比容量跟着出新版本（旧版保留）
    revised = researcher.post(f"/api/result-values/{stored['id']}/revisions", {
        "value": {"x": [0, 40, 90, 150, 175], "y": [4.2, 4.0, 3.8, 3.4, 2.8]}, "unit": "V", "reason": "截止点读错一行",
    })
    assert revised.status_code == 201, revised.text
    assert revised.json()["rederived"] == [capacity["code"]]
    current = {row["metric_definition_id"]: row for row in researcher.get(f"/api/analysis-tasks/{task_id}").json()["values"]
               if not row["superseded_by_id"]}
    assert current[capacity["id"]]["value"] == 175.0 and current[capacity["id"]]["result_version"] == 2
    assert current[capacity["id"]]["provenance"] == "correction"

    for row in current.values():
        reviewed = qa.post(f"/api/result-values/{row['id']}/review", {
            "conclusion": "approved", "quality": "valid", "reason": "曲线正常",
            "signature_id": qa.sign("复核", target=row["id"], object_version=row["result_version"]),
        })
        assert reviewed.status_code == 200, reviewed.text

    view = researcher.get(f"/api/results/{batch_id}").json()
    assert [row["metric_id"] for row in view["series_metrics"]] == [curve["id"]]
    assert curve["id"] not in {row["metric_id"] for row in view["non_numeric_metrics"]}
    block = next(row for row in view["metrics"] if row["metric_id"] == capacity["id"])
    assert block["summary"]["included"] == 1, "派生的数值照常进正式统计"
    overlay = researcher.get(f"/api/results/{batch_id}/series", params={"metric_id": curve["id"]}).json()
    assert [row["assignment_id"] for row in overlay["samples"]] == [sample["id"]]
    assert overlay["samples"][0]["traces"][0]["x"][-1] == 175 and overlay["excluded"] == []
    assert researcher.get(f"/api/results/{batch_id}/series", params={"metric_id": capacity["id"]}).status_code == 409

    from app.services.report_pdf import render

    report = researcher.post("/api/reports", {"batch_id": batch_id})
    assert report.status_code == 201, report.text
    content = report.json()["content"]
    assert "curves" in content["template"]["sections"]
    chart = next(row for row in content["curves"] if row["metric_name"] == "放电曲线")
    assert chart["total"] == 1 and chart["x_unit"] == "mAh/g" and chart["traces"][0]["x"][-1] == 175
    assert render(content).startswith(b"%PDF")


def test_device_reported_curves_are_written_per_well(admin, db, finished_batch):
    """设备回报的曲线按孔位写成曲线结果，派生的数值指标一起写（要求指标冻结时就含它）。"""
    from app.core.context import system_context
    from app.models import Batch, Command, ResultValue
    from app.services.device_result_service import DeviceResultService

    curve, capacity = _metrics(admin)
    batch = db.get(Batch, finished_batch["id"])
    command = next(row for row in db.query(Command).filter(Command.batch_id == batch.id).all() if row.type == "dispatch")
    snapshot = dict(batch.recipe_snapshot)
    base = next(step for step in snapshot["steps"] if step.get("kind", "device") == "device")
    step = {**base, "method": {**(base.get("method") or {}), "code": "CYC", "version": 1, "outputs": [
        {"key": "curve", "kind": "series", "unit": "V", "metric_id": curve["id"]},
    ]}}
    snapshot["steps"] = [*snapshot["steps"], step]
    batch.recipe_snapshot = snapshot
    db.flush()
    service = DeviceResultService(db, system_context(batch.org_id))
    targets = sorted(service.analysis.assignments.for_batch(batch.id), key=lambda row: row.well)[:2]
    delivered = {"wells": {sample.well: {"curve": {"x": [0, 50, 100 + index], "y": [4.1, 3.7, 3.0]}}
                           for index, sample in enumerate(targets)}}
    outcome = service.record(batch, command, step, delivered, "device")
    assert outcome["problems"] == [] and outcome["samples"] >= 2, outcome
    db.flush()
    rows = db.query(ResultValue).filter(ResultValue.metric_definition_id.in_([curve["id"], capacity["id"]])).all()
    by_sample = {}
    for row in rows:
        by_sample.setdefault(row.assignment_id, {})[row.metric_definition_id] = row
    for index, sample in enumerate(targets):
        written = by_sample[sample.id]
        assert written[curve["id"]].value_series["traces"][0]["x"] == [0.0, 50.0, 100.0 + index]
        assert written[capacity["id"]].value_num == 100.0 + index
        assert any(flag["code"] == "derived" for flag in written[capacity["id"]].flags)
    db.rollback()
