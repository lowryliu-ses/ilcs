"""AC-33 至 AC-36、AC-38：SOP、正式统计、报告审签与全流程。"""
from datetime import datetime, timedelta

import pytest

CAPACITY = "METRIC-discharge_capacity-v1"
DENSITY = "METRIC-areal_density-v1"
PDF = b"%PDF-1.4\n% test\n"


def approve_plan(researcher, qa, plan_id: str) -> None:
    plan = researcher.get(f"/api/plans/{plan_id}").json()
    if plan["state"] != "locked":
        assert researcher.post(f"/api/plans/{plan_id}/lock").status_code == 200
        plan = researcher.get(f"/api/plans/{plan_id}").json()
    if plan["approval_state"] == "draft":
        assert researcher.post(f"/api/plans/{plan_id}/submit").status_code == 200
        plan = researcher.get(f"/api/plans/{plan_id}").json()
    if plan["approval_state"] == "review":
        assert qa.post(
            f"/api/plans/{plan_id}/decision",
            {"conclusion": "approved",
             "signature_id": qa.sign("批准方案", target=plan_id,
                                     object_version=plan["row_version"])},
        ).status_code == 200


def test_sop_lifecycle_and_self_approval_guard(admin, researcher, qa, reset_runtime):
    """AC-33：SOP 草稿 → 评审 → 发布 → 退役；作者不能批准自己写的版本。

    作者用管理员：它同时有 sop.edit 与 sop.approve，所以这里验的是
    「有权限也不能自批」，而不是权限本身不够。
    """
    uploaded = admin.upload("/api/files", "sop.pdf", PDF, "application/pdf")
    assert uploaded.status_code == 201, uploaded.text
    file_id = uploaded.json()["id"]

    created = admin.post(
        "/api/sops",
        {"code": "SOP-TEST-01", "title": "测试作业指导书", "version": "v1", "file_id": file_id,
         "capability_scope": ["cap.mix"], "requires_training_ack": True},
    )
    assert created.status_code == 201, created.text
    version_id = created.json()["id"]
    assert created.json()["state"] == "draft" and created.json()["editable"] is True
    assert created.json()["file_checksum"] == uploaded.json()["checksum"]

    assert admin.post(f"/api/sops/{version_id}/submit").status_code == 200
    self_approve = admin.post(
        f"/api/sops/{version_id}/decision",
        {"conclusion": "approved",
         "signature_id": admin.sign("批准 SOP", target=version_id)},
    )
    assert self_approve.status_code == 403
    assert self_approve.json()["detail"]["code"] == "self_approval_denied"

    published = qa.post(
        f"/api/sops/{version_id}/decision",
        {"conclusion": "approved", "effective_from": datetime.utcnow().isoformat(),
         "signature_id": qa.sign("批准 SOP", target=version_id)},
    )
    assert published.status_code == 200, published.text
    assert published.json()["state"] == "published"

    # 已发布不可原位编辑
    locked = admin.patch(f"/api/sops/{version_id}", {"requires_training_ack": False})
    assert locked.status_code == 409
    assert locked.json()["detail"]["code"] == "sop_not_editable"

    # 修订产生新版本，历史版本仍可查
    revision = admin.post(
        "/api/sops",
        {"code": "SOP-TEST-01", "title": "测试作业指导书", "version": "v2", "file_id": file_id},
    )
    assert revision.status_code == 201
    retired = qa.post(f"/api/sops/{version_id}/retire", {"reason": "已被 v2 取代"})
    assert retired.status_code == 200 and retired.json()["state"] == "retired"
    assert researcher.get(f"/api/sops/{version_id}").status_code == 200, "历史引用仍可查"


