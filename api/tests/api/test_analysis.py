"""AC-06 至 AC-08、AC-29 至 AC-32、AC-34：回传、采集状态、质量与审核。"""
import pytest

CAPACITY = "METRIC-discharge_capacity-v1"
DENSITY = "METRIC-areal_density-v1"
APPEARANCE = "METRIC-appearance-v1"


@pytest.fixture()
def sample_and_tasks(operator, researcher, reset_runtime, request):
    """一个样本 + 两个检测任务：第二个任务不能被第一个的回传带完成。

    库是会话级共享的，样本编号按用例名取，避免用例之间互相干扰。
    """
    sample_id = f"PS-AN-{abs(hash(request.node.name)) % 100000:05d}"
    sample = operator.post(
        "/api/samples",
        {"id": sample_id, "source": "回传用例", "sample_type": "极片", "quantity": "5",
         "unit": "g"},
    )
    assert sample.status_code == 201, sample.text
    first = researcher.post(
        "/api/analysis-tasks",
        {
            "physical_sample_id": sample_id, "method": "电性能测试",
            "method_version": "EC-02 v2", "required_metrics": [CAPACITY, DENSITY],
        },
    )
    assert first.status_code == 201, first.text
    second = researcher.post(
        "/api/analysis-tasks",
        {
            "physical_sample_id": sample_id, "method": "外观检查",
            "method_version": "EC-02 v2", "required_metrics": [APPEARANCE],
        },
    )
    assert second.status_code == 201, second.text
    return {"sample": sample_id, "first": first.json()["id"], "second": second.json()["id"]}


def test_partial_ingest_moves_task_to_collecting_only(lims, researcher, sample_and_tasks):
    """AC-30：分批回传只入账对应指标；第二个任务状态不变。"""
    task_id = sample_and_tasks["first"]
    first = lims.post(
        "/api/integrations/results",
        {
            "event_id": "ec-0001", "task_id": task_id, "sample_id": sample_and_tasks["sample"],
            "parser_version": "ec-parser 2.1", "instrument_serial": "EC-TESTER-0001",
            "metrics": [{"metric_version_id": CAPACITY, "value": 205.4, "unit": "mAh/g"}],
        },
    )
    assert first.status_code == 200, first.text
    assert first.json()["task_state"] == "collecting"
    assert "未进入正式统计" in first.json()["note"]

    other = researcher.get(f"/api/analysis-tasks/{sample_and_tasks['second']}").json()
    assert other["state"] == "pending", "单个指标回传不能完成其他检测任务"

    second = lims.post(
        "/api/integrations/results",
        {
            "event_id": "ec-0002", "task_id": task_id,
            "metrics": [{"metric_version_id": DENSITY, "value": 22.6, "unit": "mg/cm2"}],
        },
    )
    assert second.status_code == 200, second.text
    assert second.json()["task_state"] == "collected"

    detail = researcher.get(f"/api/analysis-tasks/{task_id}").json()
    assert detail["collected"] is True and detail["missing_metrics"] == []
    assert detail["review_pending"] == 2, "采集完成，审核仍待复核"
    assert all(row["quality"] == "unassessed" for row in detail["values"])
    assert all(row["review_state"] == "pending" for row in detail["values"])
    assert all(row["official"] is False for row in detail["values"])


def test_replayed_event_returns_the_original_result(lims, sample_and_tasks):
    """AC-06：同一事件顺序重传返回原事件与原结果，只有一份业务写入。"""
    payload = {
        "event_id": "ec-replay-1", "task_id": sample_and_tasks["first"],
        "metrics": [{"metric_version_id": CAPACITY, "value": 201.0, "unit": "mAh/g"}],
    }
    first = lims.post("/api/integrations/results", payload)
    assert first.status_code == 200 and first.json()["replayed"] is False
    replay = lims.post("/api/integrations/results", payload)
    assert replay.status_code == 200 and replay.json()["replayed"] is True
    assert replay.json()["results"][0]["id"] == first.json()["results"][0]["id"]


