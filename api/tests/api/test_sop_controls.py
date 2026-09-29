"""DEV-13 / AC-33：SOP 版本取代、新批次按生效版本执行、适用范围校验、执行侧快照与受控元数据。"""
from datetime import datetime, timedelta
from uuid import uuid4

from tests.api.test_reports import CAPACITY, PDF, approve_plan


def publish_sop(author, qa, code: str, version: str, effective_from: datetime | None = None, **extra) -> dict:
    uploaded = author.upload("/api/files", f"{code}-{version}.pdf", PDF, "application/pdf")
    assert uploaded.status_code == 201, uploaded.text
    created = author.post(
        "/api/sops",
        {"code": code, "title": f"{code} 指导书", "version": version, "file_id": uploaded.json()["id"], **extra},
    )
    assert created.status_code == 201, created.text
    version_id = created.json()["id"]
    submitted = author.post(f"/api/sops/{version_id}/submit")
    assert submitted.status_code == 200, submitted.text
    published = qa.post(
        f"/api/sops/{version_id}/decision",
        {"conclusion": "approved", "effective_from": (effective_from or datetime.utcnow()).isoformat(),
         "signature_id": qa.sign("批准 SOP", target=version_id)},
    )
    assert published.status_code == 200, published.text
    return published.json()


def released_recipe(researcher, qa, sop_version_id: str, cap: str = "cap.mix", **step_extra) -> str:
    recipe_id = researcher.post("/api/recipes", {"name": f"SOP 用例流程 {uuid4().hex[:4]}", "plate": 4}).json()["id"]
    params = {"temp": 25, "rpm": 2000} if cap == "cap.mix" else {}
    patched = researcher.patch(
        f"/api/recipes/{recipe_id}",
        {"risk": "RA-sop v1", "sop_version_id": sop_version_id, "bom": [],
         "steps": [{"kind": "device", "name": "设备步骤", "cap": cap, "params": params, "dur": 5, **step_extra}]},
    )
    assert patched.status_code == 200, patched.text
    submitted = researcher.post(f"/api/recipes/{recipe_id}/submit")
    assert submitted.status_code == 200, submitted.text
    for target, meaning in (("approved", "批准"), ("released", "发布")):
        moved = qa.post(f"/api/recipes/{recipe_id}/transition",
                        {"target_state": target, "signature_id": qa.sign_recipe(meaning, recipe_id)})
        assert moved.status_code == 200, moved.text
    return recipe_id


def approved_plan(researcher, qa, recipe_id: str, **extra) -> str:
    body = {"name": "SOP 用例方案", "recipe_id": recipe_id, "plan_type": "single_condition",
            "sample_count": 2, "required_metrics": [CAPACITY], **extra}
    plan = researcher.post("/api/plans", body)
    assert plan.status_code == 201, plan.text
    approve_plan(researcher, qa, plan.json()["id"])
    return plan.json()["id"]