def test_sop_training_acknowledgement_gates_dispatch(
    researcher, qa, operator, admin, reset_runtime
):
    """AC-33 / DEV-13.3：要求培训确认的 SOP 没有确认记录时阻止执行。"""
    uploaded = researcher.upload("/api/files", "sop-ack.pdf", PDF, "application/pdf")
    version = researcher.post(
        "/api/sops",
        {"code": "SOP-ACK-01", "title": "需确认的指导书", "version": "v1",
         "file_id": uploaded.json()["id"], "capability_scope": ["cap.mix"],
         "requires_training_ack": True},
    ).json()
    researcher.post(f"/api/sops/{version['id']}/submit")
    qa.post(
        f"/api/sops/{version['id']}/decision",
        {"conclusion": "approved", "effective_from": datetime.utcnow().isoformat(),
         "signature_id": qa.sign("批准 SOP", target=version["id"])},
    )

    recipe = researcher.post("/api/recipes", {"name": "需 SOP 确认的方法", "plate": 4}).json()
    recipe_id = recipe["id"]
    assert researcher.patch(
        f"/api/recipes/{recipe_id}",
        {
            "risk": "RA-ack v1", "sop_version_id": version["id"], "bom": [],
            "steps": [{"kind": "device", "name": "控温匀浆", "cap": "cap.mix",
                       "params": {"temp": 25, "rpm": 2000}, "dur": 5}],
        },
    ).status_code == 200
    researcher.post(f"/api/recipes/{recipe_id}/submit")
    qa.post(f"/api/recipes/{recipe_id}/transition",
            {"target_state": "approved", "signature_id": qa.sign_recipe("批准", recipe_id)})
    qa.post(f"/api/recipes/{recipe_id}/transition",
            {"target_state": "released", "signature_id": qa.sign_recipe("发布", recipe_id)})

    plan = researcher.post(
        "/api/plans",
        {"name": "SOP 确认方案", "recipe_id": recipe_id, "plan_type": "single_condition",
         "sample_count": 2, "required_metrics": [CAPACITY]},
    ).json()
    approve_plan(researcher, qa, plan["id"])
    batch_id = operator.post("/api/batches", {"plan_id": plan["id"]}).json()["id"]
    assert operator.post(f"/api/batches/{batch_id}/schedule", {}).status_code == 200

    preflight = operator.get(f"/api/batches/{batch_id}/preflight?manual_review=true").json()
    qualification = next(row for row in preflight["checks"] if row["key"] == "qualification")
    assert qualification["state"] == "blocked"
    assert "阅读确认" in qualification["detail"]

    assert operator.post(f"/api/sops/{version['id']}/acknowledge").status_code == 200
    after = operator.get(f"/api/batches/{batch_id}/preflight?manual_review=true").json()
    assert after["ok"] is True, after["blocked"]
    # 批次固化了实际采用的 SOP 版本与附件摘要
    snapshot = operator.get(f"/api/batches/{batch_id}").json()["sop_snapshot"]
    assert snapshot["code"] == "SOP-ACK-01" and snapshot["file_checksum"]


@pytest.fixture()
def reviewed_batch(researcher, qa, operator, lims, reset_runtime, executor):
    """AC-38 的骨架：方案 → 任务 → 批次 → 执行 → 检测 → 复核，返回可出报告的批次。"""
    plan_id = "EP-205-01"
    approve_plan(researcher, qa, plan_id)
    batch_id = operator.post("/api/batches", {"plan_id": plan_id}).json()["id"]
    assert operator.post(f"/api/batches/{batch_id}/schedule", {}).status_code == 200
    assert operator.post(
        f"/api/batches/{batch_id}/dispatch",
        {"manual_review": True, "signature_id": operator.sign("批准执行", target=batch_id)},
    ).status_code == 200
    for _ in range(12):
        executor()
    detail = operator.get(f"/api/batches/{batch_id}").json()
    assert detail["state"] == "done", detail["failure_reason"]

    # 为前两个运行分配建检测任务并回传，其中一个判为 invalid
    assignments = detail["samples"][:3]
    values: list[dict] = []
    for index, assignment in enumerate(assignments):
        task = researcher.post(
            "/api/analysis-tasks",
            {
                "sample_id": assignment["id"],
                "physical_sample_id": assignment["physical_sample_id"],
                "method": "电性能测试", "method_version": "EC-02 v2",
                "required_metrics": [CAPACITY],
            },
        )
        assert task.status_code == 201, task.text
        task_id = task.json()["id"]
        ingested = lims.post(
            "/api/integrations/results",
            {
                "event_id": f"rep-{batch_id}-{index}", "task_id": task_id,
                "parser_version": "ec-parser 2.1",
                "metrics": [{"metric_version_id": CAPACITY, "value": 200.0 + index,
                             "unit": "mAh/g"}],
            },
        )
        assert ingested.status_code == 200, ingested.text
        values.append(ingested.json()["results"][0])

    for index, value in enumerate(values):
        quality = "invalid" if index == 2 else "valid"
        reviewed = qa.post(
            f"/api/result-values/{value['id']}/review",
            {"conclusion": "approved", "quality": quality,
             "reason": "内短路，数据无效" if quality == "invalid" else "曲线正常",
             "signature_id": qa.sign("复核", target=value["id"], object_version=1)},
        )
        assert reviewed.status_code == 200, reviewed.text
    return {"batch_id": batch_id, "values": values}


