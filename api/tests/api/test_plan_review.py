"""方案模板、多级审批（指定审批人、逐级、职责分离）、驳回状态、版本对比与恢复、批注；
数字 SOP（结构化步骤 → 方法草稿）、SOP 版本对比与恢复。"""
import uuid

CAPACITY = "METRIC-discharge_capacity-v1"
PDF = b"%PDF-1.4 sop"


def _me(session) -> dict:
    return session.get("/api/auth/me").json()


def _plan(researcher, **extra) -> dict:
    created = researcher.post("/api/plans", {
        "name": f"评审用例 {uuid.uuid4().hex[:4]}", "recipe_id": "R-205", "plan_type": "single_condition",
        "sample_count": 4, "required_metrics": [CAPACITY], **extra,
    })
    assert created.status_code == 201, created.text
    return created.json()


def test_multi_level_approval_with_assigned_approvers(researcher, qa, admin, reset_runtime):
    plan = _plan(researcher)
    qa_id, admin_id = _me(qa)["id"], _me(admin)["id"]
    submitted = researcher.post(f"/api/plans/{plan['id']}/submit", {"approvers": [
        {"label": "技术审核", "assignee_id": admin_id}, {"label": "QA 批准", "assignee_id": qa_id},
    ]})
    assert submitted.status_code == 200, submitted.text
    body = submitted.json()
    assert body["approval_state"] == "review" and [row["label"] for row in body["approvals"]] == ["技术审核", "QA 批准"]
    assert researcher.patch(f"/api/plans/{plan['id']}", {"goal": "改", "row_version": body["row_version"]}).status_code == 409

    out_of_turn = qa.post(f"/api/plans/{plan['id']}/decision", {
        "conclusion": "approved", "signature_id": qa.sign("批准方案", target=plan["id"], object_version=body["row_version"]),
    })
    assert out_of_turn.status_code == 403 and out_of_turn.json()["detail"]["code"] == "approver_not_assigned"

    first = admin.post(f"/api/plans/{plan['id']}/decision", {
        "conclusion": "approved", "signature_id": admin.sign("批准方案", target=plan["id"], object_version=body["row_version"]),
    })
    assert first.status_code == 200, first.text
    assert first.json()["approval_state"] == "review" and first.json()["approvals"][0]["conclusion"] == "approved"

    final = qa.post(f"/api/plans/{plan['id']}/decision", {
        "conclusion": "approved", "signature_id": qa.sign("批准方案", target=plan["id"], object_version=first.json()["row_version"]),
    })
    assert final.status_code == 200, final.text
    assert final.json()["approval_state"] == "approved"
    assert all(row["decided_by_name"] for row in final.json()["approvals"])


def test_rejection_is_recorded_and_versions_can_be_compared_and_restored(researcher, qa, reset_runtime):
    plan = _plan(researcher, goal="初版目的")
    assert researcher.post(f"/api/plans/{plan['id']}/submit").status_code == 200
    rejected = qa.post(f"/api/plans/{plan['id']}/decision", {"conclusion": "rejected", "reason": "样本数不够"})
    assert rejected.status_code == 200 and rejected.json()["approval_state"] == "rejected"
    assert rejected.json()["reject_reason"] == "样本数不够"

    edited = researcher.patch(f"/api/plans/{plan['id']}", {"goal": "改过的目的", "sample_count": 6,
                                                           "row_version": rejected.json()["row_version"]})
    assert edited.status_code == 200, edited.text
    diff = researcher.get(f"/api/plans/{plan['id']}/diff?from_version=1").json()
    changed = {row["field"]: row for row in diff["changes"]}
    assert changed["goal"]["before"] == "初版目的" and changed["goal"]["after"] == "改过的目的"
    assert "sample_count" in changed

    restored = researcher.post(f"/api/plans/{plan['id']}/restore", {"from_version": 1, "row_version": edited.json()["row_version"]})
    assert restored.status_code == 200, restored.text
    assert restored.json()["goal"] == "初版目的" and restored.json()["sample_count"] == 4
    assert researcher.post(f"/api/plans/{plan['id']}/submit").status_code == 200, "驳回后改完可以再提交"


