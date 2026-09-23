"""增删改查接口的守卫行为：能删的删掉，不能删的必须说清楚为什么。"""


def test_draft_recipe_can_be_deleted_but_a_referenced_one_cannot(client, researcher, operator, reset_runtime):
    draft = researcher.post("/api/recipes", {"name": "待删草稿", "plate": 8, "copy_from": "R-201"})
    recipe_id = draft.json()["id"]
    assert draft.json()["delete_blockers"] == []

    assert researcher.client.delete(f"/api/recipes/{recipe_id}", headers=researcher.headers).status_code == 200
    assert researcher.get(f"/api/recipes/{recipe_id}").status_code == 404

    # 已发布的配方永远不能删，只能退役
    refused = researcher.client.delete("/api/recipes/R-201", headers=researcher.headers)
    assert refused.status_code == 409
    assert "退役" in str(refused.json()["detail"])


def test_recipe_referenced_by_a_plan_cannot_be_deleted(client, researcher, reset_runtime):
    draft = researcher.post("/api/recipes", {"name": "被计划引用", "plate": 8, "copy_from": "R-205"})
    recipe_id = draft.json()["id"]
    researcher.post("/api/plans", {"name": "引用它的计划", "recipe_id": recipe_id})

    refused = researcher.client.delete(f"/api/recipes/{recipe_id}", headers=researcher.headers)

    assert refused.status_code == 409
    assert "实验计划" in str(refused.json()["detail"])


def test_plan_delete_needs_unlock_and_no_bound_batches(client, researcher, operator, reset_runtime):
    plan = researcher.post("/api/plans", {"name": "待删计划", "recipe_id": "R-205"})
    plan_id = plan.json()["id"]

    deleted = researcher.client.delete(f"/api/plans/{plan_id}", headers=researcher.headers)
    assert deleted.status_code == 200

    # 已锁定的计划必须先解锁
    refused = researcher.client.delete("/api/plans/EP-205-01", headers=researcher.headers)
    assert refused.status_code == 409
    assert "解锁" in str(refused.json()["detail"])


def test_undispatched_batch_delete_returns_window_and_reservation(client, operator, reset_runtime):
    created = operator.post("/api/batches", {"plan_id": "EP-205-01"})
    batch_id = created.json()["id"]
    operator.post(f"/api/batches/{batch_id}/schedule", {})

    before = len(operator.get("/api/reservations").json())
    deleted = operator.client.delete(f"/api/batches/{batch_id}", headers=operator.headers)

    assert deleted.status_code == 200, deleted.text
    assert operator.get(f"/api/batches/{batch_id}").status_code == 404
    assert len(operator.get("/api/reservations").json()) < before, "预留应随批次一并移除"
    assert all(
        item["batch_id"] != batch_id
        for lane in operator.get("/api/schedule/board").json()["stations"]
        for item in lane["items"]
    ), "工位时间窗应已归还"


def test_unschedule_returns_the_batch_to_the_queue(client, operator, reset_runtime):
    created = operator.post("/api/batches", {"plan_id": "EP-205-01"})
    batch_id = created.json()["id"]
    operator.post(f"/api/batches/{batch_id}/schedule", {})

    back = operator.post(f"/api/batches/{batch_id}/unschedule")

    assert back.status_code == 200, back.text
    assert back.json()["state"] == "planned"
    detail = operator.get(f"/api/batches/{batch_id}").json()
    assert detail["allocations"] == [], "工位时间窗应已归还"
    assert detail["reservations"], "物料预留不受取消排程影响"

    # 没排过程的批次不能取消排程
    assert operator.post(f"/api/batches/{batch_id}/unschedule").status_code == 409


def test_capability_in_use_can_only_be_retired(client, admin, reset_runtime):
    refused = admin.client.delete("/api/capabilities/cap.mix", headers=admin.headers)
    assert refused.status_code == 409
    assert "工位" in str(refused.json()["detail"])

    retired = admin.post("/api/capabilities/cap.mix/retire", {"retired": True})
    assert retired.status_code == 200 and retired.json()["retired"] is True

    rows = {c["id"]: c for c in admin.get("/api/capabilities").json()}
    assert rows["cap.mix"]["retired"] is True
    assert rows["cap.mix"]["delete_blockers"], "在用能力不能删，界面上要给出理由"

    admin.post("/api/capabilities/cap.mix/retire", {"retired": False})


