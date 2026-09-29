"""设备接入模板：一类设备怎么接存成要发布的模板；工位 = 模板的某一版 + 自己的连接参数。

驱动在 adapters/catalog 里声明自己的配置项：界面据此出表单，保存前据此校验（配错了当场说清楚）。
模板发布要另一个人签名，发布后内容冻结；升级不自动推给工位，由人逐台切换、重新验收。
"""
import copy

import pytest
from sqlalchemy import text

from sim_harness import OVEN_MAP, line_sim

LIMITS = {"cap.vacuum_dry": {"temp": [60, 180], "vacuum": [0.1, 5]}}


def _oven_template(code: str = "TPL-OVEN-VD") -> dict:
    mapping = copy.deepcopy(OVEN_MAP)
    return {
        "code": code, "name": "真空干燥箱（文本命令）", "driver": "line_command_v1", "protocol": "串口 / TCP 命令",
        "version": "cmd-manual-2.3", "model": "VAC-OVEN-80", "vendor": "示例厂家",
        "config": {**mapping, "request_timeout_sec": 1, "connect_timeout_sec": 1, "probe_interval_sec": 0.5},
        "connection": {"transport": {"kind": "tcp", "host": "oven-01.lab.internal", "port": 4001}},
        "supports": {"hold": True, "abort": True, "query": True, "dedup": True},
        "acceptance": {"capability": "cap.vacuum_dry", "params": {"temp": 120, "vacuum": 1}},
        "note": "命令手册 2.3 版",
    }


@pytest.fixture()
def station(admin):
    from app.core.db import SessionLocal
    from app.models import Station

    with SessionLocal() as db:
        exists = db.get(Station, "ST-95") is not None
    if not exists:
        created = admin.post("/api/stations", {
            "id": "ST-95", "name": "模板套用测试站", "protocol": "sim", "limits": LIMITS,
            "adapter_kind": "simulation", "adapter_driver": "simulation",
            "signature_id": admin.sign("工程变更批准", target="ST-95"),
        })
        assert created.status_code == 201, created.text
    else:
        assert admin.post("/api/stations/ST-95/retire", {"retired": False}).status_code == 200
    yield "ST-95"
    _patch(admin, "ST-95", kind="simulation", driver="simulation", protocol="sim", config={}, credential_ref="",
           template_id="")
    assert admin.post("/api/stations/ST-95/retire", {"retired": True}).status_code == 200


def _patch(admin, station_id: str, **changes):
    adapter = admin.get(f"/api/stations/{station_id}/adapter").json()
    return admin.patch(f"/api/stations/{station_id}/adapter", {
        **changes, "row_version": adapter["row_version"],
        "signature_id": admin.sign("设备集成配置变更批准", target=station_id, object_version=adapter["row_version"]),
    })


def _release(session, template: dict):
    return session.post(f"/api/device-templates/{template['id']}/release", {
        "row_version": template["row_version"],
        "signature_id": session.sign("发布设备接入模板", target=template["id"], object_version=template["row_version"]),
    })


def _station_pass(station_id: str) -> dict:
    from app.core.db import SessionLocal
    from app.services.execution_service import ExecutorLoop

    with SessionLocal() as db:
        return ExecutorLoop(db).station_pass(station_id)


def test_driver_catalog_describes_every_registered_driver(admin):
    from app.adapters.registry import REAL_IMPLEMENTATIONS

    drivers = admin.get("/api/drivers", params={"station_id": "ST-05"}).json()
    assert {row["key"] for row in drivers} == set(REAL_IMPLEMENTATIONS)
    line = next(row for row in drivers if row["key"] == "line_command_v1")
    assert "transport" in line["connection_keys"] and "status" in {f["name"] for f in line["fields"] if f["required"]}
    # 起步模板按工位能力极限生成（以前写死在前端）
    limits = next(row for row in admin.get("/api/stations").json() if row["id"] == "ST-05")["limits"]
    assert set(line["template"]["capabilities"]) == set(limits)
    assert not {"sql_table_v1", "composite_v1", "mt_sics_v1"} & {row["key"] for row in drivers}, "删掉的驱动不再登记"


