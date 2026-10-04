"""设备模块进 ILCS 的整条路：导入 profile.json → 另一个人发布 → 工位套用 → 执行器跑接入验收。

模块（样板 sample-cycler）在设备仓库里：网关（ilcs_gateway + 样板的驱动与假厂家 SDK）在本进程里起，走真实的 HTTPS 与令牌；
ILCS 侧不打桩。模块自己的测试、模块脚手架与网关 SDK 的测试都在设备仓库里（`./run-tests.sh`）。
"""
import json

import pytest

from sim_harness import DEVICES, needs_devices

pytestmark = needs_devices
MODULE = DEVICES / "gateway" / "sample-cycler"
LIMITS = {"cap.test": {"rate": [0.01, 10], "vmax": [2.0, 5.0]}}


def test_device_module_profiles_import_as_templates():
    """设备仓库里网关模块的 profile.json 都能按 ILCS 的规则导入成接入模板：摘要对得上、核对通过。ILCS 改了模板规则时，
    这里先发现交付物导不进来。映射模块（如 scpi-cell-meter）的 profile-*.json 是驱动宿主的设备配置，不是模板。"""
    from app.services.template_service import FORMAT, template_check, template_digest

    profiles = sorted((DEVICES / "gateway").glob("*/profile.json"))
    assert len(profiles) >= 7, profiles
    for path in profiles:
        profile = json.loads(path.read_text(encoding="utf-8"))
        assert profile["format"] == FORMAT, path.parent.name
        assert profile["digest"] == template_digest(profile), f"{path.parent.name} 的 profile.json 改过之后要重算摘要"
        assert template_check(profile)["ok"], (path.parent.name, template_check(profile))


@pytest.fixture()
def module_gateway(tmp_path, monkeypatch):
    from app.core.config import settings

    for path in (MODULE, DEVICES / "gateway"):
        monkeypatch.syspath_prepend(str(path))
    from ilcs_gateway import serve
    from driver.device import Instrument
    from simulator.fake_sdk import FakeVendorSdk

    secrets = tmp_path / "secrets"
    monkeypatch.setattr(settings, "adapter_credential_root", str(secrets))
    server = serve(Instrument(FakeVendorSdk("SIM-CYC-M1", run_seconds=0.5)), device_id="SIM-CYC-M1",
                   state_dir=tmp_path / "state", address="127.0.0.1", port=0, token_file=secrets / "SIM-CYC-M1.token",
                   cert=secrets / "SIM-CYC-M1.crt", key=secrets / "SIM-CYC-M1.key", host_name="localhost")
    try:
        yield server, secrets
    finally:
        server.stop()


@pytest.fixture()
def cycler_station(admin):
    from app.core.db import SessionLocal
    from app.models import Station

    with SessionLocal() as db:
        exists = db.get(Station, "ST-94") is not None
    if not exists:
        created = admin.post("/api/stations", {
            "id": "ST-94", "name": "设备模块联调站", "protocol": "sim", "limits": LIMITS,
            "adapter_kind": "simulation", "adapter_driver": "simulation",
            "signature_id": admin.sign("工程变更批准", target="ST-94"),
        })
        assert created.status_code == 201, created.text
    else:
        assert admin.post("/api/stations/ST-94/retire", {"retired": False}).status_code == 200
    yield "ST-94"
    _patch(admin, "ST-94", kind="simulation", driver="simulation", protocol="sim", config={}, credential_ref="",
           template_id="")
    assert admin.post("/api/stations/ST-94/retire", {"retired": True}).status_code == 200


def _patch(admin, station_id: str, **changes):
    adapter = admin.get(f"/api/stations/{station_id}/adapter").json()
    return admin.patch(f"/api/stations/{station_id}/adapter", {
        **changes, "row_version": adapter["row_version"],
        "signature_id": admin.sign("设备集成配置变更批准", target=station_id, object_version=adapter["row_version"]),
    })


def _station_pass(station_id: str) -> None:
    from app.core.db import SessionLocal
    from app.services.execution_service import ExecutorLoop

    with SessionLocal() as db:
        ExecutorLoop(db).station_pass(station_id)


def test_module_profile_goes_live_through_template_and_acceptance(admin, qa, module_gateway, cycler_station,
                                                                  reset_runtime):
    server, secrets = module_gateway
    profile = json.loads((MODULE / "profile.json").read_text(encoding="utf-8"))
    imported = admin.post("/api/device-templates/import", {"filename": "profile.json", "document": profile})
    assert imported.status_code == 201, imported.text
    draft = imported.json()
    assert draft["state"] == "draft" and draft["digest"] == profile["digest"] and draft["check"]["ok"], draft["check"]
    released = qa.post(f"/api/device-templates/{draft['id']}/release", {
        "row_version": draft["row_version"],
        "signature_id": qa.sign("发布设备接入模板", target=draft["id"], object_version=draft["row_version"]),
    })
    assert released.status_code == 200, released.text

    applied = _patch(
        admin, cycler_station, template_id=draft["id"],
        template_connection={"base_url": f"https://localhost:{server.port}/api/v1",
                             "ca_file": str(secrets / "SIM-CYC-M1.crt"), "expected_device_id": "SIM-CYC-M1"},
        credential_ref=f"file://{secrets / 'SIM-CYC-M1.token'}",
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["driver"] == "http_json_v1" and applied.json()["acceptance"]["required"] == "physical"

    _station_pass(cycler_station)  # 执行器：自动只读级验收（模拟网关自报为模拟器，只读级就放行）
    listed = admin.get(f"/api/stations/{cycler_station}/adapter/acceptance").json()
    assert listed["gate"]["required"] == "" and listed["runs"][0]["ok"], listed
    assert listed["runs"][0]["template"]["code"] == "TPL-SAMPLE-CYCLER"

    adapter = admin.get(f"/api/stations/{cycler_station}/adapter").json()
    requested = admin.post(f"/api/stations/{cycler_station}/adapter/acceptance", {
        "level": "physical", "faults": True, "approval": "联调环境，模拟网关",
        "signature_id": admin.sign("批准设备接入验收", target=cycler_station, object_version=adapter["config_version"]),
    })
    assert requested.status_code == 201, requested.text
    _station_pass(cycler_station)
    run = admin.get(f"/api/acceptance-runs/{requested.json()['id']}").json()
    states = {check["key"]: check["state"] for check in run["checks"]}
    assert run["state"] == "done" and run["ok"], run["report_md"]
    assert all(state == "pass" for state in states.values()), states
