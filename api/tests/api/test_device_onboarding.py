"""接入一台设备的增删改：已有工位补接设备、资产关联的取消与更换、工位不再承接某项能力。

- 登记工位时没填协议的，之后照样能接设备（手工配置或套已发布的设备接入模板），检查与登记新工位时一起建的同一套；
- 取消 / 更换资产关联有入口；工位已关联别的资产时要明确说「移过来」；
- 工位上还有未结束批次的时间窗时，换关联、移除在用的能力都被拒绝——已排下的工步和新口径对不上。
"""
import copy

import pytest

# HTTPS 网关（http_json_v1）的接口映射：模板里放它，连接（base_url）由工位填
GATEWAY = {
    "paths": {"health": "/health", "submit": "/commands", "query": "/commands/{command_id}",
              "hold": "/commands/{command_id}/hold", "abort": "/commands/{command_id}/abort"},
    "idempotency_header": "Idempotency-Key", "request_timeout_sec": 1, "connect_timeout_sec": 1,
}
LOCAL = {"base_url": "https://127.0.0.1:8443/api/v1"}


def _station(admin, station_id: str, **extra) -> None:
    created = admin.post("/api/stations", {
        "id": station_id, "name": f"接入测试 {station_id}", "limits": {}, **extra,
        "signature_id": admin.sign("工程变更批准", target=station_id),
    })
    assert created.status_code == 201, created.text


def _row(session, station_id: str) -> dict:
    return next(row for row in session.get("/api/stations").json() if row["id"] == station_id)


def _connect(session, station_id: str, **payload):
    return session.post(f"/api/stations/{station_id}/adapter", {
        **payload, "signature_id": session.sign("设备集成配置变更批准", target=station_id),
    })


def _retire(admin, station_id: str) -> None:
    adapter = admin.get(f"/api/stations/{station_id}/adapter")
    if adapter.status_code == 200 and adapter.json()["kind"] == "real":
        # 真实设备退回模拟：别让停用工位上的真实适配器留在执行门里
        version = adapter.json()["row_version"]
        reset = admin.patch(f"/api/stations/{station_id}/adapter", {
            "kind": "simulation", "driver": "simulation", "config": {}, "credential_ref": "", "template_id": "",
            "row_version": version,
            "signature_id": admin.sign("设备集成配置变更批准", target=station_id, object_version=version),
        })
        assert reset.status_code == 200, reset.text
    assert admin.post(f"/api/stations/{station_id}/retire", {"retired": True}).status_code == 200


@pytest.fixture()
def planned(operator, reset_runtime):
    """一批排好程、还没开跑的批次：它的时间窗落在哪台工位、用的是哪项能力（先交班，别被清退）。"""
    from app.core.db import SessionLocal
    from app.domain.steps import normalize
    from app.models import Allocation, Batch

    created = operator.post("/api/batches", {"plan_id": "EP-205-01"})
    assert created.status_code == 201, created.text
    batch_id = created.json()["id"]
    assert operator.post(f"/api/batches/{batch_id}/schedule", {}).status_code == 200
    with SessionLocal() as db:
        allocation = db.query(Allocation).filter(Allocation.batch_id == batch_id, Allocation.kind == "work").first()
        steps = normalize(db.get(Batch, batch_id).recipe_snapshot.get("steps") or [])
        found = {"batch_id": batch_id, "station_id": allocation.station_id, "cap": steps[allocation.step_index]["cap"]}
    yield found
    with SessionLocal() as db:
        db.query(Allocation).filter(Allocation.batch_id == batch_id).delete()
        db.get(Batch, batch_id).state = "aborted"
        db.commit()