def test_invalid_config_is_rejected_when_saving(admin, station):
    checked = admin.post(f"/api/stations/{station}/adapter/check", {
        "driver": "line_command_v1", "config": {"transport": {"kind": "tcp", "host": "127.0.0.1", "port": 4001},
                                                 "capabilities": {}, "stauts": {}},
    }).json()
    assert not checked["ok"] and any("status" in problem for problem in checked["problems"])
    assert any("stauts" in warning for warning in checked["warnings"]), "拼错的键只提醒，驱动会忽略它"

    saved = _patch(admin, station, kind="real", driver="line_command_v1", protocol="串口 / TCP 命令",
                   config={"transport": {"kind": "tcp", "host": "10.99.0.1", "port": 4001}, **copy.deepcopy(OVEN_MAP)})
    assert saved.status_code == 422, saved.text
    detail = saved.json()["detail"]
    assert detail["code"] == "adapter_config_invalid" and any("白名单" in item for item in detail["problems"])
    bad_regex = _patch(admin, station, kind="real", driver="line_command_v1", protocol="串口 / TCP 命令",
                       config={"transport": {"kind": "tcp", "host": "127.0.0.1", "port": 4001},
                               **{**copy.deepcopy(OVEN_MAP), "error_pattern": "([unclosed"}})
    assert bad_regex.status_code == 422 and "正则" in bad_regex.json()["detail"]["message"]
    # 停用、改说明不因为配置检查被挡住
    assert _patch(admin, station, note="待接线").status_code == 200


def test_template_lifecycle_apply_and_upgrade(admin, qa, station, reset_runtime):
    created = admin.post("/api/device-templates", _oven_template())
    assert created.status_code == 201, created.text
    draft = created.json()
    assert draft["state"] == "draft" and draft["revision"] == 1 and draft["check"]["ok"], draft["check"]
    assert "transport" in draft["connection_keys"]

    self_release = _release(admin, draft)
    assert self_release.status_code == 403 and self_release.json()["detail"]["code"] == "self_approval"
    released = _release(qa, draft)
    assert released.status_code == 200, released.text
    r1 = released.json()
    assert r1["state"] == "released" and r1["digest"].startswith("sha256:")
    assert admin.patch(f"/api/device-templates/{r1['id']}", {"note": "改一下", "row_version": r1["row_version"]}).status_code == 409

    with line_sim(task_seconds=0.3) as (_, _, port):
        connection = {"transport": {"kind": "tcp", "host": "127.0.0.1", "port": port}}
        applied = _patch(admin, station, template_id=r1["id"], template_connection=connection)
        assert applied.status_code == 200, applied.text
        adapter = applied.json()
        assert adapter["kind"] == "real" and adapter["driver"] == "line_command_v1"
        assert adapter["template"]["code"] == "TPL-OVEN-VD" and adapter["template"]["revision"] == 1
        assert adapter["acceptance"]["required"] == "physical", "第一次接成真实设备"
        detail = admin.get(f"/api/stations/{station}/adapter").json()
        assert detail["config"]["transport"]["port"] == port and detail["config"]["status"] == OVEN_MAP["status"]
        assert detail["template_connection"] == connection

        _station_pass(station)
        runs = admin.get(f"/api/stations/{station}/adapter/acceptance").json()
        assert runs["gate"]["required"] == "" and runs["runs"][0]["template"]["revision"] == 1, runs
        assert runs["defaults"] == {"capability": "cap.vacuum_dry", "params": {"temp": 120, "vacuum": 1},
                                    "source": "template"}, "申请验收的缺省取模板的验收缺省"
        report = admin.get(f"/api/acceptance-runs/{runs['runs'][0]['id']}").json()["report_md"]
        assert "设备接入模板 TPL-OVEN-VD r1" in report

        # 新修订发布：旧版退役，但不自动推给工位——列出来，由人逐台切换
        revised = admin.post(f"/api/device-templates/{r1['id']}/revise")
        assert revised.status_code == 201, revised.text
        r2 = revised.json()
        assert r2["revision"] == 2 and r2["state"] == "draft"
        updated = admin.patch(f"/api/device-templates/{r2['id']}", {
            "config": {**r2["config"], "request_timeout_sec": 2}, "row_version": r2["row_version"],
        })
        assert updated.status_code == 200, updated.text
        r2 = _release(qa, updated.json()).json()
        assert admin.get(f"/api/device-templates/{r1['id']}").json()["state"] == "retired"
        stations = {row["station_id"]: row for row in r2["stations"]}
        assert stations[station]["revision"] == 1 and stations[station]["outdated"]
        assert admin.get(f"/api/stations/{station}/adapter").json()["template"]["outdated"]
        options = admin.get(f"/api/stations/{station}/adapter/templates").json()
        assert [row["revision"] for row in options if row["code"] == "TPL-OVEN-VD"] == [2]

        # 换到 r2：连接参数沿用工位上的那份；驱动没变，只欠只读级
        switched = _patch(admin, station, template_id=r2["id"])
        assert switched.status_code == 200, switched.text
        assert switched.json()["template"]["revision"] == 2 and not switched.json()["template"]["outdated"]
        assert switched.json()["acceptance"]["required"] == "readonly"
        detail = admin.get(f"/api/stations/{station}/adapter").json()
        assert detail["config"]["request_timeout_sec"] == 2 and detail["config"]["transport"]["port"] == port

        # 手工改完整配置：和模板对不上了，不再按模板管理
        manual = _patch(admin, station, config={**detail["config"], "request_timeout_sec": 3})
        assert manual.status_code == 200 and manual.json()["template"] is None