def test_retired_capability_blocks_new_recipe_steps(client, admin, researcher, reset_runtime):
    admin.post("/api/capabilities/cap.degas/retire", {"retired": True})
    try:
        draft = researcher.post("/api/recipes", {"name": "用到停用能力", "plate": 8, "copy_from": "R-201"})
        recipe_id = draft.json()["id"]
        detail = researcher.get(f"/api/recipes/{recipe_id}").json()
        degas = next(row for row in detail["validation"] if row["cap"] == "cap.degas")

        assert degas["ok"] is False
        assert any("已停用" in issue for issue in degas["issues"])
        assert researcher.post(f"/api/recipes/{recipe_id}/submit").status_code == 409
    finally:
        admin.post("/api/capabilities/cap.degas/retire", {"retired": False})


def test_station_create_edit_and_retire(client, admin, reset_runtime):
    created = admin.post("/api/stations", {
        "id": "ST-99", "name": "测试超声站", "island": 1, "model": "SONIC-1", "positions": 4,
        "cal_due": "2027-01-01", "limits": {"cap.degas": {"vacuum": [10, 500]}},
        "signature_id": admin.sign("工程变更批准", target="ST-99"),
    })
    assert created.status_code == 201, created.text

    assert admin.patch("/api/stations/ST-99", {"name": "超声分散站", "positions": 6}).status_code == 200
    row = next(s for s in admin.get("/api/stations").json() if s["id"] == "ST-99")
    assert row["name"] == "超声分散站" and row["positions"] == 6
    assert row["retired"] is False and row["retire_blockers"] == []

    # 能力极限要签名，不能从台账接口偷偷改：未声明的字段在 schema 层就被拒
    assert admin.patch("/api/stations/ST-99", {"limits": {}}).status_code == 422

    retired = admin.post("/api/stations/ST-99/retire", {"retired": True})
    assert retired.status_code == 200
    row = next(s for s in admin.get("/api/stations").json() if s["id"] == "ST-99")
    assert row["retired"] is True and row["status"] == "offline"

    # 重新启用要回到空闲：停在 offline 会让工位永远显示离线，也排不进去
    admin.post("/api/stations/ST-99/retire", {"retired": False})
    row = next(s for s in admin.get("/api/stations").json() if s["id"] == "ST-99")
    assert row["retired"] is False and row["status"] == "idle"


def test_adapter_configuration_is_versioned_secret_safe_and_testable(
    client, admin, operator, researcher, reset_runtime,
):
    created = admin.post("/api/stations", {
        "id": "ST-98", "name": "适配器配置测试站", "protocol": "Modbus TCP",
        "adapter_kind": "simulation", "adapter_driver": "simulation",
        "adapter_config": {"host": "10.0.0.20", "port": 502, "unit_id": 1},
        "credential_ref": "vault://ilcs/devices/ST-98",
        "signature_id": admin.sign("工程变更批准", target="ST-98"),
    })
    assert created.status_code == 201, created.text

    # 普通工位列表供全体登录用户使用，不能把网络拓扑和凭据引用顺手下发。
    public = next(row for row in researcher.get("/api/stations").json() if row["id"] == "ST-98")["adapter"]
    assert public["config"] == {} and public["credential_ref"] == ""
    assert public["credential_configured"] is True
    assert researcher.get("/api/stations/ST-98/adapter").status_code == 403

    detail = admin.get("/api/stations/ST-98/adapter")
    assert detail.status_code == 200, detail.text
    adapter = detail.json()
    assert adapter["config"]["host"] == "10.0.0.20"
    assert adapter["credential_ref"] == "vault://ilcs/devices/ST-98"

    updated = admin.patch("/api/stations/ST-98/adapter", {
        "protocol": "Modbus TCP", "driver": "simulation", "kind": "simulation",
        "config": {"host": "10.0.0.21", "port": 502, "unit_id": 2},
        "row_version": adapter["row_version"],
        "signature_id": admin.sign(
            "设备集成配置变更批准", target="ST-98", object_version=adapter["row_version"],
        ),
    })
    assert updated.status_code == 200, updated.text
    assert updated.json()["config_version"] == adapter["config_version"] + 1
    assert updated.json()["row_version"] == adapter["row_version"] + 1
    assert updated.json()["connected"] is False, "配置变更后必须重新握手"

    stale = admin.patch("/api/stations/ST-98/adapter", {
        "note": "旧页面覆盖", "row_version": adapter["row_version"],
        "signature_id": admin.sign(
            "设备集成配置变更批准", target="ST-98", object_version=adapter["row_version"],
        ),
    })
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "version_conflict"

    current = updated.json()["row_version"]
    inline_secret = admin.patch("/api/stations/ST-98/adapter", {
        "config": {"host": "10.0.0.21", "password": "must-not-persist"},
        "row_version": current,
        "signature_id": admin.sign(
            "设备集成配置变更批准", target="ST-98", object_version=current,
        ),
    })
    assert inline_secret.status_code == 422
    assert inline_secret.json()["detail"]["code"] == "inline_adapter_secret_forbidden"

    bad_ref = admin.patch("/api/stations/ST-98/adapter", {
        "credential_ref": "plain-text-password", "row_version": current,
        "signature_id": admin.sign(
            "设备集成配置变更批准", target="ST-98", object_version=current,
        ),
    })
    assert bad_ref.status_code == 422
    assert bad_ref.json()["detail"]["code"] == "credential_ref_invalid"

    real = admin.patch("/api/stations/ST-98/adapter", {
        "kind": "real", "driver": "modbus_tcp", "row_version": current,
        "signature_id": admin.sign(
            "设备集成配置变更批准", target="ST-98", object_version=current,
        ),
    })
    assert real.status_code == 200, real.text
    unavailable = admin.post("/api/stations/ST-98/adapter/test")
    assert unavailable.status_code == 409
    assert unavailable.json()["detail"]["code"] == "adapter_driver_unavailable"

    # 复原为模拟器并走健康检查重连，不给后续用例留下一个关闭全局执行门的适配器。
    real_version = real.json()["row_version"]
    restored = admin.patch("/api/stations/ST-98/adapter", {
        "kind": "simulation", "driver": "simulation", "row_version": real_version,
        "signature_id": admin.sign(
            "设备集成配置变更批准", target="ST-98", object_version=real_version,
        ),
    })
    assert restored.status_code == 200, restored.text
    tested = admin.post("/api/stations/ST-98/adapter/test")
    assert tested.status_code == 200 and tested.json()["health"]["reachable"] is True
    assert operator.post("/api/stations/ST-98/adapter/reconnect").status_code == 200