def test_an_existing_station_can_be_connected_later(admin, researcher, reset_runtime):
    _station(admin, "ST-ONB-1")
    try:
        assert admin.get("/api/stations/ST-ONB-1/adapter").status_code == 404
        assert _row(admin, "ST-ONB-1")["adapter"] is None

        assert _connect(researcher, "ST-ONB-1", protocol="sim").status_code == 403
        missing = _connect(admin, "ST-ONB-1", kind="simulation")
        assert missing.status_code == 422 and missing.json()["detail"]["code"] == "adapter_protocol_required"

        created = _connect(admin, "ST-ONB-1", protocol="内置模拟", kind="simulation")
        assert created.status_code == 201, created.text
        adapter = created.json()
        assert adapter["kind"] == "simulation" and adapter["driver"] == "simulation"
        assert adapter["config_version"] == 1 and adapter["acceptance"]["required"] == ""
        assert adapter["connected"] is False, "新接入的设备先离线，等执行器握手"
        assert _row(admin, "ST-ONB-1")["adapter"]["protocol"] == "内置模拟"

        again = _connect(admin, "ST-ONB-1", protocol="内置模拟")
        assert again.status_code == 409 and again.json()["detail"]["code"] == "adapter_exists"
        audit = admin.get("/api/audit", params={"target": "ST-ONB-1"}).json()
        assert any(row["action"] == "登记设备适配器" for row in audit), audit
    finally:
        _retire(admin, "ST-ONB-1")

    _station(admin, "ST-ONB-5")
    _retire(admin, "ST-ONB-5")
    retired = _connect(admin, "ST-ONB-5", protocol="内置模拟")
    assert retired.status_code == 409 and retired.json()["detail"]["code"] == "station_retired"


def test_connecting_a_real_device_uses_the_same_checks(admin, qa, reset_runtime):
    from app.core.db import SessionLocal
    from app.models import AcceptanceRun

    _station(admin, "ST-ONB-2")
    try:
        invalid = _connect(admin, "ST-ONB-2", kind="real", driver="http_json_v1", protocol="HTTPS JSON",
                           config=copy.deepcopy(GATEWAY))
        assert invalid.status_code == 422 and invalid.json()["detail"]["code"] == "adapter_config_invalid", "缺网关地址"
        secret = _connect(admin, "ST-ONB-2", kind="real", driver="http_json_v1", protocol="HTTPS JSON",
                          config={**copy.deepcopy(GATEWAY), **LOCAL, "password": "plain"})
        assert secret.status_code == 422, secret.text
        orphan = _connect(admin, "ST-ONB-2", template_connection=dict(LOCAL))
        assert orphan.status_code == 422 and orphan.json()["detail"]["code"] == "template_required"
        assert admin.get("/api/stations/ST-ONB-2/adapter").status_code == 404, "被拒的登记不留半份适配器"

        draft = admin.post("/api/device-templates", {
            "code": "TPL-ONBOARD-OVEN", "name": "真空干燥箱网关（接入测试）", "driver": "http_json_v1",
            "protocol": "HTTPS JSON", "model": "VAC-OVEN-80",
            "config": copy.deepcopy(GATEWAY),
            "connection": {"base_url": "https://oven-01.lab.internal/api/v1"},
            "supports": {"hold": True, "abort": True, "query": True, "dedup": True},
            "acceptance": {"capability": "cap.vacuum_dry", "params": {"temp": 120, "vacuum": 1}},
        })
        assert draft.status_code == 201, draft.text
        assert draft.json()["check"]["ok"], draft.json()["check"]
        unreleased = _connect(admin, "ST-ONB-2", template_id=draft.json()["id"], template_connection=dict(LOCAL))
        assert unreleased.status_code == 409 and unreleased.json()["detail"]["code"] == "template_not_released"
        released = qa.post(f"/api/device-templates/{draft.json()['id']}/release", {
            "row_version": draft.json()["row_version"],
            "signature_id": qa.sign("发布设备接入模板", target=draft.json()["id"], object_version=draft.json()["row_version"]),
        })
        assert released.status_code == 200, released.text

        created = _connect(admin, "ST-ONB-2", template_id=draft.json()["id"], template_connection=dict(LOCAL),
                           credential_ref="vault://ilcs/devices/ST-ONB-2")
        assert created.status_code == 201, created.text
        adapter = created.json()
        assert adapter["kind"] == "real" and adapter["driver"] == "http_json_v1"
        assert adapter["template"]["code"] == "TPL-ONBOARD-OVEN" and adapter["template"]["revision"] == 1
        # 第一次接真实设备：欠动作级验收，执行器先自动跑一次只读级
        assert adapter["acceptance"]["required"] == "physical"
        detail = admin.get("/api/stations/ST-ONB-2/adapter").json()
        assert detail["config"]["base_url"] == LOCAL["base_url"] and "paths" in detail["config"]
        assert detail["template_connection"] == LOCAL
        with SessionLocal() as db:
            queued = db.query(AcceptanceRun).filter(
                AcceptanceRun.station_id == "ST-ONB-2", AcceptanceRun.state == "queued",
            ).all()
            assert [run.level for run in queued] == ["readonly"]
    finally:
        _retire(admin, "ST-ONB-2")