def test_released_template_content_is_frozen_in_the_database(admin, qa, operator, db):
    template = admin.post("/api/device-templates", _oven_template("TPL-FROZEN")).json()
    released = _release(qa, template).json()
    with pytest.raises(Exception, match="内容冻结"):
        db.execute(text("UPDATE device_templates SET config = '{}' WHERE id = :id"), {"id": released["id"]})
    db.rollback()
    # 审批证据同样冻结；状态只能已发布 → 退役
    with pytest.raises(Exception, match="内容冻结"):
        db.execute(text("UPDATE device_templates SET released_by = 'someone' WHERE id = :id"), {"id": released["id"]})
    db.rollback()
    with pytest.raises(Exception, match="状态只能从已发布改为退役"):
        db.execute(text("UPDATE device_templates SET state = 'draft' WHERE id = :id"), {"id": released["id"]})
    db.rollback()
    with pytest.raises(Exception, match="不许删除"):
        db.execute(text("DELETE FROM device_templates WHERE id = :id"), {"id": released["id"]})
    db.rollback()
    # 退役只改状态，可以
    retired = operator.post(f"/api/device-templates/{released['id']}/retire", {"row_version": released["row_version"]})
    assert retired.status_code == 403, "退役要发布权限"
    retired = qa.post(f"/api/device-templates/{released['id']}/retire", {"row_version": released["row_version"]})
    assert retired.status_code == 200 and retired.json()["state"] == "retired"
    with pytest.raises(Exception, match="状态只能从已发布改为退役"):
        db.execute(text("UPDATE device_templates SET state = 'released' WHERE id = :id"), {"id": released["id"]})
    db.rollback()
    # 模板里是点表、命令与内部地址示例：只给维护工位的人与发布模板的人（审了才能发布）看
    for path in ("/api/device-templates", f"/api/device-templates/{released['id']}",
                 f"/api/device-templates/{released['id']}/export"):
        assert operator.get(path).status_code == 403, path
        assert qa.get(path).status_code == 200, path


@pytest.fixture()
def engineer(client):
    from conftest import Session

    return Session(client, "engineer")


def test_whoever_edited_a_draft_cannot_release_it(admin, engineer, qa):
    """起草人与改过草稿的人都不能发布它：否则 B 改了 A 的草稿再自己发布，职责分离就空了。"""
    draft = admin.post("/api/device-templates", _oven_template("TPL-SOD")).json()
    edited = engineer.patch(f"/api/device-templates/{draft['id']}", {"note": "工程师改过点表说明",
                                                                      "row_version": draft["row_version"]})
    assert edited.status_code == 200, edited.text
    for session in (engineer, admin):
        refused = _release(session, edited.json())
        assert refused.status_code == 403 and refused.json()["detail"]["code"] == "self_approval", refused.text
    released = _release(qa, edited.json())
    assert released.status_code == 200, released.text