def test_new_version_supersedes_old_and_new_batches_follow_it(researcher, qa, operator, reset_runtime):
    code = f"SOP-SUP-{uuid4().hex[:4]}"
    v1 = publish_sop(researcher, qa, code, "v1", capability_scope=["cap.mix"])
    recipe_id = released_recipe(researcher, qa, v1["id"])
    plan_id = approved_plan(researcher, qa, recipe_id)
    old_batch = operator.post("/api/batches", {"plan_id": plan_id})
    assert old_batch.status_code == 201, old_batch.text
    assert operator.get(f"/api/batches/{old_batch.json()['id']}").json()["sop_snapshot"]["version"] == "v1"

    v2 = publish_sop(researcher, qa, code, "v2", capability_scope=["cap.mix"])
    # 发布结果列出影响面：还关联 v1 的流程、按 v1 在途的批次
    assert v2["superseded_versions"] == ["v1"]
    assert recipe_id in [row["id"] for row in v2["impacted_recipes"]]
    assert old_batch.json()["id"] in [row["id"] for row in v2["impacted_batches"]]

    old = researcher.get(f"/api/sops/{v1['id']}").json()
    assert old["status"] == "superseded" and old["superseded_by_version"] == "v2" and old["effective_to"]
    effective = [row["id"] for row in researcher.get("/api/sops/effective").json()]
    assert v2["id"] in effective and v1["id"] not in effective

    # 已被取代的版本不能再被新流程关联，也不能再做阅读确认
    fresh = researcher.post("/api/recipes", {"name": "关联旧版", "plate": 4}).json()
    linked = researcher.patch(f"/api/recipes/{fresh['id']}", {"sop_version_id": v1["id"]})
    assert linked.status_code == 422 and linked.json()["detail"]["code"] == "sop_not_effective"
    assert operator.post(f"/api/sops/{v1['id']}/acknowledge").status_code == 409

    # 流程仍关联 v1：新批次按同编号当前生效的 v2 执行，并记下流程关联的是 v1
    plan_2 = approved_plan(researcher, qa, recipe_id)
    new_batch = operator.post("/api/batches", {"plan_id": plan_2})
    assert new_batch.status_code == 201, new_batch.text
    snapshot = operator.get(f"/api/batches/{new_batch.json()['id']}").json()["sop_snapshot"]
    assert snapshot["version"] == "v2" and snapshot["linked_version"] == "v1"

    # 流程详情提示新批次按 v2 执行
    checks = {row["key"]: row for row in researcher.get(f"/api/recipes/{recipe_id}").json()["checks"]}
    assert checks["sop"]["ok"] is True and "新批次按 v2 执行" in checks["sop"]["detail"]

    # 按 v1 固化、尚未开跑的批次：开跑检查提醒，不阻塞（在途不自动改版，由负责人决定）
    old_id = old_batch.json()["id"]
    assert operator.post(f"/api/batches/{old_id}/schedule", {}).status_code == 200
    preflight = operator.get(f"/api/batches/{old_id}/preflight?manual_review=true").json()
    sop_check = next(row for row in preflight["checks"] if row["key"] == "sop")
    assert sop_check["state"] == "warn", sop_check
    assert "已被取代" in sop_check["detail"] and "v2" in sop_check["detail"]


def test_future_version_takes_over_only_when_it_becomes_effective(researcher, qa, reset_runtime):
    code = f"SOP-FUT-{uuid4().hex[:4]}"
    v1 = publish_sop(researcher, qa, code, "v1")
    later = datetime.utcnow() + timedelta(days=2)
    v2 = publish_sop(researcher, qa, code, "v2", effective_from=later)
    assert v2["status"] == "pending"
    first = researcher.get(f"/api/sops/{v1['id']}").json()
    assert first["status"] == "effective" and first["superseded_by"] == v2["id"], "v1 生效到 v2 开始，不留空窗"

    # 在 v2 生效前又紧急发布 v3：v3 取代 v1，并只生效到 v2 开始
    v3 = publish_sop(researcher, qa, code, "v3")
    assert v3["status"] == "effective" and v3["superseded_by"] == v2["id"] and v3["effective_to"]
    assert researcher.get(f"/api/sops/{v1['id']}").json()["status"] == "superseded"
    assert researcher.get(f"/api/sops/{v2['id']}").json()["status"] == "pending"


def test_retired_sop_without_successor_blocks_new_batches(researcher, qa, operator, reset_runtime):
    code = f"SOP-RET-{uuid4().hex[:4]}"
    v1 = publish_sop(researcher, qa, code, "v1", capability_scope=["cap.mix"])
    recipe_id = released_recipe(researcher, qa, v1["id"])
    plan_id = approved_plan(researcher, qa, recipe_id)
    running = operator.post("/api/batches", {"plan_id": plan_id}).json()["id"]

    retired = qa.post(f"/api/sops/{v1['id']}/retire", {"reason": "工艺停用"})
    assert retired.status_code == 200, retired.text
    assert retired.json()["replacement_version"] == ""
    assert running in [row["id"] for row in retired.json()["impacted_batches"]]

    plan_2 = approved_plan(researcher, qa, recipe_id)
    blocked = operator.post("/api/batches", {"plan_id": plan_2})
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["detail"]["code"] == "sop_not_effective"

    checks = {row["key"]: row for row in researcher.get(f"/api/recipes/{recipe_id}").json()["checks"]}
    assert checks["sop"]["ok"] is False and "没有生效版本" in checks["sop"]["detail"]