def test_official_statistics_exclude_invalid_results_with_reasons(operator, reviewed_batch):
    """AC-34：审核通过但质量 invalid 的结果默认排除并显示理由。"""
    batch_id = reviewed_batch["batch_id"]
    official = operator.get(f"/api/results/{batch_id}").json()
    assert official["official"] is True
    assert "审核通过且质量有效" in official["scope_label"]
    block = next(row for row in official["metrics"] if row["metric_id"] == CAPACITY)
    assert block["summary"]["included"] == 2
    assert block["summary"]["excluded"] == 1
    reasons = {row["reason"] for row in block["excluded"]}
    assert reasons == {"invalid"}
    assert block["excluded"][0]["reason_label"] == "已审核但质量判定无效"

    exploratory = operator.get(f"/api/results/{batch_id}?official=false").json()
    ex_block = next(row for row in exploratory["metrics"] if row["metric_id"] == CAPACITY)
    assert ex_block["summary"]["included"] == 3
    assert "探索性范围" in exploratory["scope_label"]

    export = operator.get(f"/api/results/{batch_id}/export")
    assert "仅审核通过且质量有效" in export.text
    exploratory_export = operator.get(f"/api/results/{batch_id}/export?official=false")
    assert "不可用于正式报告" in exploratory_export.text


def test_non_matrix_plan_hides_factor_effects(researcher, qa, operator, reset_runtime):
    """DEV-14.2：非矩阵实验不显示无意义的因子主效应。"""
    approve_plan(researcher, qa, "EP-210-01")
    view = operator.get("/api/plans/EP-210-01").json()
    assert view["is_matrix"] is False
    matrix = operator.get("/api/plans/EP-201-03").json()
    assert matrix["is_matrix"] is True


def test_report_publish_requires_reviewed_results_and_blocks_self_approval(
    admin, researcher, qa, operator, lims, reviewed_batch
):
    """AC-35、AC-36：未审核结果禁止发布；作者不能批准自己的报告。

    作者用管理员：它有 report.edit 与 report.approve，验的是「有权限也不能自批」。
    """
    batch_id = reviewed_batch["batch_id"]
    created = admin.post(
        "/api/reports", {"batch_id": batch_id, "conclusion": "60 μL 注液量容量最高。"}
    )
    assert created.status_code == 201, created.text
    version_id = created.json()["id"]
    assert created.json()["state"] == "draft"
    content = created.json()["content"]
    assert content["exclusions"], "排除说明必须出现在报告内容里"
    assert content["statistics"][0]["included"] == 2

    assert admin.post(f"/api/reports/{version_id}/submit").status_code == 200
    self_approve = admin.post(
        f"/api/reports/{version_id}/approve",
        {"conclusion": "approved",
         "signature_id": admin.sign("批准报告", target=version_id)},
    )
    assert self_approve.status_code == 403
    assert self_approve.json()["detail"]["code"] == "self_approval_denied"

    approved = qa.post(
        f"/api/reports/{version_id}/approve",
        {"conclusion": "approved", "signature_id": qa.sign("批准报告", target=version_id)},
    )
    assert approved.status_code == 200, approved.text

    published = qa.post(
        f"/api/reports/{version_id}/publish",
        {"signature_id": qa.sign("发布报告", target=version_id,
                                 object_version=approved.json()["row_version"])},
    )
    assert published.status_code == 200, published.text
    snapshot = published.json()["publish_snapshot"]
    assert snapshot["result_versions"], "发布时固化全部结果版本"
    assert snapshot["algorithm_version"] and snapshot["template_version"]
    assert snapshot["pdf_checksum"] and snapshot["signature_id"]
    assert published.json()["readonly"] is True

    pdf = operator.client.get(
        f"/api/reports/{version_id}/download", headers=operator.headers
    )
    assert pdf.status_code == 200
    assert pdf.content.startswith(b"%PDF"), "发布报告可下载固定模板 PDF"
    assert pdf.headers["X-Checksum-Sha256"] == snapshot["pdf_checksum"]

    # 已发布不可编辑
    assert admin.patch(f"/api/reports/{version_id}", {"conclusion": "改一下"}).status_code == 409


