"""设备模块进 ILCS 的整条路：模块自测 → 导入 profile.json → 另一个人发布 → 工位套用 → 执行器跑接入验收。

模块的网关（sdk/ilcs_gateway + 样板的驱动与假厂家 SDK）在本进程里起，走真实的 HTTPS 与令牌；ILCS 侧不打桩。
"""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[3]
MODULE = ROOT / "device-modules" / "sample-cycler"
LIMITS = {"cap.test": {"rate": [0.01, 10], "vmax": [2.0, 5.0]}}


def _pytest(target: Path, **env) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(target)], cwd=ROOT,
        capture_output=True, text=True, timeout=300, env={**os.environ, **env},
    )


def test_sample_module_passes_its_own_tests():
    result = _pytest(MODULE / "tests")
    assert result.returncode == 0, result.stdout + result.stderr


def test_scaffolded_module_is_green_out_of_the_box(tmp_path):
    created = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "new-device-module.py"), "acme-vd80", "--title", "ACME 真空干燥箱",
         "--model", "VD-80", "--vendor", "ACME", "--capability", "cap.vacuum_dry", "--param", "temp=60:180",
         "--param", "vacuum=0.1:5", "--program", "VD-120=120℃干燥", "--output", str(tmp_path)],
        cwd=ROOT, capture_output=True, text=True, timeout=120,
    )
    assert created.returncode == 0, created.stdout + created.stderr
    module = tmp_path / "acme-vd80"
    profile = json.loads((module / "profile.json").read_text(encoding="utf-8"))
    assert profile["code"] == "TPL-ACME-VD80" and profile["acceptance"]["capability"] == "cap.vacuum_dry"
    result = _pytest(module / "tests", ILCS_REPO=str(ROOT))
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.fixture()
def module_gateway(tmp_path, monkeypatch):
    from app.core.config import settings

    for path in (MODULE, ROOT / "sdk"):
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


@pytest.fixture()
def sdk(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "sdk"))
    import ilcs_gateway

    return ilcs_gateway


class _NeverAnswers:
    """启动命令发出去就没了下文（厂家 SDK 超时）、设备侧也找不到作业：不知道它开没开始。"""

    def __init__(self, sdk):
        self.sdk = sdk
        self.aborted: list[str] = []

    def identity(self):
        return {"device_id": "SIM-NA", "model": "NA", "simulator": True}

    def start(self, job):
        raise TimeoutError("厂家 SDK 超时")

    def status(self, job):
        return self.sdk.Status("running")

    def abort(self, job):
        self.aborted.append(job.handle)

    def lookup(self, job):
        return None

    def hold(self, job):
        raise self.sdk.Rejected("unsupported", "不支持")

    def resume(self, job):
        raise self.sdk.Rejected("unsupported", "不支持")

    def fault_target(self):
        return None


def test_gateway_sdk_security_defaults(sdk, tmp_path):
    """HTTPS 必须带令牌；明文只给本机联调、缺省只听 127.0.0.1；令牌、私钥、台账创建时就是属主只读。"""
    import stat

    from ilcs_gateway.server import serve

    device = _NeverAnswers(sdk)
    secrets = tmp_path / "secrets"
    with pytest.raises(SystemExit, match="访问令牌"):
        serve(device, device_id="SIM-NA", state_dir=tmp_path / "state", port=0,
              cert=secrets / "SIM-NA.crt", key=secrets / "SIM-NA.key")
    with pytest.raises(SystemExit, match="只能监听本机地址"):
        serve(device, device_id="SIM-NA", state_dir=tmp_path / "state", port=0, insecure=True, address="0.0.0.0")
    plain = serve(device, device_id="SIM-NA", state_dir=tmp_path / "state", port=0, insecure=True)
    try:
        assert plain.address == "127.0.0.1"
    finally:
        plain.stop()
    server = serve(device, device_id="SIM-NA", state_dir=tmp_path / "state", port=0, address="127.0.0.1",
                   token_file=secrets / "SIM-NA.token", cert=secrets / "SIM-NA.crt", key=secrets / "SIM-NA.key")
    try:
        server.gateway.submit({"command_id": "CMD-1", "capability": "cap.test", "params": {}})
        for path in (secrets / "SIM-NA.token", secrets / "SIM-NA.key", tmp_path / "state" / "SIM-NA.json"):
            assert stat.S_IMODE(path.stat().st_mode) == 0o600, path
        assert not list(secrets.glob(".*.tmp")), "不留临时文件"
    finally:
        server.stop()


def test_gateway_sdk_does_not_claim_to_have_stopped_a_job_it_cannot_find(sdk, tmp_path):
    """启动没拿到应答、设备侧也还没找到的作业：终止回结果未知，原作业不记成已停——不知道它开没开始，也就没法确认停住了。"""
    from ilcs_gateway.gateway import Gateway
    from ilcs_gateway.ledger import Ledger

    device = _NeverAnswers(sdk)
    gateway = Gateway(device, Ledger(tmp_path / "ledger.json"))
    started = gateway.submit({"command_id": "CMD-1", "capability": "cap.test", "params": {}})
    assert started["state"] == "unknown"
    stopped = gateway.control("abort", "CMD-ABORT", {"target_command_id": "CMD-1"})
    assert stopped["state"] == "unknown" and "现场核查" in stopped["error"]
    assert not device.aborted, "拿不到作业号：不去乱停"
    assert gateway.query("CMD-1")["state"] == "unknown", "原作业照样是结果未知，不记成已停"
    assert gateway.ledger.find("CMD-ABORT") is None, "不落控制记录：同一终止指令号再来会重新找一次"
