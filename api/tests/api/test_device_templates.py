"""设备接入模板：一类设备怎么接存成要发布的模板；工位 = 模板的某一版 + 自己的连接参数。

驱动在 adapters/catalog 里声明自己的配置项：界面据此出表单，保存前据此校验（配错了当场说清楚）。
模板发布要另一个人签名，发布后内容冻结；升级不自动推给工位，由人逐台切换、重新验收。
这里用 HTTPS 网关（http_json_v1）的模板：接口映射归模板，网关地址、证书、设备编号由工位填。
"""
import copy

import pytest
from sqlalchemy import text

LIMITS = {"cap.vacuum_dry": {"temp": [60, 180], "vacuum": [0.1, 5]}}
GATEWAY = {
    "paths": {"health": "/health", "submit": "/commands", "query": "/commands/{command_id}",
              "hold": "/commands/{command_id}/hold", "abort": "/commands/{command_id}/abort"},
    "idempotency_header": "Idempotency-Key",
}
RETIRED = {"sql_table_v1", "composite_v1", "mt_sics_v1", "modbus_map_v1", "opcua_map_v1", "rest_map_v1",
           "line_command_v1", "modbus_tcp_v1", "opcua_v1"}


def _gateway_template(code: str = "TPL-GW-VD") -> dict:
    return {
        "code": code, "name": "真空干燥箱网关（HTTPS JSON）", "driver": "http_json_v1", "protocol": "HTTPS JSON",
        "version": "gateway-sdk-1.2", "model": "VAC-OVEN-80", "vendor": "示例厂家",
        "config": {**copy.deepcopy(GATEWAY), "request_timeout_sec": 2, "connect_timeout_sec": 1,
                   "heartbeat_mode": "probe", "probe_interval_sec": 0.5},
        "connection": {"base_url": "https://oven-01.lab.internal/api/v1",
                       "ca_file": "/run/secrets/ilcs/gateway/oven-01.crt", "expected_device_id": "<设备编号>"},
        "supports": {"hold": True, "abort": True, "query": True, "dedup": True},
        "acceptance": {"capability": "cap.vacuum_dry", "params": {"temp": 120, "vacuum": 1}},
        "note": "网关 SDK 1.2 版",
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
    assert {row["key"] for row in drivers} == set(REAL_IMPLEMENTATIONS) == {"http_json_v1", "sila2_v1"}
    assert not RETIRED & {row["key"] for row in drivers}, "删掉的、移到驱动宿主的驱动不再登记"
    gateway = next(row for row in drivers if row["key"] == "http_json_v1")
    required = {f["name"] for f in gateway["fields"] if f["required"]}
    assert required == {"base_url"} and {"base_url", "ca_file", "expected_device_id"} <= set(gateway["connection_keys"])
    # 表单用的嵌套结构说明
    paths = next(item for item in gateway["fields"] if item["name"] == "paths")
    assert {item["name"] for item in paths["fields"]} == {"health", "submit", "query", "hold", "abort"}
    sila = next(row for row in drivers if row["key"] == "sila2_v1")
    assert {"host", "port"} <= {f["name"] for f in sila["fields"] if f["required"]}
    plain = {row["key"]: row for row in admin.get("/api/drivers").json()}
    assert all(row["capability_examples"] == {} for row in plain.values()), "按 ILCS 契约接的驱动没有能力映射"


def test_invalid_config_is_rejected_when_saving(admin, station):
    checked = admin.post(f"/api/stations/{station}/adapter/check", {
        "driver": "http_json_v1", "config": {"pahts": {"health": "/health"}},
    }).json()
    assert not checked["ok"] and any("网关地址" in problem for problem in checked["problems"]), checked
    assert any("pahts" in warning for warning in checked["warnings"]), "拼错的键只提醒，驱动会忽略它"
    tasks = admin.post(f"/api/stations/{station}/adapter/check", {
        "driver": "sila2_v1", "config": {"host": "127.0.0.1", "port": 50201, "insecure": True, "tasks": "no"},
    }).json()
    assert not tasks["ok"] and any("tasks" in problem for problem in tasks["problems"]), tasks

    saved = _patch(admin, station, kind="real", driver="http_json_v1", protocol="HTTPS JSON",
                   config={**copy.deepcopy(GATEWAY), "base_url": "https://10.99.0.1/api/v1"})
    assert saved.status_code == 422, saved.text
    detail = saved.json()["detail"]
    assert detail["code"] == "adapter_config_invalid" and any("白名单" in item for item in detail["problems"])
    bad_path = _patch(admin, station, kind="real", driver="http_json_v1", protocol="HTTPS JSON",
                      config={**copy.deepcopy(GATEWAY), "base_url": "https://127.0.0.1:8443/api/v1",
                              "paths": {**GATEWAY["paths"], "submit": "https://elsewhere.example/commands"}})
    assert bad_path.status_code == 422 and "相对路径" in bad_path.json()["detail"]["message"]
    retired = _patch(admin, station, kind="real", driver="line_command_v1", protocol="串口 / TCP 命令", config={})
    assert retired.status_code == 422, "移到驱动宿主的驱动不能再选"
    # 停用、改说明不因为配置检查被挡住
    assert _patch(admin, station, note="待接线").status_code == 200


def test_template_lifecycle_apply_and_upgrade(admin, qa, station, reset_runtime, tmp_path, monkeypatch):
    from app.core.config import settings
    from sim_harness import gateway_sim

    monkeypatch.setattr(settings, "adapter_credential_root", str(tmp_path))
    created = admin.post("/api/device-templates", _gateway_template())
    assert created.status_code == 201, created.text
    draft = created.json()
    assert draft["state"] == "draft" and draft["revision"] == 1 and draft["check"]["ok"], draft["check"]
    assert "base_url" in draft["connection_keys"]

    self_release = _release(admin, draft)
    assert self_release.status_code == 403 and self_release.json()["detail"]["code"] == "self_approval"
    released = _release(qa, draft)
    assert released.status_code == 200, released.text
    r1 = released.json()
    assert r1["state"] == "released" and r1["digest"].startswith("sha256:")
    assert admin.patch(f"/api/device-templates/{r1['id']}", {"note": "改一下", "row_version": r1["row_version"]}).status_code == 409

    with gateway_sim(tmp_path, task_seconds=0.3) as (_, _, port):
        connection = {"base_url": f"https://localhost:{port}/api/v1", "ca_file": str(tmp_path / "SIM-GW-T.crt"),
                      "expected_device_id": "SIM-GW-T"}
        applied = _patch(admin, station, template_id=r1["id"], template_connection=connection,
                         credential_ref=f"file://{tmp_path / 'SIM-GW-T.token'}")
        assert applied.status_code == 200, applied.text
        adapter = applied.json()
        assert adapter["kind"] == "real" and adapter["driver"] == "http_json_v1"
        assert adapter["template"]["code"] == "TPL-GW-VD" and adapter["template"]["revision"] == 1
        assert adapter["acceptance"]["required"] == "physical", "第一次接成真实设备"
        detail = admin.get(f"/api/stations/{station}/adapter").json()
        assert detail["config"]["base_url"] == connection["base_url"] and detail["config"]["paths"] == GATEWAY["paths"]
        assert detail["template_connection"] == connection

        _station_pass(station)
        runs = admin.get(f"/api/stations/{station}/adapter/acceptance").json()
        assert runs["gate"]["required"] == "" and runs["runs"][0]["template"]["revision"] == 1, runs
        assert runs["defaults"] == {"capability": "cap.vacuum_dry", "params": {"temp": 120, "vacuum": 1},
                                    "source": "template"}, "申请验收的缺省取模板的验收缺省"
        report = admin.get(f"/api/acceptance-runs/{runs['runs'][0]['id']}").json()["report_md"]
        assert "设备接入模板 TPL-GW-VD r1" in report

        # 新修订发布：旧版退役，但不自动推给工位——列出来，由人逐台切换
        revised = admin.post(f"/api/device-templates/{r1['id']}/revise")
        assert revised.status_code == 201, revised.text
        r2 = revised.json()
        assert r2["revision"] == 2 and r2["state"] == "draft"
        updated = admin.patch(f"/api/device-templates/{r2['id']}", {
            "config": {**r2["config"], "request_timeout_sec": 3}, "row_version": r2["row_version"],
        })
        assert updated.status_code == 200, updated.text
        r2 = _release(qa, updated.json()).json()
        assert admin.get(f"/api/device-templates/{r1['id']}").json()["state"] == "retired"
        stations = {row["station_id"]: row for row in r2["stations"]}
        assert stations[station]["revision"] == 1 and stations[station]["outdated"]
        assert admin.get(f"/api/stations/{station}/adapter").json()["template"]["outdated"]
        options = admin.get(f"/api/stations/{station}/adapter/templates").json()
        assert [row["revision"] for row in options if row["code"] == "TPL-GW-VD"] == [2]

        # 换到 r2：连接参数沿用工位上的那份；驱动没变，只欠只读级
        switched = _patch(admin, station, template_id=r2["id"])
        assert switched.status_code == 200, switched.text
        assert switched.json()["template"]["revision"] == 2 and not switched.json()["template"]["outdated"]
        assert switched.json()["acceptance"]["required"] == "readonly"
        detail = admin.get(f"/api/stations/{station}/adapter").json()
        assert detail["config"]["request_timeout_sec"] == 3 and detail["config"]["base_url"] == connection["base_url"]

        # 手工改完整配置：和模板对不上了，不再按模板管理
        manual = _patch(admin, station, config={**detail["config"], "request_timeout_sec": 4})
        assert manual.status_code == 200 and manual.json()["template"] is None


def test_released_template_content_is_frozen_in_the_database(admin, qa, operator, db):
    template = admin.post("/api/device-templates", _gateway_template("TPL-FROZEN")).json()
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
    # 模板里是接口映射与内部地址示例：只给维护工位的人与发布模板的人（审了才能发布）看
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
    draft = admin.post("/api/device-templates", _gateway_template("TPL-SOD")).json()
    edited = engineer.patch(f"/api/device-templates/{draft['id']}", {"note": "工程师改过接口说明",
                                                                      "row_version": draft["row_version"]})
    assert edited.status_code == 200, edited.text
    for session in (engineer, admin):
        refused = _release(session, edited.json())
        assert refused.status_code == 403 and refused.json()["detail"]["code"] == "self_approval", refused.text
    released = _release(qa, edited.json())
    assert released.status_code == 200, released.text


def test_passwords_written_into_urls_count_as_secrets(admin, station):
    leaked = _gateway_template("TPL-URL-SECRET")
    leaked["config"]["report_url"] = "https://svc:hunter2@10.20.1.5/api/report"
    response = admin.post("/api/device-templates", leaked)
    assert response.status_code == 422 and response.json()["detail"]["code"] == "inline_adapter_secret_forbidden"
    saved = _patch(admin, station, kind="real", driver="http_json_v1", protocol="HTTPS JSON",
                   config={"base_url": "https://gw:hunter2@127.0.0.1:8443/api/v1"})
    assert saved.status_code == 422 and saved.json()["detail"]["code"] == "inline_adapter_secret_forbidden", saved.text
    # 只写用户名、不带口令的地址照常可以（口令放 credential_ref 指向的文件）
    checked = admin.post(f"/api/stations/{station}/adapter/check", {
        "driver": "http_json_v1", "config": {"base_url": "https://gw@127.0.0.1:8443/api/v1"},
    }).json()
    assert not any("口令" in problem for problem in checked["problems"])


def test_stations_fill_connection_parameters_only(admin, qa, station):
    """工位只填驱动登记的连接参数：映射（接口路径、幂等头）归模板，改映射要改模板、另一个人发布。"""
    orphan = _patch(admin, station, template_connection={"base_url": "https://127.0.0.1:8443/api/v1"})
    assert orphan.status_code == 422 and orphan.json()["detail"]["code"] == "template_required"
    template = _release(qa, admin.post("/api/device-templates", _gateway_template("TPL-CONN")).json()).json()
    remapped = _patch(admin, station, template_id=template["id"], template_connection={
        "base_url": "https://127.0.0.1:8443/api/v1", "paths": {"submit": "/jobs"},
    })
    assert remapped.status_code == 422, remapped.text
    detail = remapped.json()["detail"]
    assert detail["code"] == "template_connection_invalid" and any("paths" in item for item in detail["problems"])


def test_connection_parameters_are_the_drivers_connection_keys():
    from app.services.template_service import connection_problems

    plain = connection_problems("http_json_v1", {}, {"base_url": "https://gw/api/v1", "paths": {}})
    assert len(plain) == 1 and plain[0].startswith("paths 不是"), plain
    assert connection_problems("composite_v1", {}, {}) == ["驱动 composite_v1 没有登记"]
    assert connection_problems("line_command_v1", {}, {}) == ["驱动 line_command_v1 没有登记"]


def test_export_and_import_keep_the_digest(admin, qa):
    template = admin.post("/api/device-templates", _gateway_template("TPL-EXPORT")).json()
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
    leaked = _gateway_template("TPL-SECRET")
    leaked["connection"]["password"] = "must-not-persist"
    assert admin.post("/api/device-templates", leaked).status_code == 422
    for driver in ("vendor_x_v9", "line_command_v1"):
        unknown = {**_gateway_template(f"TPL-UNKNOWN-{driver}"), "driver": driver}
        response = admin.post("/api/device-templates", unknown)
        assert response.status_code == 422 and response.json()["detail"]["code"] == "template_driver_invalid", driver
    incomplete = _gateway_template("TPL-INCOMPLETE")
    incomplete["config"]["paths"] = "/commands"
    draft = admin.post("/api/device-templates", incomplete).json()
    assert not draft["check"]["ok"] and any("paths" in problem for problem in draft["check"]["problems"])