def test_passwords_written_into_urls_count_as_secrets(admin, station):
    leaked = _oven_template("TPL-URL-SECRET")
    leaked["config"]["report_url"] = "https://svc:hunter2@10.20.1.5/api/report"
    response = admin.post("/api/device-templates", leaked)
    assert response.status_code == 422 and response.json()["detail"]["code"] == "inline_adapter_secret_forbidden"
    saved = _patch(admin, station, kind="real", driver="rest_map_v1", protocol="REST 接口映射",
                   config={"base_url": "https://fleet:hunter2@127.0.0.1:8443/api"})
    assert saved.status_code == 422 and saved.json()["detail"]["code"] == "inline_adapter_secret_forbidden", saved.text
    # 只写用户名、不带口令的地址照常可以（口令放 credential_ref 指向的文件）
    checked = admin.post(f"/api/stations/{station}/adapter/check", {
        "driver": "rest_map_v1", "config": {"base_url": "https://fleet@127.0.0.1:8443/api"},
    }).json()
    assert not any("口令" in problem for problem in checked["problems"])


def test_stations_fill_connection_parameters_only(admin, qa, station):
    """工位只填驱动登记的连接参数：映射（点表、命令、状态码）归模板，改映射要改模板、另一个人发布。"""
    orphan = _patch(admin, station, template_connection={"transport": {"kind": "tcp", "host": "127.0.0.1", "port": 4001}})
    assert orphan.status_code == 422 and orphan.json()["detail"]["code"] == "template_required"
    template = _release(qa, admin.post("/api/device-templates", _oven_template("TPL-CONN")).json()).json()
    remapped = _patch(admin, station, template_id=template["id"], template_connection={
        "transport": {"kind": "tcp", "host": "127.0.0.1", "port": 4001}, "status": {"send": "STATE?"},
    })
    assert remapped.status_code == 422, remapped.text
    detail = remapped.json()["detail"]
    assert detail["code"] == "template_connection_invalid" and any("status" in item for item in detail["problems"])


def test_connection_parameters_are_the_drivers_connection_keys():
    from app.services.template_service import connection_problems

    plain = connection_problems("line_command_v1", {}, {"transport": {}, "status": {}})
    assert len(plain) == 1 and plain[0].startswith("status 不是"), plain
    assert connection_problems("composite_v1", {}, {}) == ["驱动 composite_v1 没有登记"]


def test_export_and_import_keep_the_digest(admin, qa):
    template = admin.post("/api/device-templates", _oven_template("TPL-EXPORT")).json()
    released = _release(qa, template).json()
    exported = admin.get(f"/api/device-templates/{released['id']}/export")
    assert exported.status_code == 200 and "attachment" in exported.headers["content-disposition"]
    document = exported.json()
    assert document["format"] == "ilcs-device-template/1" and document["digest"] == released["digest"]

    imported = admin.post("/api/device-templates/import", {"filename": "TPL-EXPORT-r1.json", "document": document})
    assert imported.status_code == 201, imported.text
    body = imported.json()
    assert body["state"] == "draft" and body["revision"] == 2 and body["source"]["kind"] == "import"
    assert body["digest"] == document["digest"], "内容没变，摘要不变"

    tampered = {**document, "code": "TPL-EXPORT-2", "config": {**document["config"], "request_timeout_sec": 9}}
    rejected = admin.post("/api/device-templates/import", {"filename": "x.json", "document": tampered})
    assert rejected.status_code == 422 and rejected.json()["detail"]["code"] == "template_digest_mismatch"
    wrong = admin.post("/api/device-templates/import", {"filename": "x.json", "document": {"code": "X"}})
    assert wrong.status_code == 422 and wrong.json()["detail"]["code"] == "template_file_invalid"


def test_template_rejects_secrets_and_unknown_drivers(admin):
    leaked = _oven_template("TPL-SECRET")
    leaked["connection"]["password"] = "must-not-persist"
    assert admin.post("/api/device-templates", leaked).status_code == 422
    unknown = {**_oven_template("TPL-UNKNOWN"), "driver": "vendor_x_v9"}
    response = admin.post("/api/device-templates", unknown)
    assert response.status_code == 422 and response.json()["detail"]["code"] == "template_driver_invalid"
    incomplete = _oven_template("TPL-INCOMPLETE")
    del incomplete["config"]["status"]
    draft = admin.post("/api/device-templates", incomplete).json()
    assert not draft["check"]["ok"] and any("status" in problem for problem in draft["check"]["problems"])
