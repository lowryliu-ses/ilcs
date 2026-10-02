"""设备模块自测：对模拟站跑 ILCS 的接入验收清单（含故障项目），三项能力各跑一遍；网关重启后按指令号查回。

    pytest devices/gateway/balance-dosing/tests
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ilcs_gateway import serve
from ilcs_gateway.testing import acceptance

from simulator import simulated_station

SUPPORTS = {"hold": False, "abort": True, "query": True, "dedup": True}


@pytest.fixture()
def gateway(tmp_path: Path):
    station, simulation = simulated_station(state_dir=tmp_path / "state", settle_sec=0.02, dose_seconds=0.4,
                                            step_seconds=0.0001)
    device_id = station.config.device_id
    secrets = tmp_path / "secrets"
    server = serve(station, device_id=device_id, state_dir=tmp_path / "state", address="127.0.0.1", port=0,
                   token_file=secrets / f"{device_id}.token", cert=secrets / f"{device_id}.crt",
                   key=secrets / f"{device_id}.key", host_name="localhost")
    try:
        yield server, station, simulation, secrets
    finally:
        server.stop()
        simulation.stop()


@pytest.mark.parametrize(("capability", "params"), [
    ("cap.ely.dose_liquid", {"mass": 1.0}),
    ("cap.ely.dose_solid", {"mass": 0.05}),
    ("cap.weigh", {}),
])
def test_module_passes_the_ilcs_acceptance_checklist(gateway, tmp_path, capability, params):
    server, station, simulation, secrets = gateway
    device_id = station.config.device_id
    report = acceptance(
        f"https://localhost:{server.port}/api/v1", token_file=secrets / f"{device_id}.token",
        ca_file=secrets / f"{device_id}.crt", capability=capability, params=params, expected_device_id=device_id,
        state_root=tmp_path / "ilcs", supports=SUPPORTS, timeout=30,
    )
    states = {check.key: check.state for check in report.checks}
    assert report.ok, report.markdown()
    assert states.pop("hold") == "skip", "契约如实声明不支持保持"
    assert all(state == "pass" for state in states.values()), states
    assert report.simulator and report.identity["methods"]


def test_gateway_restart_still_answers_by_command_id(gateway, tmp_path):
    server, station, simulation, secrets = gateway
    body = {"command_id": "CMD-1", "capability": "cap.ely.dose_liquid", "params": {"mass": 2.0},
            "material": {"name": "EMC", "unit": "g", "param": "mass"}}
    assert server.gateway.submit(body)["state"] == "running"
    import time

    deadline = time.monotonic() + 20
    while server.gateway.query("CMD-1")["state"] == "running" and time.monotonic() < deadline:
        time.sleep(0.1)
    first = server.gateway.query("CMD-1")
    assert first["state"] == "done" and first["delivered"]["materials"][0]["material"] == "EMC"
    server.stop()
    reborn, other = simulated_station(state_dir=tmp_path / "state")
    try:
        device_id = station.config.device_id
        again = serve(reborn, device_id=device_id, state_dir=tmp_path / "state", address="127.0.0.1", port=0,
                      token_file=secrets / f"{device_id}.token", cert=secrets / f"{device_id}.crt",
                      key=secrets / f"{device_id}.key", host_name="localhost")
        try:
            replay = again.gateway.submit(body)
            assert replay["state"] == "done" and replay["delivered"] == first["delivered"], "重投回放台账，不再加一次"
            assert other.world.net() == pytest.approx(other.world.vessel_g), "新的模拟设备上什么都没加"
        finally:
            again.stop()
    finally:
        other.stop()