def test_recipe_capabilities_must_fit_the_sop_scope(researcher, qa, reset_runtime):
    sop = publish_sop(researcher, qa, f"SOP-SCP-{uuid4().hex[:4]}", "v1", capability_scope=["cap.test"])
    recipe_id = researcher.post("/api/recipes", {"name": "范围不符", "plate": 4}).json()["id"]
    assert researcher.patch(
        f"/api/recipes/{recipe_id}",
        {"risk": "RA-scope v1", "sop_version_id": sop["id"], "bom": [],
         "steps": [{"kind": "device", "name": "匀浆", "cap": "cap.mix", "params": {"temp": 25, "rpm": 2000},
                    "dur": 5}]},
    ).status_code == 200
    checks = {row["key"]: row for row in researcher.get(f"/api/recipes/{recipe_id}").json()["checks"]}
    assert checks["sop"]["ok"] is False and "cap.mix" in checks["sop"]["detail"]
    submitted = researcher.post(f"/api/recipes/{recipe_id}/submit")
    assert submitted.status_code == 409, submitted.text
    assert submitted.json()["detail"]["code"] == "sop_unusable"


def test_sop_steps_must_stay_inside_its_own_scope(researcher, reset_runtime):
    uploaded = researcher.upload("/api/files", "scope.pdf", PDF, "application/pdf").json()
    draft = researcher.post("/api/sops", {"code": f"SOP-STP-{uuid4().hex[:4]}", "title": "步骤越界",
                                          "file_id": uploaded["id"], "capability_scope": ["cap.test"]}).json()
    saved = researcher.put(f"/api/sops/{draft['id']}/steps", {
        "row_version": draft["row_version"],
        "steps": [{"title": "匀浆", "kind": "device", "capability": "cap.mix", "params": {"rpm": 2000},
                   "duration_min": 10, "instructions": "", "checks": []}],
    })
    assert saved.status_code == 200, saved.text
    submitted = researcher.post(f"/api/sops/{draft['id']}/submit")
    assert submitted.status_code == 409 and submitted.json()["detail"]["code"] == "sop_scope_mismatch"


def test_sample_type_outside_sop_scope_blocks_start(researcher, qa, operator, reset_runtime):
    sample_id = f"PS-SOPT-{uuid4().hex[:5]}"
    assert operator.post("/api/samples", {"id": sample_id, "source": "SOP 用例", "sample_type": "浆料"}).status_code == 201
    sop = publish_sop(researcher, qa, f"SOP-TYP-{uuid4().hex[:4]}", "v1", capability_scope=["cap.mix"],
                      sample_types=["极片"])
    recipe_id = released_recipe(researcher, qa, sop["id"])
    plan_id = approved_plan(researcher, qa, recipe_id, plan_type="commissioned_test", sample_ids=[sample_id],
                            sample_count=1)
    batch_id = operator.post("/api/batches", {"plan_id": plan_id}).json()["id"]
    assert operator.post(f"/api/batches/{batch_id}/schedule", {}).status_code == 200
    preflight = operator.get(f"/api/batches/{batch_id}/preflight?manual_review=true").json()
    sop_check = next(row for row in preflight["checks"] if row["key"] == "sop")
    assert sop_check["state"] == "blocked" and "浆料" in sop_check["detail"], sop_check


def test_batch_snapshot_carries_steps_and_attachment_for_the_executor(researcher, qa, operator, reset_runtime):
    steps = [{"title": "匀浆", "kind": "device", "capability": "cap.mix", "params": {"rpm": 2000},
              "duration_min": 10, "instructions": "出现结块立即停机", "checks": []}]
    uploaded = researcher.upload("/api/files", "exec.pdf", PDF, "application/pdf").json()
    draft = researcher.post("/api/sops", {"code": f"SOP-EXE-{uuid4().hex[:4]}", "title": "执行侧可见",
                                          "file_id": uploaded["id"], "capability_scope": ["cap.mix"]}).json()
    assert researcher.put(f"/api/sops/{draft['id']}/steps",
                          {"steps": steps, "row_version": draft["row_version"]}).status_code == 200
    assert researcher.post(f"/api/sops/{draft['id']}/submit").status_code == 200
    assert qa.post(f"/api/sops/{draft['id']}/decision",
                   {"conclusion": "approved", "signature_id": qa.sign("批准 SOP", target=draft["id"])}).status_code == 200
    recipe_id = released_recipe(researcher, qa, draft["id"], sop_step=1)
    batch_id = operator.post("/api/batches", {"plan_id": approved_plan(researcher, qa, recipe_id)}).json()["id"]
    detail = operator.get(f"/api/batches/{batch_id}").json()
    # 节点对应 SOP 第 1 步：批次页把说明带给执行人
    guide = detail["steps"][0]["sop_guide"]
    assert guide["index"] == 1 and guide["instructions"] == "出现结块立即停机"
    snapshot = detail["sop_snapshot"]
    assert snapshot["steps"][0]["instructions"] == "出现结块立即停机"
    assert snapshot["filename"] == "exec.pdf"
    # 操作员能下载批次固化的 SOP 附件
    assert operator.get(f"/api/files/{snapshot['file_id']}/download").status_code == 200


