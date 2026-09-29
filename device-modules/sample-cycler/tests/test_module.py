"""设备模块自测：对模拟接口跑 ILCS 的接入验收清单（含故障项目），再测几条驱动自己的判断。CI 里必须全过。

    pytest device-modules/sample-cycler/tests
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ilcs_gateway import Job, Rejected, serve
from ilcs_gateway.testing import acceptance

from driver.device import CAPABILITY, PARAMETERS, Instrument
from simulator.fake_sdk import FakeVendorSdk

DEVICE_ID = FakeVendorSdk().serial
# 验收用的参数取设备允许范围的中点；越界的取第一个参数上限的十倍
PARAMS = {name: round((low + high) / 2, 6) for name, (low, high) in PARAMETERS.items()}
FIRST = next(iter(PARAMETERS))
OUT_OF_RANGE = {**PARAMS, FIRST: PARAMETERS[FIRST][1] * 10 + 1}


@pytest.fixture()
def gateway(tmp_path: Path):
    secrets = tmp_path / "secrets"
    sdk = FakeVendorSdk(DEVICE_ID, run_seconds=0.5)
    server = serve(Instrument(sdk), device_id=DEVICE_ID, state_dir=tmp_path / "state", address="127.0.0.1", port=0,
                   token_file=secrets / f"{DEVICE_ID}.token", cert=secrets / f"{DEVICE_ID}.crt",
                   key=secrets / f"{DEVICE_ID}.key", host_name="localhost")
    try:
        yield server, sdk, secrets
    finally:
        server.stop()


def test_module_passes_the_ilcs_acceptance_checklist(gateway, tmp_path):
    server, sdk, secrets = gateway
    report = acceptance(
        f"https://localhost:{server.port}/api/v1", token_file=secrets / f"{DEVICE_ID}.token",
        ca_file=secrets / f"{DEVICE_ID}.crt", capability=CAPABILITY, params=PARAMS, expected_device_id=DEVICE_ID,
        state_root=tmp_path / "ilcs",
    )
    states = {check.key: check.state for check in report.checks}
    assert report.ok, report.markdown()
    assert all(state == "pass" for state in states.values()), states
    assert report.simulator and report.identity["reported_model"] == FakeVendorSdk().model
    assert report.identity["methods"], "方法目录由设备自报"


def test_gateway_restart_still_answers_by_command_id(gateway, tmp_path):
    """网关进程重启：台账在盘上，按原指令号照样查得到；同一指令号再投不会让设备再动一次。"""
    server, sdk, secrets = gateway
    first = server.gateway.submit({"command_id": "CMD-1", "capability": CAPABILITY, "params": PARAMS})
    assert first["state"] == "running" and sdk.faults.motions == 1
    server.stop()
    again = serve(Instrument(sdk), device_id=DEVICE_ID, state_dir=tmp_path / "state", address="127.0.0.1", port=0,
                  token_file=secrets / f"{DEVICE_ID}.token", cert=secrets / f"{DEVICE_ID}.crt",
                  key=secrets / f"{DEVICE_ID}.key", host_name="localhost")
    try:
        assert again.gateway.query("CMD-1")["state"] == "running"
        replay = again.gateway.submit({"command_id": "CMD-1", "capability": CAPABILITY, "params": PARAMS})
        assert replay["state"] == "running" and sdk.faults.motions == 1, "重投回放台账，不再动设备"
        assert again.gateway.query("CMD-UNKNOWN") is None
    finally:
        again.stop()


@pytest.mark.parametrize(("capability", "params", "program", "kind"), [
    ("cap.not_this_device", PARAMS, "", "unsupported"),
    (CAPABILITY, OUT_OF_RANGE, "", "invalid"),
    (CAPABILITY, PARAMS, "UNKNOWN-PROGRAM", "invalid"),
])
def test_driver_rejects_what_the_device_cannot_do(capability, params, program, kind):
    device = Instrument(FakeVendorSdk(DEVICE_ID))
    with pytest.raises(Rejected) as caught:
        device.start(Job(command_id="CMD-X", capability=capability, params=params, method={"program": program}))
    assert caught.value.kind == kind


def test_full_channels_and_estop_are_explicit_rejections():
    sdk = FakeVendorSdk(DEVICE_ID, channels=1, run_seconds=60)
    device = Instrument(sdk)
    device.start(Job(command_id="CMD-A", capability=CAPABILITY, params=PARAMS))
    with pytest.raises(Rejected) as busy:
        device.start(Job(command_id="CMD-B", capability=CAPABILITY, params=PARAMS))
    assert busy.value.kind == "busy"
    sdk.faults.set_fault("interlock")
    with pytest.raises(Rejected) as estop:
        device.start(Job(command_id="CMD-C", capability=CAPABILITY, params=PARAMS))
    assert estop.value.kind == "interlocked" and sdk.faults.motions == 1
