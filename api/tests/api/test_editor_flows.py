"""图形化编辑器与运维动作依赖的接口：步骤保存与差异、能力登记、重连、指令转人工核查。"""


def test_saving_steps_records_a_diff_and_a_history_entry(client, researcher, reset_runtime):
    draft = researcher.post("/api/recipes", {"name": "编辑器差异用例", "plate": 8, "copy_from": "R-201"})
    recipe_id = draft.json()["id"]
    steps = draft.json()["steps"]
    history_before = len(draft.json()["history"])

    steps[2]["params"]["rpm"] = 2400
    saved = researcher.patch(f"/api/recipes/{recipe_id}", {"steps": steps, "bom": draft.json()["bom"]})

    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert len(body["history"]) == history_before + 1
    signs = [sign for sign, _ in body["diff"]]
    assert signs == ["-", "+"]
    assert "rpm=2400" in body["diff"][1][1]
    assert body["valid"] is True


def test_incomplete_steps_block_submission_and_say_which_step(client, researcher, reset_runtime):
    draft = researcher.post("/api/recipes", {"name": "编辑器校验用例", "plate": 8, "copy_from": "R-201"})
    recipe_id = draft.json()["id"]
    steps = draft.json()["steps"]
    steps[1]["dur"] = 0

    saved = researcher.patch(f"/api/recipes/{recipe_id}", {"steps": steps})
    assert saved.json()["valid"] is False
    assert saved.json()["validation"][1]["issues"] == ["计划时长必须大于 0"]
    # 越限与填写缺失分开：这一步仍有能承接的工位
    assert saved.json()["validation"][1]["fits"]

    complete = {check["key"]: check for check in saved.json()["checks"]}["complete"]
    assert complete["ok"] is False and "第 2 步" in complete["detail"]

    assert researcher.post(f"/api/recipes/{recipe_id}/submit").status_code == 409


def test_recipe_checks_flag_an_empty_bom_without_blocking_the_draft(client, researcher, reset_runtime):
    draft = researcher.post("/api/recipes", {"name": "空 BOM 用例", "plate": 8, "copy_from": "R-201"})
    recipe_id = draft.json()["id"]

    saved = researcher.patch(f"/api/recipes/{recipe_id}", {"bom": []})

    assert saved.status_code == 200
    checks = {check["key"]: check for check in saved.json()["checks"]}
    assert checks["bom"]["ok"] is False
    assert checks["risk"]["ok"] is True


def test_registering_a_capability_needs_a_signature_and_admin_role(client, admin, researcher, reset_runtime):
    payload = {
        "id": "cap.sonicate", "name": "超声分散",
        "params": {"power": "超声功率 W", "minutes": "超声时长 min"},
        "recovery": {"pausable": True, "maxHoldMin": 15, "hold": "换能器停振", "retryable": True, "verify": ["累计超声时间"]},
        "stations": ["ST-02"],
    }

    assert researcher.post("/api/capabilities", {**payload, "signature_id": ""}).status_code == 403

    created = admin.post(
        "/api/capabilities",
        {**payload, "signature_id": admin.sign("能力模型变更批准", target="cap.sonicate")},
    )
    assert created.status_code == 201, created.text

    listed = {row["id"]: row for row in admin.get("/api/capabilities").json()}
    assert listed["cap.sonicate"]["stations"] == ["ST-02"]
    assert listed["cap.sonicate"]["recovery"]["retryable"] is True

    # 重复标识被拒
    assert admin.post(
        "/api/capabilities",
        {**payload, "signature_id": admin.sign("能力模型变更批准", target="cap.sonicate")},
    ).status_code == 400


def test_reconnect_brings_an_adapter_back_online_and_writes_audit(client, operator, reset_runtime):
    client.post("/api/runtime/stations/ST-03/heartbeat", json={"connected": False})
    assert operator.get("/api/stations").json()

    reconnected = operator.post("/api/stations/ST-03/adapter/reconnect")

    assert reconnected.status_code == 200, reconnected.text
    assert reconnected.json()["adapter"]["connected"] is True
    events = operator.get("/api/audit?target=ST-03").json()
    assert events[0]["action"] == "重连设备适配器"
    assert events[0]["after"] == "在线"


def test_only_unknown_commands_can_be_sent_to_manual_review(client, operator, db, reset_runtime):
    from app.models import Command

    batch_id = operator.post("/api/batches", {"plan_id": "EP-205-01"}).json()["id"]
    command = Command(
        org_id="ORG-001", batch_id=batch_id, station_id="ST-03", capability="cap.coat",
        type="dispatch", state="unknown", step_index=4,
    )
    db.add(command)
    db.commit()
    command_id = command.id

    moved = operator.post(f"/api/commands/{command_id}/manual-review")
    assert moved.status_code == 200, moved.text
    assert moved.json()["state"] == "manual"

    # 已经转过的不能再转，避免把人工结论覆盖掉
    assert operator.post(f"/api/commands/{command_id}/manual-review").status_code == 400


def test_telemetry_endpoint_groups_points_into_series(client, operator, db, reset_runtime):
    from app.core.clock import now
    from app.models import Telemetry

    batch_id = operator.post("/api/batches", {"plan_id": "EP-201-03"}).json()["id"]
    for index in range(5):
        db.add(Telemetry(station_id="ST-01-A", batch_id=batch_id, metric="temp",
                         setpoint=25.0, value=24.0 + index * 0.2, device_ts=now()))
    db.commit()

    feed = operator.get(f"/api/batches/{batch_id}/telemetry")

    assert feed.status_code == 200, feed.text
    series = {row["metric"]: row for row in feed.json()["series"]}
    assert "temp" in series
    assert len(series["temp"]["points"]) >= 5
    assert series["temp"]["setpoint"] == 25.0