def test_asset_links_can_be_moved_and_removed(admin, reset_runtime):
    _station(admin, "ST-ONB-3", model="SONIC-1")
    first = admin.post("/api/assets", {"asset_no": "AS-ONB-1", "name": "接入测试资产一", "model": "SONIC-1"}).json()
    second = admin.post("/api/assets", {"asset_no": "AS-ONB-2", "name": "接入测试资产二", "model": "SONIC-2"}).json()
    try:
        assert admin.post(f"/api/assets/{first['id']}/stations", {"station_id": "ST-ONB-3"}).status_code == 200
        before = _row(admin, "ST-ONB-3")
        assert before["asset_id"] == first["id"] and before["model"] == "SONIC-1"

        # 已关联别的资产：不带 move 不动，带了才移过来，容量、校准与型号改按新资产
        refused = admin.post(f"/api/assets/{second['id']}/stations", {"station_id": "ST-ONB-3"})
        assert refused.status_code == 409 and refused.json()["detail"]["code"] == "station_linked_elsewhere"
        assert "AS-ONB-1" in refused.json()["detail"]["message"]
        assert _row(admin, "ST-ONB-3")["asset_id"] == first["id"]
        moved = admin.post(f"/api/assets/{second['id']}/stations", {"station_id": "ST-ONB-3", "move": True})
        assert moved.status_code == 200, moved.text
        assert "不一致" in moved.json()["warning"], "工位原型号 SONIC-1 与新资产 SONIC-2 对不上，要提示核对"
        row = _row(admin, "ST-ONB-3")
        assert row["asset_id"] == second["id"] and row["model"] == "SONIC-2"
        assert row["row_version"] > before["row_version"]
        assert "ST-ONB-3" not in admin.get(f"/api/assets/{first['id']}").json()["station_ids"]

        wrong = admin.delete(f"/api/assets/{first['id']}/stations/ST-ONB-3")
        assert wrong.status_code == 409 and wrong.json()["detail"]["code"] == "station_not_linked"
        unlinked = admin.delete(f"/api/assets/{second['id']}/stations/ST-ONB-3")
        assert unlinked.status_code == 200, unlinked.text
        assert "ST-ONB-3" not in unlinked.json()["station_ids"]
        row = _row(admin, "ST-ONB-3")
        assert row["asset"] is None and row["model_source"] == "station" and row["model"] == "SONIC-1", \
            "取消关联后工位不能没了型号：留着的旧登记照用"
    finally:
        _retire(admin, "ST-ONB-3")


def test_links_do_not_change_under_scheduled_work(admin, planned):
    from app.core.db import SessionLocal
    from app.models import Station

    station_id = planned["station_id"]
    with SessionLocal() as db:
        asset_id = db.get(Station, station_id).asset_id
    assert asset_id, "排程落点是关联了资产的工位"
    spare = admin.post("/api/assets", {"asset_no": "AS-ONB-3", "name": "接入测试备用资产"}).json()

    unlink = admin.delete(f"/api/assets/{asset_id}/stations/{station_id}")
    assert unlink.status_code == 409 and unlink.json()["detail"]["code"] == "station_has_open_allocations"
    move = admin.post(f"/api/assets/{spare['id']}/stations", {"station_id": station_id, "move": True})
    assert move.status_code == 409 and move.json()["detail"]["code"] == "station_has_open_allocations"
    row = _row(admin, station_id)
    ledger = admin.patch(f"/api/stations/{station_id}", {"asset_id": "", "row_version": row["row_version"]})
    assert ledger.status_code == 409 and ledger.json()["detail"]["code"] == "station_has_open_allocations"
    # 不动关联的台账修改照常
    renamed = admin.patch(f"/api/stations/{station_id}", {"name": row["name"], "row_version": row["row_version"]})
    assert renamed.status_code == 200, renamed.text