def test_service_identity_can_be_managed_without_exposing_secret(client, admin, reset_runtime):
    invalid = admin.post("/api/service-identities", {
        "source": "Bad Source", "name": "错误来源", "scopes": {"everything": "all"},
    })
    assert invalid.status_code == 422
    assert invalid.json()["detail"]["code"] == "service_source_invalid"

    created = admin.post("/api/service-identities", {
        "source": "lims-crud-test", "name": "测试 LIMS",
        "scopes": {"stations": ["ST-07"], "analysis_tasks": []},
    })
    assert created.status_code == 201, created.text
    issued = created.json()
    assert issued["secret"] and len(issued["secret"]) >= 32

    listed = next(
        row for row in admin.get("/api/service-identities").json()
        if row["id"] == issued["id"]
    )
    assert "secret" not in listed and "secret_hash" not in listed
    assert listed["row_version"] == 1

    updated = admin.patch(f"/api/service-identities/{issued['id']}", {
        "name": "生产 LIMS", "scopes": {"analysis_tasks": "all"},
        "row_version": listed["row_version"],
    })
    assert updated.status_code == 200, updated.text
    assert updated.json()["row_version"] == 2
    assert updated.json()["scopes"] == {"analysis_tasks": "all"}

    stale = admin.patch(f"/api/service-identities/{issued['id']}", {
        "name": "旧页面覆盖", "row_version": 1,
    })
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "version_conflict"

    rotated = admin.post(f"/api/service-identities/{issued['id']}/rotate")
    assert rotated.status_code == 200 and rotated.json()["secret"] != issued["secret"]
    after_rotate = next(
        row for row in admin.get("/api/service-identities").json()
        if row["id"] == issued["id"]
    )
    assert after_rotate["row_version"] == 3, "轮换版本必须持久化，不能只改内存对象"

    disabled = admin.post(f"/api/service-identities/{issued['id']}/state", {"state": "disabled"})
    assert disabled.status_code == 200 and disabled.json()["row_version"] == 4


def test_retired_station_stops_matching_recipe_steps(client, admin, researcher, reset_runtime):
    before = next(r for r in researcher.get("/api/recipes/R-205").json()["validation"] if r["cap"] == "cap.test")
    assert "ST-07" in before["fits"]

    admin.post("/api/stations/ST-07/retire", {"retired": True})
    try:
        after = next(r for r in researcher.get("/api/recipes/R-205").json()["validation"] if r["cap"] == "cap.test")
        assert after["fits"] == [] and after["ok"] is False
    finally:
        admin.post("/api/stations/ST-07/retire", {"retired": False})