def test_unreviewed_result_blocks_report_submission(
    researcher, qa, operator, lims, reviewed_batch
):
    batch_id = reviewed_batch["batch_id"]
    detail = operator.get(f"/api/batches/{batch_id}").json()
    assignment = detail["samples"][3]
    task = researcher.post(
        "/api/analysis-tasks",
        {"sample_id": assignment["id"], "physical_sample_id": assignment["physical_sample_id"],
         "method": "电性能测试", "required_metrics": [CAPACITY]},
    ).json()
    assert lims.post(
        "/api/integrations/results",
        {"event_id": f"unreviewed-{batch_id}", "task_id": task["id"],
         "metrics": [{"metric_version_id": CAPACITY, "value": 199.0, "unit": "mAh/g"}]},
    ).status_code == 200

    report = researcher.post("/api/reports", {"batch_id": batch_id, "conclusion": "待定"})
    assert report.status_code == 201
    blocked = researcher.post(f"/api/reports/{report.json()['id']}/submit")
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "unreviewed_results"
    assert any("待复核" in row["label"] for row in blocked.json()["detail"]["blocked"])


def test_revising_source_results_requires_a_new_report_version(
    researcher, qa, operator, reviewed_batch
):
    """AC-35：发布报告后修订源结果，原 PDF 与引用不变，新报告标明替代关系。"""
    batch_id = reviewed_batch["batch_id"]
    created = researcher.post("/api/reports", {"batch_id": batch_id, "conclusion": "初版结论"})
    version_id = created.json()["id"]
    researcher.post(f"/api/reports/{version_id}/submit")
    approved = qa.post(
        f"/api/reports/{version_id}/approve",
        {"conclusion": "approved", "signature_id": qa.sign("批准报告", target=version_id)},
    )
    published = qa.post(
        f"/api/reports/{version_id}/publish",
        {"signature_id": qa.sign("发布报告", target=version_id,
                                 object_version=approved.json()["row_version"])},
    )
    assert published.status_code == 200, published.text
    original_pdf = published.json()["pdf_file_id"]

    # 修订一个源结果
    value = reviewed_batch["values"][0]
    revised = researcher.post(
        f"/api/result-values/{value['id']}/revisions",
        {"value": 210.0, "unit": "mAh/g", "reason": "按标准曲线重新换算"},
    )
    assert revised.status_code == 201, revised.text

    # 原报告不变
    unchanged = operator.get(f"/api/reports/{version_id}").json()
    assert unchanged["state"] == "published"
    assert unchanged["pdf_file_id"] == original_pdf

    new_version = researcher.post(f"/api/reports/{version_id}/revisions")
    assert new_version.status_code == 201, new_version.text
    assert new_version.json()["version"] == 2
    assert new_version.json()["supersedes_id"] == version_id


