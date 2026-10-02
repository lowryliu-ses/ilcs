"""称量加料站（driver/device.py）的判断规则：对着模拟设备跑，走的是真实的天平 / Quantos / 注射泵客户端。"""
from __future__ import annotations

from pathlib import Path
import time

import pytest

from ilcs_gateway import Job, ReceiptLost, Rejected

from driver.device import Station
from simulator import default_config, simulated_station


@pytest.fixture()
def rig(tmp_path: Path):
    station, simulation = simulated_station(state_dir=tmp_path / "state", settle_sec=0.02, dose_seconds=0.15,
                                            step_seconds=0.00001)
    try:
        yield station, simulation
    finally:
        simulation.stop()


def job(command_id: str, capability: str, material: str = "", **params) -> Job:
    return Job(command_id=command_id, capability=capability, params=params,
               material={"name": material, "unit": "g", "param": "mass"} if material else {})


def wait(station: Station, started: Job, timeout: float = 15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = station.status(started)
        if status.state != "running":
            return status
        time.sleep(0.05)
    raise AssertionError("动作没在时限内结束")


def run(station: Station, started: Job):
    started.handle = station.start(started)
    return wait(station, started)


def test_weigh_reports_the_stable_net_mass(rig):
    station, simulation = rig
    status = run(station, job("CMD-W", "cap.weigh"))
    assert status.state == "done" and status.actuals["mass"] == pytest.approx(simulation.world.vessel_g, abs=1e-4)
    assert "materials" not in status.actuals


def test_liquid_dosing_lands_within_tolerance_and_reports_consumption(rig):
    station, simulation = rig
    status = run(station, job("CMD-L", "cap.ely.dose_liquid", "EMC", mass=12.5))
    assert status.state == "done", status.error
    assert status.actuals["mass"] == pytest.approx(12.5, abs=0.01)
    assert status.actuals["materials"] == [{"material": "EMC", "unit": "g", "quantity": status.actuals["mass"]}]
    assert simulation.world.net() == pytest.approx(status.actuals["mass"], abs=1e-4), "回报的就是秤上加进去的量"


def test_a_dry_line_fails_and_says_how_much_went_in(rig):
    station, simulation = rig
    simulation.world.reservoirs[2].volume_ul = 3000  # DEC 只剩 3 mL，目标要 10 g
    status = run(station, job("CMD-D", "cap.ely.dose_liquid", "DEC", mass=10))
    assert status.state == "failed" and "读数没有变化" in status.error and "已加" in status.error


@pytest.mark.parametrize(("command", "kind", "fragment"), [
    (job("CMD-1", "cap.ely.dose_liquid", "VC", mass=1), "invalid", "没有登记阀端口"),
    (job("CMD-2", "cap.ely.dose_liquid", "EMC", mass=-1), "invalid", "不是非负的质量"),
    (job("CMD-3", "cap.ely.dose_liquid", "EMC", mass=1, rate=2), "invalid", "不接受参数 rate"),
    (job("CMD-4", "cap.ely.dose_liquid", "EMC", wells={"A1": {"mass": 1}, "A2": {"mass": 2}}), "unsupported", "一次只能放一瓶"),
    (job("CMD-5", "cap.weigh", mass=1), "invalid", "称量不带参数"),
    (job("CMD-6", "cap.ely.stir"), "unsupported", "不做 cap.ely.stir"),
    (Job(command_id="CMD-7", capability="cap.weigh", params={}, method={"program": "NOPE"}), "invalid", "没有 cap.weigh 的程序"),
])
def test_what_the_station_refuses_before_moving(rig, command, kind, fragment):
    station, simulation = rig
    with pytest.raises(Rejected) as caught:
        station.start(command)
    assert caught.value.kind == kind and fragment in caught.value.message
    assert simulation.station_motions() == 0 if hasattr(simulation, "station_motions") else True
    assert station.faults.motions == 0


def test_one_well_and_a_zero_amount(rig):
    station, simulation = rig
    status = run(station, job("CMD-A1", "cap.ely.dose_liquid", "DMC", wells={"A1": {"mass": 2.0}}))
    assert status.state == "done" and status.actuals["wells"]["A1"]["mass"] == pytest.approx(2.0, abs=0.01)
    zero = run(station, job("CMD-Z", "cap.ely.dose_liquid", "DMC", wells={"A2": {"mass": 0}}))
    assert zero.state == "done" and zero.actuals["wells"]["A2"]["mass"] == 0 and "materials" not in zero.actuals


def test_no_material_uses_the_acceptance_material_only(rig):
    station, simulation = rig
    status = run(station, job("CMD-ACC", "cap.ely.dose_liquid", mass=1.0))
    assert status.state == "done" and status.actuals["material"] == "DMC"
    data = default_config()
    data.pop("acceptance_material")
    plain, other = simulated_station(config=data, settle_sec=0.02, step_seconds=0.00001)
    try:
        with pytest.raises(Rejected, match="没带物料"):
            plain.start(job("CMD-X", "cap.ely.dose_liquid", mass=1.0))
    finally:
        other.stop()


def test_powder_dosing_checks_the_head_and_reports_actual(rig):
    station, simulation = rig
    status = run(station, job("CMD-P", "cap.ely.dose_solid", "LiFSI", mass=0.8))
    assert status.state == "done", status.error
    assert simulation.world.head().substance == "LiFSI", "模拟模式按指令的料换上了加样头"
    assert status.actuals["mass"] == pytest.approx(0.8, rel=0.01)
    assert status.actuals["materials"][0]["material"] == "LiFSI"


def test_wrong_or_missing_head_is_refused_without_a_loader(rig):
    station, simulation = rig
    station.head_loader = None  # 真机：网关不换头，只核对
    simulation.world.mount("LiPF6")
    with pytest.raises(Rejected, match="不加错料"):
        station.start(job("CMD-H", "cap.ely.dose_solid", "LiBF4", mass=0.3))
    simulation.world.mounted = None
    with pytest.raises(Rejected, match="没有装加样头"):
        station.start(job("CMD-H2", "cap.ely.dose_solid", "LiPF6", mass=0.3))
    assert station.faults.motions == 0


def test_powder_flow_error_is_a_failure_with_the_partial_amount(rig):
    station, simulation = rig
    simulation.scale.powder_flow_error = True
    status = run(station, job("CMD-F", "cap.ely.dose_solid", "LiPF6", mass=0.5))
    assert status.state == "failed" and "出粉故障" in status.error and "已加" in status.error


def test_busy_balance_and_abort(rig):
    station, simulation = rig
    simulation.scale.dose_seconds = 5
    started = job("CMD-LONG", "cap.ely.dose_solid", "LiPF6", mass=0.5)
    started.handle = station.start(started)
    with pytest.raises(Rejected) as busy:
        station.start(job("CMD-NEXT", "cap.weigh"))
    assert busy.value.kind == "busy"
    station.abort(started)
    status = station.status(started)
    assert status.state == "failed" and "终止" in status.error


def test_results_survive_a_gateway_restart(rig, tmp_path: Path):
    station, simulation = rig
    started = job("CMD-R", "cap.ely.dose_liquid", "EMC", mass=1.0)
    done = run(station, started)
    reborn = Station(station.config, station.balance, station.quantos, station.pump, state_dir=tmp_path / "state")
    again = reborn.status(started)
    assert again.state == "done" and again.actuals == done.actuals
    with pytest.raises(RuntimeError, match="没有出结论"):
        reborn.status(Job(command_id="CMD-NEVER", capability="cap.weigh", params={}, handle="CMD-NEVER#1"))


def test_lost_receipt_still_doses_once(rig):
    station, simulation = rig
    station.faults.set_fault("lost_receipt")
    started = job("CMD-LOST", "cap.ely.dose_liquid", "EMC", mass=1.0)
    with pytest.raises(ReceiptLost) as lost:
        station.start(started)
    started.handle = lost.value.handle
    assert wait(station, started).state == "done" and station.faults.motions == 1


def test_an_abort_that_lands_after_the_dose_finished_is_refused(rig):
    """停止命令没赶上（这里让 Quantos 不理停止）：粉确实加进去了，如实拒绝终止，原作业照报完成、按实际量入账。"""
    station, simulation = rig
    station.quantos.stop = lambda: None
    started = job("CMD-RACE", "cap.ely.dose_solid", "LiPF6", mass=0.4)
    started.handle = station.start(started)
    with pytest.raises(Rejected, match="来不及终止"):
        station.abort(started)
    status = station.status(started)
    assert status.state == "done" and status.actuals["mass"] == pytest.approx(0.4, rel=0.01)


def test_an_aborted_weighing_reports_nothing(rig):
    station, simulation = rig
    started = job("CMD-W3", "cap.weigh")
    started.handle = station.start(started)
    station.abort(started)
    status = station.status(started)
    assert status.state == "failed" and "终止" in status.error and "mass" not in status.actuals


def test_programs_come_from_the_config_with_their_own_tolerances(tmp_path):
    """设备方法里写的程序名（如电解液线的 DOSE-SOLVENT）要和站自报的一致；程序可以带自己的容差。"""
    data = default_config()
    data["programs"] = {"DOSE-SOLVENT": {"action": "dose_liquid", "name": "溶剂称量加注", "tolerance_g": 0.05}}
    station, simulation = simulated_station(config=data, settle_sec=0.02, step_seconds=0.00001)
    try:
        assert [row["program"] for row in station.identity()["methods"]] == ["DOSE-SOLVENT"]
        with pytest.raises(Rejected, match="没有 cap.weigh 的程序"):
            station.start(job("CMD-W", "cap.weigh"))
        started = Job(command_id="CMD-S", capability="cap.ely.dose_liquid", params={"mass": 2.0},
                      method={"program": "DOSE-SOLVENT"}, material={"name": "EMC", "unit": "g", "param": "mass"})
        status = run(station, started)
        assert status.state == "done" and abs(status.actuals["mass"] - 2.0) <= 0.05
    finally:
        simulation.stop()
    data["programs"] = {"X": {"action": "stir"}}
    with pytest.raises(ValueError, match="程序 X 的 action"):
        simulated_station(config=data)