def test_a_station_can_stop_offering_a_capability(admin, planned):
    _station(admin, "ST-ONB-4", limits={
        "cap.vacuum_dry": {"temp": [60, 180], "vacuum": [0.1, 5]}, "cap.transfer": {},
    })
    try:
        row = _row(admin, "ST-ONB-4")
        sign = lambda: admin.sign("工程变更批准", target="ST-ONB-4")  # noqa: E731
        both = admin.patch("/api/stations/ST-ONB-4/limits", {
            "limits": {"cap.transfer": {}}, "remove": ["cap.transfer"],
            "row_version": row["row_version"], "signature_id": sign(),
        })
        assert both.status_code == 400
        unknown = admin.patch("/api/stations/ST-ONB-4/limits", {
            "remove": ["cap.not_here"], "row_version": row["row_version"], "signature_id": sign(),
        })
        assert unknown.status_code == 400 and "cap.not_here" in unknown.json()["detail"]["message"]

        removed = admin.patch("/api/stations/ST-ONB-4/limits", {
            "limits": {"cap.vacuum_dry": {"temp": [70, 170]}}, "remove": ["cap.transfer"],
            "row_version": row["row_version"], "signature_id": sign(),
        })
        assert removed.status_code == 200, removed.text
        assert removed.json()["limits"] == {"cap.vacuum_dry": {"temp": [70, 170], "vacuum": [0.1, 5]}}
        assert _row(admin, "ST-ONB-4")["row_version"] > row["row_version"]
    finally:
        _retire(admin, "ST-ONB-4")

    # 排好程的批次还要在这台工位上用这项能力：不能移除
    busy = _row(admin, planned["station_id"])
    refused = admin.patch(f"/api/stations/{planned['station_id']}/limits", {
        "remove": [planned["cap"]], "row_version": busy["row_version"],
        "signature_id": admin.sign("工程变更批准", target=planned["station_id"]),
    })
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "capability_in_use_on_station"
    assert planned["batch_id"] in refused.json()["detail"]["blocked"][0]["label"]
    assert planned["cap"] in _row(admin, planned["station_id"])["limits"]


def test_a_new_station_can_be_registered_onto_an_asset(admin, reset_runtime):
    """登记表单只登记台账与关联的仪器设备：型号以资产为准，通道不能超过资产容量。"""
    asset = admin.post("/api/assets", {
        "asset_no": "AS-ONB-6", "name": "接入测试分散机", "model": "DISP-3", "capacity": 2,
    }).json()
    too_wide = admin.post("/api/stations", {
        "id": "ST-ONB-6", "name": "分散机", "asset_id": asset["id"], "channels": 3, "limits": {},
        "signature_id": admin.sign("工程变更批准", target="ST-ONB-6"),
    })
    assert too_wide.status_code == 409 and too_wide.json()["detail"]["code"] == "channels_exceed_asset_capacity"
    _station(admin, "ST-ONB-6", asset_id=asset["id"], channels=2)
    try:
        row = _row(admin, "ST-ONB-6")
        assert row["asset_id"] == asset["id"] and row["model"] == "DISP-3" and row["model_source"] == "asset"
        assert row["adapter"] is None and row["limits"] == {}
        assert admin.get(f"/api/assets/{asset['id']}").json()["station_ids"] == ["ST-ONB-6"]
    finally:
        _retire(admin, "ST-ONB-6")


def test_registering_a_capability_onto_stations_bumps_their_version(admin, reset_runtime):
    """接口还能在登记能力时一并写工位极限（界面不再这么做）：写了就递增工位行版本，旧版本的极限编辑会被拒。"""
    _station(admin, "ST-ONB-7")
    try:
        before = _row(admin, "ST-ONB-7")
        created = admin.post("/api/capabilities", {
            "id": "cap.onboard_probe", "name": "接入测试探针", "params": {"depth": "深度"},
            "recovery": {"pausable": False, "retryable": True}, "stations": ["ST-ONB-7"],
            "signature_id": admin.sign("能力模型变更批准", target="cap.onboard_probe"),
        })
        assert created.status_code == 201, created.text
        after = _row(admin, "ST-ONB-7")
        assert after["limits"] == {"cap.onboard_probe": {"depth": [0, 100]}}
        assert after["row_version"] > before["row_version"]
        stale = admin.patch("/api/stations/ST-ONB-7/limits", {
            "limits": {}, "row_version": before["row_version"],
            "signature_id": admin.sign("工程变更批准", target="ST-ONB-7"),
        })
        assert stale.status_code == 409
    finally:
        _retire(admin, "ST-ONB-7")