def test_recipe_can_link_a_published_sop_and_only_a_published_one(researcher, qa, reset_runtime):
    """界面上新接的方法↔SOP 关联：只能指向已发布版本，草稿与不存在的都要被拒。"""
    draft = researcher.post(
        "/api/sops", {"code": "SOP-LINK-1", "title": "关联用作业指导", "version": "v1"}
    )
    assert draft.status_code == 201, draft.text
    version_id = draft.json()["id"]

    recipe = researcher.post("/api/recipes", {"name": "SOP 关联样例", "plate": 8})
    assert recipe.status_code == 201, recipe.text
    recipe_id = recipe.json()["id"]

    # 草稿 SOP 不能被引用
    rejected = researcher.patch(f"/api/recipes/{recipe_id}", {"sop_version_id": version_id})
    assert rejected.status_code == 422
    assert "已发布" in rejected.json()["detail"]["message"]

    assert researcher.patch(
        f"/api/recipes/{recipe_id}", {"sop_version_id": "SOPV-不存在"}
    ).status_code == 404

    # 发布之后才能关联，且详情里带出可读摘要供界面显示
    uploaded = researcher.upload(
        "/api/files", "sop.pdf", b"%PDF-1.4 link", "application/pdf", ref_type="sop"
    )
    assert uploaded.status_code == 201, uploaded.text
    file_id = uploaded.json()["id"]
    assert researcher.patch(f"/api/sops/{version_id}", {"file_id": file_id}).status_code == 200
    assert researcher.post(f"/api/sops/{version_id}/submit").status_code == 200
    published = qa.post(
        f"/api/sops/{version_id}/decision",
        {"conclusion": "approved",
         "signature_id": qa.sign("批准并发布 SOP", target=version_id)},
    )
    assert published.status_code == 200, published.text

    linked = researcher.patch(f"/api/recipes/{recipe_id}", {"sop_version_id": version_id})
    assert linked.status_code == 200, linked.text
    detail = researcher.get(f"/api/recipes/{recipe_id}").json()
    assert detail["sop_version_id"] == version_id
    assert detail["sop"]["code"] == "SOP-LINK-1"
    assert detail["sop"]["state"] == "published"
    sop_check = next(row for row in detail["checks"] if row["key"] == "sop")
    # 清单里显示编号与版本，不是版本 UUID——它是给人看的
    assert "SOP-LINK-1" in sop_check["detail"] and "v1" in sop_check["detail"]
    assert version_id not in sop_check["detail"]


def test_sop_draft_is_editable_and_published_one_is_not(researcher, qa, reset_runtime):
    """草稿可改，发布后只读——受控文件的内容与版本号一对一。"""
    draft = researcher.post(
        "/api/sops", {"code": "SOP-EDIT-1", "title": "可改的草稿", "version": "v1"}
    )
    version_id = draft.json()["id"]

    edited = researcher.patch(
        f"/api/sops/{version_id}",
        {"capability_scope": ["cap.mix"], "sample_types": ["极片"], "requires_training_ack": True},
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["capability_scope"] == ["cap.mix"]
    assert edited.json()["requires_training_ack"] is True

    file_id = researcher.upload(
        "/api/files", "sop.pdf", b"%PDF-1.4 edit", "application/pdf", ref_type="sop"
    ).json()["id"]
    researcher.patch(f"/api/sops/{version_id}", {"file_id": file_id})
    researcher.post(f"/api/sops/{version_id}/submit")
    qa.post(
        f"/api/sops/{version_id}/decision",
        {"conclusion": "approved", "signature_id": qa.sign("批准并发布 SOP", target=version_id)},
    )

    frozen = researcher.patch(f"/api/sops/{version_id}", {"sample_types": ["电芯"]})
    assert frozen.status_code == 409, frozen.text


def test_effective_sops_are_listed_when_no_capability_is_given(researcher, reset_runtime):
    """方法编辑器拉的是「全部生效版本」。不指定能力就是不筛，不是筛成空。"""
    everything = researcher.get("/api/sops/effective")
    assert everything.status_code == 200, everything.text
    rows = everything.json()
    assert rows, "种子里有两份已发布且已生效的 SOP，下拉框不该是空的"
    assert all(row["state"] == "published" for row in rows)
    scoped = [row for row in rows if row["capability_scope"]]
    assert scoped, "这两份都限定了适用能力——正是它们以前被漏掉"

    # 指定能力时仍然要筛
    capability = scoped[0]["capability_scope"][0]
    narrowed = researcher.get(f"/api/sops/effective?capability_id={capability}").json()
    assert all(
        not row["capability_scope"] or capability in row["capability_scope"] for row in narrowed
    )
    unrelated = researcher.get("/api/sops/effective?capability_id=cap.不存在").json()
    assert all(not row["capability_scope"] for row in unrelated)