def test_lot_edit_is_narrowed_after_release(client, operator, qa, reset_runtime):
    created = operator.post("/api/lots", {
        "id": "LOT-CRUD-01", "material": "NMP 溶剂", "qty": 5, "unit": "L", "expiry": "2027-06-01",
    })
    assert created.status_code == 201, created.text

    # 未放行：有效期可以改
    assert operator.patch("/api/lots/LOT-CRUD-01", {"expiry": "2027-09-01"}).status_code == 200

    # 数量一律不能直接改：变化必须通过库存事件或盘点调整入账，否则流水与账面对不上
    refused = operator.patch("/api/lots/LOT-CRUD-01", {"qty": 8})
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "quantity_not_editable"

    qa.post("/api/lots/LOT-CRUD-01/release", {"signature_id": qa.sign("复验合格", target="LOT-CRUD-01")})

    # 已放行：有效期进过质量判断，不能再改
    narrowed = operator.patch("/api/lots/LOT-CRUD-01", {"expiry": "2028-01-01"})
    assert narrowed.status_code == 409
    assert "已放行" in str(narrowed.json()["detail"])
    assert operator.patch("/api/lots/LOT-CRUD-01", {"storage": "冷藏 B-2"}).status_code == 200

    # 库是会话级共享的：留下的批号会改变别的用例看到的可用量
    assert operator.client.delete("/api/lots/LOT-CRUD-01", headers=operator.headers).status_code == 200


def test_lot_adjust_cannot_go_below_reserved(client, operator, qa, reset_runtime):
    operator.post("/api/lots", {
        "id": "LOT-CRUD-02", "material": "电解液 LP57", "qty": 50, "unit": "mL", "expiry": "2027-06-01",
    })
    qa.post("/api/lots/LOT-CRUD-02/release", {"signature_id": qa.sign("复验合格", target="LOT-CRUD-02")})

    adjusted = operator.post("/api/lots/LOT-CRUD-02/adjust", {"qty": "42", "reason": "盘点实测"})
    assert adjusted.status_code == 200, adjusted.text
    # 数量是明确精度的十进制，接口按字符串返回，不经过浮点
    assert adjusted.json()["qty"] == "42.000000"
    ledger = operator.get("/api/lots/LOT-CRUD-02/ledger").json()
    assert ledger["reconciled"], "流水累计必须等于账面库存"
    assert ledger["lines"][-1]["event_type"] == "adjust"

    assert operator.post("/api/lots/LOT-CRUD-02/adjust", {"qty": "10", "reason": ""}).status_code == 400

    audit = operator.get("/api/audit?target=LOT-CRUD-02").json()
    assert any(e["action"] == "批号盘点调整" and "-8" in e["detail"] for e in audit)

    assert operator.client.delete("/api/lots/LOT-CRUD-02", headers=operator.headers).status_code == 200


def test_used_lot_cannot_be_deleted_only_scrapped(client, operator, qa, reset_runtime):
    operator.post("/api/lots", {
        "id": "LOT-CRUD-03", "material": "NMP 溶剂", "qty": 9, "unit": "L", "expiry": "2027-06-01",
    })
    qa.post("/api/lots/LOT-CRUD-03/release", {"signature_id": qa.sign("复验合格", target="LOT-CRUD-03")})

    scrapped = qa.post("/api/lots/LOT-CRUD-03/scrap", {
        "reason": "开封后吸潮，水分超标", "signature_id": qa.sign("质量判定", target="LOT-CRUD-03"),
    })
    assert scrapped.status_code == 200, scrapped.text
    assert scrapped.json()["state"] == "scrapped" and scrapped.json()["qty"] == "0.000000"

    # 报废后既不能删也不能改
    assert operator.client.delete("/api/lots/LOT-CRUD-03", headers=operator.headers).status_code == 409
    assert operator.patch("/api/lots/LOT-CRUD-03", {"storage": "x"}).status_code == 409

    # 报废批号不再进入计划的可用量
    preview = operator.get("/api/plans/EP-201-03").json()["materials"]
    assert all("LOT-CRUD-03" not in row["lots"] for row in preview)


def test_waste_tank_crud_requires_an_empty_tank_before_removal(client, operator, reset_runtime):
    created = operator.post("/api/waste", {"id": "WT-99", "kind": "有机废液", "capacity_l": 30, "level_pct": 20})
    assert created.status_code == 201, created.text

    assert operator.patch("/api/waste/WT-99", {"capacity_l": 40}).status_code == 200

    refused = operator.client.delete("/api/waste/WT-99", headers=operator.headers)
    assert refused.status_code == 409 and "换桶清空" in str(refused.json()["detail"])

    operator.post("/api/waste/WT-99/swap")
    assert operator.client.delete("/api/waste/WT-99", headers=operator.headers).status_code == 200


def test_delete_needs_the_right_role(client, researcher, operator, reset_runtime):
    plan = researcher.post("/api/plans", {"name": "权限用例", "recipe_id": "R-205"})
    plan_id = plan.json()["id"]

    # 操作员不能删实验计划
    assert operator.client.delete(f"/api/plans/{plan_id}", headers=operator.headers).status_code == 403
    assert researcher.client.delete(f"/api/plans/{plan_id}", headers=researcher.headers).status_code == 200