def test_plan_templates_prefill_new_plans(researcher, reset_runtime):
    source = _plan(researcher, goal="模板来源的目的", repeats=2)
    template = researcher.post("/api/plans/templates", {"name": "扣电单条件", "from_plan_id": source["id"]})
    assert template.status_code == 201, template.text
    assert template.json()["body"]["goal"] == "模板来源的目的"
    created = researcher.post("/api/plans", {"name": "套模板", "template_id": template.json()["id"]})
    assert created.status_code == 201, created.text
    plan = created.json()
    assert plan["recipe_id"] == "R-205" and plan["plan_type"] == "single_condition"
    assert plan["goal"] == "模板来源的目的" and plan["sample_count"] == 4
    listed = researcher.get("/api/plans/templates").json()
    assert any(row["id"] == template.json()["id"] for row in listed)


def test_comments_on_a_plan(researcher, qa, reset_runtime):
    plan = _plan(researcher)
    posted = qa.post("/api/comments", {"target_type": "plan", "target_id": plan["id"], "anchor": "sample_count",
                                       "body": "每组至少 3 个平行样"})
    assert posted.status_code == 201, posted.text
    assert posted.json()["target_version"] == "v1"
    rows = researcher.get(f"/api/comments?target_type=plan&target_id={plan['id']}").json()
    assert rows[0]["body"] == "每组至少 3 个平行样" and not rows[0]["resolved"]
    resolved = researcher.post(f"/api/comments/{rows[0]['id']}/resolve")
    assert resolved.status_code == 200 and resolved.json()["resolved"]


def test_digital_sop_generates_a_method_draft_and_versions_restore(researcher, qa, reset_runtime):
    uploaded = researcher.upload("/api/files", "sop.pdf", PDF, "application/pdf", ref_type="sop")
    code = f"SOP-DG-{uuid.uuid4().hex[:4]}"
    version = researcher.post("/api/sops", {"code": code, "title": "扣电组装", "file_id": uploaded.json()["id"]})
    assert version.status_code == 201, version.text
    version = version.json()
    steps = [
        {"title": "极片干燥", "kind": "device", "capability": "cap.vacuum_dry", "params": {"temp": 120, "vacuum": 1},
         "duration_min": 60},
        {"title": "目检", "kind": "manual", "instructions": "检查极片边缘", "checks": ["无毛刺", "无掉粉"]},
        {"title": "静置", "kind": "wait", "duration_min": 10},
        {"title": "QA 复核", "kind": "review"},
    ]
    bad = researcher.put(f"/api/sops/{version['id']}/steps", {"steps": [{"title": "x", "kind": "device", "capability": "cap.nope"}],
                                                             "row_version": version["row_version"]})
    assert bad.status_code == 422 or bad.status_code == 400 or bad.json()["detail"]["code"] == "sop_steps_invalid"
    saved = researcher.put(f"/api/sops/{version['id']}/steps", {"steps": steps, "row_version": version["row_version"]})
    assert saved.status_code == 200, saved.text

    assert researcher.post(f"/api/sops/{version['id']}/submit").status_code == 200
    published = qa.post(f"/api/sops/{version['id']}/decision", {
        "conclusion": "approved", "signature_id": qa.sign("批准 SOP", target=version["id"], object_version=saved.json()["row_version"] + 1),
    })
    assert published.status_code == 200, published.text

    generated = researcher.post(f"/api/sops/{version['id']}/generate-recipe", {"plate": 8})
    assert generated.status_code == 201, generated.text
    assert generated.json()["sop_linked"] and generated.json()["steps"] == 4
    recipe = researcher.get(f"/api/recipes/{generated.json()['recipe_id']}").json()
    kinds = [row.get("kind") for row in recipe["steps"]]
    assert kinds == ["device", "manual", "wait", "review"]
    assert [field["label"] for field in recipe["steps"][1]["form"]][:2] == ["无毛刺", "无掉粉"]
    assert recipe["sop_version_id"] == version["id"]
    assert all(row["ok"] for row in recipe["validation"]), [row["issues"] for row in recipe["validation"]]

    restored = researcher.post(f"/api/sops/{version['id']}/restore", {"version": "v9"})
    assert restored.status_code == 201, restored.text
    assert restored.json()["restored_from"] == version["id"] and restored.json()["steps"] == saved.json()["steps"]
    diff = researcher.get(f"/api/sops/{restored.json()['id']}/diff?against={version['id']}").json()
    assert diff["changes"] == [], "恢复出来的版本与来源内容一致"


def test_approver_candidates_have_the_approval_permission(researcher, qa):
    names = {row["id"] for row in researcher.get("/api/plans/approvers").json()}
    assert _me(qa)["id"] in names and _me(researcher)["id"] not in names