def test_document_meta_owner_category_and_review_due(researcher, qa, operator, admin, reset_runtime):
    meta = researcher.get("/api/sops/meta").json()
    owner_ids = {row["id"] for row in meta["owners"]}
    qa_id = qa.get("/api/auth/me").json()["id"]
    operator_id = operator.get("/api/auth/me").json()["id"]
    assert qa_id in owner_ids and operator_id not in owner_ids, "负责人只能是能编写或批准 SOP 的人"

    uploaded = researcher.upload("/api/files", "meta.pdf", PDF, "application/pdf").json()
    rejected = researcher.post("/api/sops", {"code": f"SOP-MET-{uuid4().hex[:4]}", "title": "元数据",
                                             "file_id": uploaded["id"], "owner_id": operator_id})
    assert rejected.status_code == 422 and rejected.json()["detail"]["code"] == "sop_owner_invalid"

    code = f"SOP-MET-{uuid4().hex[:4]}"
    # 复审到期按服务端的 UTC 日期判：用本地日期在北京时间 0–8 点会比 UTC 早一天，「昨天」变成「今天」
    from app.core.clock import now

    due = (now().date() - timedelta(days=1)).isoformat()
    created = researcher.post("/api/sops", {"code": code, "title": "元数据", "file_id": uploaded["id"],
                                            "category": "浆料制备", "owner_id": qa_id, "review_due": due})
    assert created.status_code == 201, created.text
    row = created.json()
    assert row["category"] == "浆料制备" and row["owner_id"] == qa_id and row["review_due"] == due

    window = researcher.patch(f"/api/sops/{row['id']}", {
        "effective_from": datetime.utcnow().isoformat(),
        "effective_to": (datetime.utcnow() - timedelta(days=1)).isoformat(), "row_version": row["row_version"],
    })
    assert window.status_code == 422 and window.json()["detail"]["code"] == "sop_window_invalid"

    assert researcher.post(f"/api/sops/{row['id']}/submit").status_code == 200
    assert qa.post(f"/api/sops/{row['id']}/decision",
                   {"conclusion": "approved", "signature_id": qa.sign("批准 SOP", target=row["id"])}).status_code == 200
    published = researcher.get(f"/api/sops/{row['id']}").json()
    assert published["owner_name"] and published["review_overdue"] is True

    # 分类与负责人不是版本内容：已发布也能改
    changed = researcher.patch(f"/api/sops/documents/{row['sop_id']}", {"category": "电芯制备"})
    assert changed.status_code == 200, changed.text
    assert researcher.get(f"/api/sops/{row['id']}").json()["category"] == "电芯制备"
    assert "电芯制备" in researcher.get("/api/sops/meta").json()["categories"]
    filtered = researcher.get("/api/sops?category=电芯制备").json()["items"]
    assert row["id"] in [item["id"] for item in filtered]


def test_seeded_sops_are_complete_enough_to_demo(researcher):
    rows = researcher.get("/api/sops?page_size=100").json()["items"]
    ec = next(row for row in rows if row["code"] == "SOP-EC-02")
    assert ec["file_id"] and ec["steps"] and ec["requires_training_ack"] and ec["ack_count"] >= 1
    assert ec["category"] and ec["owner_name"] and ec["status"] == "effective"
    slr = sorted((row for row in rows if row["code"] == "SOP-SLR-01"), key=lambda row: row["version"])
    assert [row["version"] for row in slr] == ["v2", "v3"]
    assert slr[0]["status"] == "superseded" and slr[0]["superseded_by_version"] == "v3"
    assert slr[1]["status"] == "effective"
    # 附件真实可下载
    assert researcher.get(f"/api/files/{ec['file_id']}/download").status_code == 200
