"""主数据：配方生命周期、能力极限、实验计划锁定、批号放行。"""


def test_recipe_lifecycle_requires_signature_and_server_validation(client, researcher, qa, reset_runtime):
    draft = researcher.post("/api/recipes", {"name": "接口测试配方", "plate": 8, "copy_from": "R-201"})
    assert draft.status_code == 201, draft.text
    recipe_id = draft.json()["id"]

    assert researcher.post(f"/api/recipes/{recipe_id}/submit").status_code == 200

    without_signature = qa.post(f"/api/recipes/{recipe_id}/transition", {"target_state": "approved", "signature_id": ""})
    assert without_signature.status_code == 400

    approved = qa.post(
        f"/api/recipes/{recipe_id}/transition",
        {"target_state": "approved", "signature_id": qa.sign_recipe("评审通过", recipe_id)},
    )
    assert approved.status_code == 200 and approved.json()["state"] == "approved"

    # 跨级流转被拒：已批准不能直接退役
    assert qa.post(
        f"/api/recipes/{recipe_id}/transition",
        {"target_state": "retired", "signature_id": qa.sign_recipe("退役", recipe_id)},
    ).status_code == 409

    released = qa.post(
        f"/api/recipes/{recipe_id}/transition",
        {"target_state": "released", "signature_id": qa.sign_recipe("批准发布", recipe_id)},
    )
    assert released.status_code == 200 and released.json()["state"] == "released"

    # 已发布不可编辑，只能派生修订草稿
    assert researcher.patch(f"/api/recipes/{recipe_id}", {"risk": "RA-x"}).status_code == 409
    revision = researcher.post(f"/api/recipes/{recipe_id}/revision")
    assert revision.status_code == 200 and revision.json()["state"] == "draft"
    assert revision.json()["parent"] == recipe_id


def test_signature_is_one_shot(client, qa, reset_runtime):
    signature = qa.sign_recipe("评审通过", "R-203")
    first = qa.post("/api/recipes/R-203/transition", {"target_state": "approved", "signature_id": signature})
    assert first.status_code == 200

    replayed = qa.post("/api/recipes/R-203/transition", {"target_state": "released", "signature_id": signature})
    assert replayed.status_code == 400
    assert "签名已使用" in replayed.json()["detail"]["message"]


def test_limit_change_marks_released_recipe_as_needing_revision(client, admin, reset_runtime):
    response = admin.patch(
        "/api/stations/ST-03/limits",
        {"limits": {"cap.coat": {"thickness": [20, 100]}}, "signature_id": admin.sign("工程变更批准", target="ST-03")},
    )
    assert response.status_code == 200, response.text
    assert "R-201" in response.json()["broken_recipes"], "180 μm 超出新上限，引用配方必须进入需修订"

    recipe = admin.get("/api/recipes/R-201").json()
    assert recipe["needs_revision"] and not recipe["valid"]

    restored = admin.patch(
        "/api/stations/ST-03/limits",
        {"limits": {"cap.coat": {"thickness": [20, 400]}}, "signature_id": admin.sign("工程变更批准", target="ST-03")},
    )
    assert restored.status_code == 200 and "R-201" not in restored.json()["broken_recipes"]
    assert admin.get("/api/recipes/R-201").json()["valid"]


def test_plan_lock_rejects_matrix_larger_than_plate(client, researcher, reset_runtime):
    created = researcher.post(
        "/api/plans",
        {
            "name": "超容量矩阵", "recipe_id": "R-205", "repeats": 4,
            "factors": [{"name": "FEC 含量", "unit": "%", "levels": [0, 2, 5, 10]}],
            "required_metrics": ["METRIC-discharge_capacity-v1"],
        },
    )
    plan_id = created.json()["id"]

    rejected = researcher.post(f"/api/plans/{plan_id}/lock")
    assert rejected.status_code == 409
    assert any(c["key"] == "capacity" for c in rejected.json()["detail"]["checks"])

    researcher.patch(f"/api/plans/{plan_id}", {"repeats": 2})
    assert researcher.post(f"/api/plans/{plan_id}/lock").status_code == 200


def test_locked_plan_is_immutable_and_lot_release_needs_signature(client, researcher, operator, qa, reset_runtime):
    assert researcher.patch("/api/plans/EP-201-03", {"repeats": 1}).status_code == 409
    assert qa.post("/api/lots", {"id": "LOT-DENIED", "material": "NMP 溶剂", "qty": 1, "unit": "L",
                                 "expiry": "2027-12-01"}).status_code == 403, "QA 不负责入库登记"

    received = operator.post(
        "/api/lots",
        {"id": "LOT-TEST-0001", "material": "NMP 溶剂", "qty": 2.0, "unit": "L", "expiry": "2027-12-01"},
    )
    assert received.status_code == 201 and received.json()["release"] == "待复验"

    released = qa.post(
        "/api/lots/LOT-TEST-0001/release", {"signature_id": qa.sign("复验合格", target="LOT-TEST-0001")}
    )
    assert released.status_code == 200 and released.json()["release"] == "已放行"