def test_a_mistaken_station_can_be_deleted_once_retired(admin, reset_runtime):
    """登记错了、从没用过的工位：先停用再删，连同它的模拟适配器；删了以后标识可以重登。"""
    _station(admin, "ST-ONB-8", limits={"cap.transfer": {}})
    assert _connect(admin, "ST-ONB-8", protocol="内置模拟").status_code == 201
    active = admin.delete("/api/stations/ST-ONB-8")
    assert active.status_code == 409 and active.json()["detail"]["code"] == "station_in_use"
    assert any("先停用" in row["label"] for row in active.json()["detail"]["blocked"])
    assert admin.post("/api/stations/ST-ONB-8/retire", {"retired": True}).status_code == 200
    assert admin.get("/api/stations/ST-ONB-8/delete-blockers").json()["blockers"] == []

    deleted = admin.delete("/api/stations/ST-ONB-8")
    assert deleted.status_code == 200, deleted.text
    assert all(row["id"] != "ST-ONB-8" for row in admin.get("/api/stations").json())
    assert admin.get("/api/stations/ST-ONB-8/adapter").status_code == 404
    audit = admin.get("/api/audit", params={"target": "ST-ONB-8"}).json()
    assert any(row["action"] == "删除工位" and "连同适配器" in row["detail"] for row in audit), audit
    _station(admin, "ST-ONB-8")
    _retire(admin, "ST-ONB-8")


def test_a_used_station_cannot_be_deleted(admin, planned):
    """排过工步、做过接入验收的工位只能停用：删除理由逐条列出。"""
    busy = admin.get(f"/api/stations/{planned['station_id']}/delete-blockers").json()["blockers"]
    assert any("先停用" in row for row in busy) and any(row.startswith("工步时间窗") for row in busy), busy
    assert admin.delete(f"/api/stations/{planned['station_id']}").status_code == 409

    # 真实设备一接入就排了一次只读级验收：验收记录是上线证据，工位只能停用
    _station(admin, "ST-ONB-9")
    created = _connect(admin, "ST-ONB-9", kind="real", driver="http_json_v1", protocol="HTTPS JSON",
                       config={**copy.deepcopy(GATEWAY), **LOCAL})
    assert created.status_code == 201, created.text
    _retire(admin, "ST-ONB-9")
    refused = admin.delete("/api/stations/ST-ONB-9")
    assert refused.status_code == 409
    assert any(row["label"].startswith("接入验收记录") for row in refused.json()["detail"]["blocked"])


def test_a_mistaken_asset_can_be_deleted_once_retired(admin, reset_runtime):
    asset = admin.post("/api/assets", {"asset_no": "AS-ONB-9", "name": "录错的资产"}).json()
    active = admin.delete(f"/api/assets/{asset['id']}")
    assert active.status_code == 409 and active.json()["detail"]["code"] == "asset_in_use"
    retired = admin.patch(f"/api/assets/{asset['id']}", {"state": "retired", "row_version": asset["row_version"]})
    assert retired.status_code == 200, retired.text
    assert admin.get(f"/api/assets/{asset['id']}/delete-blockers").json()["blockers"] == []
    deleted = admin.delete(f"/api/assets/{asset['id']}")
    assert deleted.status_code == 200, deleted.text
    assert admin.get(f"/api/assets/{asset['id']}").status_code == 404
    again = admin.post("/api/assets", {"asset_no": "AS-ONB-9", "name": "重登的资产"})
    assert again.status_code == 201, "删了以后资产号可以重登"

    # 关联过工位、登记过校准的资产只能退役
    used = admin.post("/api/assets", {
        "asset_no": "AS-ONB-10", "name": "用过的资产", "calibration_applicable": False,
        "calibration_exempt_reason": "无计量输出",
    }).json()
    _station(admin, "ST-ONB-10")
    try:
        assert admin.post(f"/api/assets/{used['id']}/stations", {"station_id": "ST-ONB-10"}).status_code == 200
        current = admin.get(f"/api/assets/{used['id']}").json()
        assert admin.patch(f"/api/assets/{used['id']}", {"state": "retired", "row_version": current["row_version"]}).status_code == 200
        blockers = admin.get(f"/api/assets/{used['id']}/delete-blockers").json()["blockers"]
        assert blockers == ["关联工位 1 条"], blockers
        assert admin.delete(f"/api/assets/{used['id']}").status_code == 409
    finally:
        _retire(admin, "ST-ONB-10")


def test_deleting_needs_the_edit_permission(researcher, operator):
    assert researcher.delete("/api/stations/ST-05").status_code == 403
    assert operator.get("/api/stations/ST-05/delete-blockers").status_code == 403
    assert researcher.delete("/api/assets/whatever").status_code == 403