def test_same_event_with_different_content_is_rejected(lims, sample_and_tasks):
    """AC-07：同键不同内容返回 409，不重复变更。"""
    task_id = sample_and_tasks["first"]
    assert lims.post(
        "/api/integrations/results",
        {"event_id": "ec-conflict", "task_id": task_id,
         "metrics": [{"metric_version_id": CAPACITY, "value": 200.0, "unit": "mAh/g"}]},
    ).status_code == 200
    conflict = lims.post(
        "/api/integrations/results",
        {"event_id": "ec-conflict", "task_id": task_id,
         "metrics": [{"metric_version_id": CAPACITY, "value": 999.0, "unit": "mAh/g"}]},
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "ingest_content_conflict"


def test_invalid_metric_rejects_the_whole_event(lims, researcher, sample_and_tasks):
    """AC-29：一个事件多指标，其中一项非法则全部不入账。"""
    task_id = sample_and_tasks["first"]
    rejected = lims.post(
        "/api/integrations/results",
        {
            "event_id": "ec-mixed", "task_id": task_id,
            "metrics": [
                {"metric_version_id": CAPACITY, "value": 205.0, "unit": "mAh/g"},
                # 单位不对
                {"metric_version_id": DENSITY, "value": 22.0, "unit": "g/cm2"},
            ],
        },
    )
    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "ingest_rejected"
    detail = researcher.get(f"/api/analysis-tasks/{task_id}").json()
    assert detail["values"] == [], "被拒事件不能留下部分结果"

    ok = lims.post(
        "/api/integrations/results",
        {
            "event_id": "ec-mixed-ok", "task_id": task_id,
            "metrics": [
                {"metric_version_id": CAPACITY, "value": 205.0, "unit": "mAh/g"},
                {"metric_version_id": DENSITY, "value": 22.0, "unit": "mg/cm2"},
            ],
        },
    )
    assert ok.status_code == 200 and len(ok.json()["results"]) == 2


def test_missing_event_id_and_wrong_task_are_refused(lims, sample_and_tasks):
    """AC-08：缺事件号、错任务、未授权指标一律整次拒绝。"""
    no_event = lims.post(
        "/api/integrations/results",
        {"event_id": "", "task_id": sample_and_tasks["first"],
         "metrics": [{"metric_version_id": CAPACITY, "value": 1, "unit": "mAh/g"}]},
    )
    assert no_event.status_code == 422
    assert no_event.json()["detail"]["code"] == "event_id_required"

    wrong_task = lims.post(
        "/api/integrations/results",
        {"event_id": "ec-wrong-task", "task_id": "AT-不存在",
         "metrics": [{"metric_version_id": CAPACITY, "value": 1, "unit": "mAh/g"}]},
    )
    assert wrong_task.status_code == 404

    wrong_sample = lims.post(
        "/api/integrations/results",
        {"event_id": "ec-wrong-sample", "task_id": sample_and_tasks["first"],
         "sample_id": "PS-别的样本",
         "metrics": [{"metric_version_id": CAPACITY, "value": 1, "unit": "mAh/g"}]},
    )
    assert wrong_sample.status_code == 409
    assert wrong_sample.json()["detail"]["code"] == "sample_mismatch"

    not_required = lims.post(
        "/api/integrations/results",
        {"event_id": "ec-not-required", "task_id": sample_and_tasks["first"],
         "metrics": [{"metric_version_id": APPEARANCE, "value": "合格"}]},
    )
    assert not_required.status_code == 409
    assert "不在该任务冻结的要求集合内" in not_required.json()["detail"]["blocked"][0]["label"]


def test_missing_value_is_not_treated_as_zero(lims, researcher, sample_and_tasks):
    """缺值不当成 0；确实测不到要写原因。"""
    task_id = sample_and_tasks["first"]
    no_value = lims.post(
        "/api/integrations/results",
        {"event_id": "ec-no-value", "task_id": task_id,
         "metrics": [{"metric_version_id": CAPACITY, "unit": "mAh/g"}]},
    )
    assert no_value.status_code == 409
    assert "缺值不当成 0" in no_value.json()["detail"]["blocked"][0]["label"]

    declared = lims.post(
        "/api/integrations/results",
        {"event_id": "ec-not-measured", "task_id": task_id,
         "metrics": [{"metric_version_id": CAPACITY,
                      "not_measured_reason": "电芯短路，无法完成充放电"}]},
    )
    assert declared.status_code == 200
    values = researcher.get(f"/api/analysis-tasks/{task_id}").json()["values"]
    assert values[0]["value"] is None
    assert "电芯短路" in values[0]["not_measured_reason"]


def test_duplicate_metric_needs_an_explicit_revision(lims, researcher, sample_and_tasks):
    """AC-32：正常采集重复冲突 409；修订要显式引用原版本并说明原因。"""
    task_id = sample_and_tasks["first"]
    assert lims.post(
        "/api/integrations/results",
        {"event_id": "ec-dup-1", "task_id": task_id,
         "metrics": [{"metric_version_id": CAPACITY, "value": 205.0, "unit": "mAh/g"}]},
    ).status_code == 200
    conflict = lims.post(
        "/api/integrations/results",
        {"event_id": "ec-dup-2", "task_id": task_id,
         "metrics": [{"metric_version_id": CAPACITY, "value": 208.0, "unit": "mAh/g"}]},
    )
    assert conflict.status_code == 409
    assert "修订请用" in conflict.json()["detail"]["blocked"][0]["label"]

    values = researcher.get(f"/api/analysis-tasks/{task_id}").json()["values"]
    original = values[0]
    revised = researcher.post(
        f"/api/result-values/{original['id']}/revisions",
        {"value": 208.0, "unit": "mAh/g", "reason": "重新按标准曲线换算"},
    )
    assert revised.status_code == 201, revised.text
    assert revised.json()["result_version"] == 2
    assert revised.json()["revises"]["id"] == original["id"]

    after = researcher.get(f"/api/analysis-tasks/{task_id}").json()["values"]
    kept = next(row for row in after if row["result_version"] == 1)
    assert kept["superseded_by_id"] == revised.json()["id"], "旧版本保留并标明被取代"


def test_researcher_cannot_review_results_at_all(researcher, sample_and_tasks):
    """研究员没有复核权限：动作权限先挡一层。"""
    task_id = sample_and_tasks["first"]
    entered = researcher.post(
        f"/api/analysis-tasks/{task_id}/results",
        {"event_id": "manual-entry-perm",
         "metrics": [{"metric_version_id": CAPACITY, "value": 204.0, "unit": "mAh/g"}]},
    )
    value_id = entered.json()["results"][0]["id"]
    denied = researcher.post(
        f"/api/result-values/{value_id}/review",
        {"conclusion": "approved", "quality": "valid",
         "signature_id": researcher.sign("复核", target=value_id, object_version=1)},
    )
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "permission_denied"


def test_admin_cannot_review_own_entry(admin, qa, sample_and_tasks):
    """AC-32 前半句：管理员同时有录入与复核权限，也不能审自己录的记录。"""
    task_id = sample_and_tasks["first"]
    entered = admin.post(
        f"/api/analysis-tasks/{task_id}/results",
        {"event_id": "manual-entry-admin",
         "metrics": [{"metric_version_id": CAPACITY, "value": 204.0, "unit": "mAh/g"}]},
    )
    assert entered.status_code == 200, entered.text
    value_id = entered.json()["results"][0]["id"]

    self_review = admin.post(
        f"/api/result-values/{value_id}/review",
        {"conclusion": "approved", "quality": "valid",
         "signature_id": admin.sign("复核", target=value_id, object_version=1)},
    )
    assert self_review.status_code == 403
    assert self_review.json()["detail"]["code"] == "self_review_denied"

    by_qa = qa.post(
        f"/api/result-values/{value_id}/review",
        {"conclusion": "approved", "quality": "valid", "reason": "曲线正常",
         "signature_id": qa.sign("复核", target=value_id, object_version=1)},
    )
    assert by_qa.status_code == 200, by_qa.text


def test_approval_requires_a_quality_verdict(researcher, qa, sample_and_tasks):
    """DEV-12.6：通过审核必须同时给出质量判定。"""
    task_id = sample_and_tasks["first"]
    entered = researcher.post(
        f"/api/analysis-tasks/{task_id}/results",
        {"event_id": "manual-entry-1",
         "metrics": [{"metric_version_id": CAPACITY, "value": 204.0, "unit": "mAh/g"}]},
    )
    assert entered.status_code == 200, entered.text
    value_id = entered.json()["results"][0]["id"]

    no_quality = qa.post(
        f"/api/result-values/{value_id}/review",
        {"conclusion": "approved",
         "signature_id": qa.sign("复核", target=value_id, object_version=1)},
    )
    assert no_quality.status_code == 422
    assert no_quality.json()["detail"]["code"] == "quality_required"

    approved = qa.post(
        f"/api/result-values/{value_id}/review",
        {"conclusion": "approved", "quality": "valid", "reason": "曲线正常",
         "signature_id": qa.sign("复核", target=value_id, object_version=1)},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["official"] is True
    assert approved.json()["reviews"][0]["reviewer_name"] == qa.user["display_name"]


def test_approved_but_invalid_result_is_excluded_from_official_statistics(
    researcher, qa, sample_and_tasks
):
    """AC-34：审核通过但质量 invalid 的结果默认排除，并显示理由。"""
    task_id = sample_and_tasks["first"]
    entered = researcher.post(
        f"/api/analysis-tasks/{task_id}/results",
        {"event_id": "manual-entry-invalid",
         "metrics": [{"metric_version_id": CAPACITY, "value": 90.0, "unit": "mAh/g"}]},
    )
    value_id = entered.json()["results"][0]["id"]
    reviewed = qa.post(
        f"/api/result-values/{value_id}/review",
        {"conclusion": "approved", "quality": "invalid", "reason": "电芯内短路，数据无效",
         "signature_id": qa.sign("复核", target=value_id, object_version=1)},
    )
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["review_state"] == "approved"
    assert reviewed.json()["quality"] == "invalid"
    assert reviewed.json()["official"] is False, "审核完成不等于质量有效"


def test_signature_cannot_be_reused_on_another_object(researcher, qa, sample_and_tasks):
    """AC-36 后半句：为一个对象签发的票据不能挪用到另一个对象。"""
    task_id = sample_and_tasks["first"]
    first = researcher.post(
        f"/api/analysis-tasks/{task_id}/results",
        {"event_id": "manual-sig-1",
         "metrics": [{"metric_version_id": CAPACITY, "value": 200.0, "unit": "mAh/g"},
                     {"metric_version_id": DENSITY, "value": 22.0, "unit": "mg/cm2"}]},
    )
    assert first.status_code == 200, first.text
    a, b = first.json()["results"][0]["id"], first.json()["results"][1]["id"]

    ticket = qa.sign("复核", target=a, object_version=1)
    misused = qa.post(
        f"/api/result-values/{b}/review",
        {"conclusion": "approved", "quality": "valid", "signature_id": ticket},
    )
    assert misused.status_code == 400
    assert "不能用于" in misused.json()["detail"]["message"]

    correct = qa.post(
        f"/api/result-values/{a}/review",
        {"conclusion": "approved", "quality": "valid", "signature_id": ticket},
    )
    assert correct.status_code == 200, "正确对象上的有效签名可以一次成功消费"


def test_retest_creates_a_new_task_and_round(researcher, sample_and_tasks):
    """AC-32 中段：重测创建新检测任务与新轮次，不覆盖原任务。"""
    task_id = sample_and_tasks["first"]
    before = researcher.get(f"/api/analysis-tasks/{task_id}").json()
    retest = researcher.post(
        f"/api/analysis-tasks/{task_id}/retests", {"reason": "复现实验"}
    )
    assert retest.status_code == 201, retest.text
    assert retest.json()["retest_of"] == task_id
    assert retest.json()["round_no"] > before["round_no"]
    assert researcher.get(f"/api/analysis-tasks/{task_id}").json()["state"] == before["state"]


def test_metric_definition_lifecycle_is_reachable_and_versioned(researcher, reset_runtime):
    """指标定义页新接的动作：登记、改本版、修订、停用，以及「被引用后只能修订」。"""
    created = researcher.post(
        "/api/metrics",
        {"code": "peel_strength", "name": "剥离强度", "version": "v1", "value_type": "number",
         "unit": "N/m", "rules": {"min": 0, "max": 500}},
    )
    assert created.status_code == 201, created.text
    metric = created.json()
    assert metric["id"] == "METRIC-peel_strength-v1"
    assert metric["editable"] is True and metric["referenced_by"] == 0

    # 数值指标必须有单位：没有单位的数字无法比较
    no_unit = researcher.post(
        "/api/metrics",
        {"code": "no_unit", "name": "缺单位", "value_type": "number", "unit": ""},
    )
    assert no_unit.status_code == 422

    # 同代码同版本不能重复登记
    dup = researcher.post(
        "/api/metrics",
        {"code": "peel_strength", "name": "剥离强度", "version": "v1", "unit": "N/m"},
    )
    assert dup.status_code == 409

    renamed = researcher.patch(
        f"/api/metrics/{metric['id']}", {"name": "剥离强度（90°）", "rules": {"min": 0, "max": 600}}
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "剥离强度（90°）"

    revised = researcher.post(
        f"/api/metrics/{metric['id']}/revisions",
        {"code": "peel_strength", "name": "剥离强度", "version": "v2", "value_type": "number",
         "unit": "N/m", "method_version": "PS-02"},
    )
    assert revised.status_code == 201, revised.text
    assert revised.json()["id"] == "METRIC-peel_strength-v2"
    assert researcher.get(f"/api/metrics").status_code == 200

    retired = researcher.post(f"/api/metrics/{metric['id']}/retire")
    assert retired.status_code == 200, retired.text
    active = researcher.get("/api/metrics?only_active=true").json()
    assert metric["id"] not in [row["id"] for row in active], "停用的版本不再出现在在用清单"
    assert "METRIC-peel_strength-v2" in [row["id"] for row in active]


def test_seeded_metric_in_use_cannot_be_edited_only_revised(researcher, reset_runtime):
    """被结果引用过的指标版本不能改口径——那等于回头改已采集数据的含义。"""
    rows = researcher.get("/api/metrics").json()
    in_use = [row for row in rows if row["referenced_by"]]
    assert in_use, "种子里应当有已被结果引用的指标"
    target = in_use[0]
    assert target["editable"] is False

    rejected = researcher.patch(f"/api/metrics/{target['id']}", {"name": "改个名字"})
    assert rejected.status_code == 409, rejected.text


def test_analysis_task_cancel_needs_a_reason_and_stops_at_collected(
    researcher, lims, sample_and_tasks
):
    """取消检测任务要理由；采集完成后不能用取消把已有数据一笔带过。"""
    open_task = sample_and_tasks["second"]
    no_reason = researcher.post(f"/api/analysis-tasks/{open_task}/cancel", {"reason": ""})
    assert no_reason.status_code == 422, no_reason.text

    cancelled = researcher.post(
        f"/api/analysis-tasks/{open_task}/cancel", {"reason": "样本破损，改期重测"}
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["state"] == "cancelled"

    # 把第一个任务的指标补齐到 collected，再试取消
    first = sample_and_tasks["first"]
    ingested = lims.post(
        "/api/integrations/results",
        {
            "event_id": f"cancel-case-{first}", "task_id": first, "parser_version": "p1",
            "metrics": [
                {"metric_version_id": CAPACITY, "value": 201.0, "unit": "mAh/g"},
                {"metric_version_id": DENSITY, "value": 22.0, "unit": "mg/cm2"},
            ],
        },
    )
    assert ingested.status_code == 200, ingested.text
    assert ingested.json()["task_state"] == "collected"

    rejected = researcher.post(f"/api/analysis-tasks/{first}/cancel", {"reason": "想撤掉"})
    assert rejected.status_code == 409, rejected.text
    assert "复核与更正" in rejected.json()["detail"]["message"]
