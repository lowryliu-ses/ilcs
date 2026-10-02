"""设备模块自测：对假加热板跑 ILCS 的接入验收清单（含故障项目），再测驱动自己的判断。CI 里必须全过。

    pytest devices/gateway/ika-stirrer/tests
"""
from __future__ import annotations

from pathlib import Path
import time

import pytest

from ilcs_gateway import Job, ReceiptLost, Rejected, serve
from ilcs_gateway.testing import acceptance

from driver.config import Config
from simulator import default_config, simulated_station

CAPABILITY = "cap.ely.stir"
DEVICE_ID = default_config()["device_id"]
SUPPORTS = {"hold": False, "abort": True, "query": True, "dedup": True}


def job(command_id: str, program: str = "STIR", **params) -> Job:
    return Job(command_id=command_id, capability=CAPABILITY, params=params, method={"program": program})


def wells_job(command_id: str, wells: dict, **params) -> Job:
    return job(command_id, wells=wells, **params)


def started(station, command: Job) -> Job:
    command.handle = station.start(command)
    return command


def until(predicate, timeout: float = 5.0, interval: float = 0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError(f"{timeout:g} 秒内没等到")


def running(sim, key: str) -> bool:
    state = sim.plates[key].snapshot()
    return state["motor"] or state["heater"]


@pytest.fixture()
def build():
    """造模拟工位（每个位置一块假板），测试结束时都停掉。"""
    made = []

    def factory(*, state_dir=None, link_timeout: float = 1.0, **changes):
        station, sim = simulated_station({**default_config(), **changes}, state_dir=state_dir, link_timeout=link_timeout)
        made.append((station, sim))
        return station, sim

    yield factory
    for station, sim in made:
        station.close()
        sim.stop()


def _serve(station, tmp_path: Path):
    secrets = tmp_path / "secrets"
    return serve(station, device_id=DEVICE_ID, state_dir=tmp_path / "state", address="127.0.0.1", port=0,
                 token_file=secrets / f"{DEVICE_ID}.token", cert=secrets / f"{DEVICE_ID}.crt",
                 key=secrets / f"{DEVICE_ID}.key", host_name="localhost"), secrets


@pytest.fixture()
def gateway(build, tmp_path: Path):
    station, sim = build(state_dir=tmp_path / "state")
    server, secrets = _serve(station, tmp_path)
    try:
        yield server, station, sim, secrets
    finally:
        server.stop()


# ---------- ILCS 接入验收 ----------

def test_module_passes_the_ilcs_acceptance_checklist(gateway, tmp_path):
    server, station, sim, secrets = gateway
    report = acceptance(
        f"https://localhost:{server.port}/api/v1", token_file=secrets / f"{DEVICE_ID}.token",
        ca_file=secrets / f"{DEVICE_ID}.crt", capability=CAPABILITY,
        params={"temp": 40, "time": 0.5, "rpm": 300, "position": 1}, expected_device_id=DEVICE_ID,
        state_root=tmp_path / "ilcs", supports=SUPPORTS,
    )
    states = {check.key: check.state for check in report.checks}
    assert report.ok, report.markdown()
    assert states.pop("hold") == "skip", "加热板不做保持，契约如实声明"
    assert all(state == "pass" for state in states.values()), states
    assert report.simulator and report.identity["methods"], "方法目录由网关配置的程序自报"
    assert not any(running(sim, key) for key in sim.plates), "验收留下的板都停了"


def test_gateway_restart_still_answers_by_command_id(build, tmp_path):
    """作业跑完时没人来查、网关随后重启：按原指令号照样答得上完成与实测值；重投不会让板子再启动一次。"""
    station, sim = build(state_dir=tmp_path / "state")
    server, secrets = _serve(station, tmp_path)
    body = {"command_id": "CMD-1", "capability": CAPABILITY, "method": {"program": "STIR"},
            "params": {"temp": 40, "time": 0.3, "rpm": 300, "position": 3}}
    assert server.gateway.submit(body)["state"] == "running" and station.faults.motions == 1
    until(lambda: not running(sim, "3"))
    assert station.runs["CMD-1"].over.wait(2), "计时线程收尾、结论写进作业记录"
    server.stop()
    station.close(stop=False)  # 进程崩掉：不动设备
    again = sim.station(state_dir=tmp_path / "state", faults=station.faults)
    restarted, _ = _serve(again, tmp_path)
    try:
        receipt = restarted.gateway.query("CMD-1")
        assert receipt["state"] == "done", receipt
        assert receipt["delivered"]["position"] == "3" and receipt["delivered"]["rpm"] == pytest.approx(300, abs=10)
        assert restarted.gateway.submit(body)["state"] == "done" and station.faults.motions == 1, "重投回放台账，不再动设备"
    finally:
        restarted.stop()
        again.close()


def test_gateway_restart_mid_run_stops_the_plate_and_fails_the_step(build, tmp_path):
    """网关在作业途中重启：不续时——先把板停下，再判失败（写明计划多久），不留一块没人计时的加热板。"""
    station, sim = build(state_dir=tmp_path / "state")
    server, _ = _serve(station, tmp_path)
    body = {"command_id": "CMD-2", "capability": CAPABILITY, "params": {"temp": 50, "time": 60, "rpm": 400, "position": 2}}
    assert server.gateway.submit(body)["state"] == "running"
    server.stop()
    station.close(stop=False)
    assert running(sim, "2"), "网关没了，板子还在转、还在加热"
    again = sim.station(state_dir=tmp_path / "state", faults=station.faults)
    restarted, _ = _serve(again, tmp_path)
    try:
        until(lambda: not running(sim, "2"))
        receipt = until(lambda: (r := restarted.gateway.query("CMD-2"))["state"] == "failed" and r)
        assert "网关重启" in receipt["error"] and "计划 60 s" in receipt["error"]
        assert restarted.gateway.submit(body)["state"] == "failed" and station.faults.motions == 1
        assert again.start(job("CMD-3", temp=40, time=0.2, rpm=300, position=2)), "位置 2 停下以后又能用了"
    finally:
        restarted.stop()
        again.close()


def test_gateway_shutdown_stops_running_plates(build, tmp_path):
    station, sim = build(state_dir=tmp_path / "state")
    command = started(station, job("CMD-S", temp=45, time=60, rpm=300, position=1))
    assert running(sim, "1")
    station.close()  # SIGTERM：先停板
    assert not running(sim, "1")
    again = sim.station(state_dir=tmp_path / "state")
    status = again.status(command)
    assert status.state == "failed" and "网关停止服务" in status.error
    again.close()


# ---------- 拒绝：设备没动 ----------

@pytest.mark.parametrize(("capability", "params", "program", "kind", "fragment"), [
    ("cap.not_this_device", {"temp": 40, "time": 1, "rpm": 300, "position": 1}, "STIR", "unsupported", "只做"),
    (CAPABILITY, {"temp": 40, "time": 1, "rpm": 300, "position": 1}, "UNKNOWN", "invalid", "没有登记程序"),
    (CAPABILITY, {"temp": 10, "time": 1, "rpm": 300, "position": 1}, "STIR", "invalid", "不能制冷"),
    (CAPABILITY, {"temp": -10, "time": 60, "rpm": 400, "position": 1}, "STIR", "invalid", "不能制冷"),
    (CAPABILITY, {"temp": 40, "time": 1, "rpm": 300, "position": 1, "pressure": 1}, "STIR", "invalid", "pressure"),
    (CAPABILITY, {"temp": 40, "time": 1, "rpm": 2000, "position": 1}, "STIR", "invalid", "最高 1500 rpm"),
    (CAPABILITY, {"temp": 40, "time": 1, "rpm": 30, "position": 1}, "STIR", "invalid", "最低 50 rpm"),
    (CAPABILITY, {"temp": 400, "time": 1, "rpm": 300, "position": 1}, "STIR", "invalid", "最高 310 ℃"),
    (CAPABILITY, {"temp": 40, "time": 0, "rpm": 300, "position": 1}, "STIR", "invalid", "正数"),
    (CAPABILITY, {"temp": 40, "rpm": 300, "position": 1}, "STIR", "invalid", "缺 time"),
    (CAPABILITY, {"temp": "hot", "time": 1, "rpm": 300, "position": 1}, "STIR", "invalid", "不是数"),
    (CAPABILITY, {"temp": float("nan"), "time": 1, "rpm": 300, "position": 1}, "STIR", "invalid", "不是数"),
    (CAPABILITY, {"temp": 40, "time": float("inf"), "rpm": 300, "position": 1}, "STIR", "invalid", "不是数"),
    (CAPABILITY, {"temp": 25, "time": 1, "rpm": 0, "position": 1}, "STIR", "invalid", "什么都不做"),
    (CAPABILITY, {"temp": 40, "time": 1, "rpm": 300, "position": 9}, "STIR", "invalid", "不是登记的位置"),
    (CAPABILITY, {"temp": 40, "time": 1, "rpm": 300, "position": "X"}, "STIR", "invalid", "不是登记的位置"),
    (CAPABILITY, {"temp": 40, "time": 1, "rpm": 300, "position": True}, "STIR", "invalid", "不是位置"),
])
def test_driver_rejects_what_the_device_cannot_do(build, capability, params, program, kind, fragment):
    station, sim = build()
    with pytest.raises(Rejected) as caught:
        station.start(Job(command_id="CMD-X", capability=capability, params=params, method={"program": program}))
    assert caught.value.kind == kind and fragment in caught.value.message, caught.value.message
    assert station.faults.motions == 0 and not any(running(sim, key) for key in sim.plates)


def test_position_limits_come_from_the_config(build):
    positions = {"1": {"max_temp_c": 80, "max_rpm": 800, "min_rpm": 100}, "2": {}}
    station, sim = build(positions=positions)
    for params, fragment in (({"temp": 90}, "最高 80 ℃"), ({"rpm": 900}, "最高 800 rpm"), ({"rpm": 60}, "最低 100 rpm")):
        with pytest.raises(Rejected, match=fragment):
            station.start(job("CMD-X", **{"temp": 40, "time": 1, "rpm": 300, "position": 1, **params}))
    assert started(station, job("CMD-OK", temp=90, time=0.2, rpm=900, position=2)).handle == "CMD-OK"


def test_stirring_at_room_temperature_leaves_the_heater_off(build):
    station, sim = build()
    started(station, job("CMD-RT", temp=25, time=0.3, rpm=500, position=1))
    state = sim.plates["1"].snapshot()
    assert state["motor"] and not state["heater"] and "START_1" not in sim.plates["1"].log
    assert not any(line.startswith("OUT_SP_1") for line in sim.plates["1"].log), "不加热就不碰温度设定值"


def test_heating_without_stirring_leaves_the_motor_off(build):
    station, sim = build()
    started(station, job("CMD-H", temp=40, time=0.3, rpm=0, position=1))
    state = sim.plates["1"].snapshot()
    assert state["heater"] and not state["motor"] and "START_4" not in sim.plates["1"].log


def test_material_on_a_stirring_step_is_rejected(build):
    station, sim = build()
    command = job("CMD-M", temp=40, time=1, rpm=300, position=1)
    command.material = {"name": "EMC", "unit": "g", "param": "mass"}
    with pytest.raises(Rejected, match="搅拌不投料"):
        station.start(command)
    assert station.faults.motions == 0


def test_position_is_required_unless_auto_position_is_on(build):
    station, sim = build(auto_position=False)
    with pytest.raises(Rejected, match="由 ILCS 指定"):
        station.start(job("CMD-X", temp=40, time=1, rpm=300))
    assert started(station, job("CMD-A", temp=40, time=0.2, rpm=300, position=2)).handle == "CMD-A"
    assert started(station, job("CMD-B", temp=40, time=0.2, rpm=300, position="4")).handle == "CMD-B"
    assert running(sim, "2") and running(sim, "4") and not running(sim, "1")


def test_busy_position_interlock_and_auto_position(build):
    station, sim = build(positions={"1": {}, "2": {}})
    started(station, job("CMD-A", temp=40, time=30, rpm=300, position=1))
    with pytest.raises(Rejected) as busy:
        station.start(job("CMD-B", temp=40, time=1, rpm=300, position=1))
    assert busy.value.kind == "busy"
    assert started(station, job("CMD-C", temp=40, time=30, rpm=300)).handle == "CMD-C"
    assert running(sim, "2"), "自动挑位置跳过在用的"
    with pytest.raises(Rejected) as full:
        station.start(job("CMD-D", temp=40, time=1, rpm=300))
    assert full.value.kind == "busy" and "空闲的位置只有 0 个" in full.value.message
    station.faults.set_fault("interlock")
    assert station.identity()["interlock"] is True and station.identity()["accepts_commands"] is False
    with pytest.raises(Rejected) as interlock:
        station.start(job("CMD-E", temp=40, time=1, rpm=300, position=1))
    assert interlock.value.kind == "interlocked" and station.faults.motions == 2


def test_a_plate_someone_started_by_hand_is_busy(build):
    station, sim = build()
    plate = sim.plates["3"]
    plate.handle("OUT_SP_4 400")
    plate.handle("START_4")
    until(lambda: plate.snapshot()["speed"] > 100)
    with pytest.raises(Rejected) as caught:
        station.start(job("CMD-X", temp=40, time=1, rpm=300, position=3))
    assert caught.value.kind == "busy" and "搅拌子在转" in caught.value.message
    assert plate.snapshot()["speed_setpoint"] == 400, "网关没碰这块板"


def test_unreachable_plate_before_start_is_a_rejection_not_unknown(build):
    station, sim = build(link_timeout=0.3)
    sim.plates["1"].mute = True
    with pytest.raises(Rejected) as caught:
        station.start(job("CMD-X", temp=40, time=1, rpm=300, position=1))
    assert caught.value.kind == "busy" and "读不到位置 1" in caught.value.message
    assert station.faults.motions == 0


def test_setpoint_the_device_will_not_take_is_rejected_before_motion(build):
    """设定值超出板子自己的范围（比如背面安全温度旋钮压低了），设备会把它压回去：回读发现，设备没动。"""
    station, sim = build()
    sim.plates["1"].max_temp = 60  # 现场把安全温度旋钮拧到了 60 ℃
    with pytest.raises(Rejected) as caught:
        station.start(job("CMD-X", temp=80, time=1, rpm=300, position=1))
    assert caught.value.kind == "invalid" and "回读 60" in caught.value.message
    assert not running(sim, "1") and station.faults.motions == 0
    assert started(station, job("CMD-Y", temp=50, time=0.2, rpm=300, position=1)).handle == "CMD-Y", "位置没被占着"


# ---------- 计时、状态、终止 ----------

def test_timer_stops_the_plate_without_any_status_polling(build):
    """计时在网关里：ILCS 一次都不来查，到点照样停。"""
    station, sim = build()
    began = time.monotonic()
    command = started(station, job("CMD-T", temp=45, time=0.4, rpm=300, position=1))
    assert running(sim, "1")
    until(lambda: not running(sim, "1"))
    assert time.monotonic() - began >= 0.4
    # 板子停了之后网关还要读一次确认停止送到了，才出结论（这里也不查状态，只等计时线程收尾）
    assert station.runs[command.handle].over.wait(2)
    status = station.status(command)
    assert status.state == "done" and status.error == ""
    assert status.actuals["position"] == "1" and status.actuals["duration_s"] == pytest.approx(0.4, abs=0.15)
    assert status.actuals["sensor"] == "external" and status.actuals["temp"] > 25
    assert {point["metric"] for point in status.telemetry} == {"temp", "rpm"}


def test_status_while_running_reports_telemetry_with_setpoints(build):
    station, sim = build(poll_sec=0.05)
    command = started(station, job("CMD-R", temp=60, time=30, rpm=500, position=2))
    status = until(lambda: (s := station.status(command)).telemetry and s)
    assert status.state == "running" and status.actuals == {}
    points = {point["metric"]: point for point in status.telemetry}
    assert points["temp"]["setpoint"] == 60 and points["rpm"]["setpoint"] == 500


def test_abort_stops_the_plates(build):
    station, sim = build()
    command = started(station, job("CMD-A", temp=50, time=60, rpm=400, position=1))
    assert running(sim, "1")
    station.abort(command)
    assert not running(sim, "1"), "终止返回时板子已经停了（停止已确认）"
    status = station.status(command)
    assert status.state == "failed" and "被终止" in status.error
    station.abort(command)  # 已经停了：照样确认


def test_abort_through_the_gateway_confirms_only_after_the_plates_stopped(gateway):
    server, station, sim, _ = gateway
    body = {"command_id": "CMD-GA", "capability": CAPABILITY, "params": {"temp": 50, "time": 60, "rpm": 400, "position": 4}}
    assert server.gateway.submit(body)["state"] == "running"
    receipt = server.gateway.control("abort", "CMD-GA-ABORT", {"target_command_id": "CMD-GA"})
    assert receipt["state"] == "done" and not running(sim, "4")
    assert server.gateway.query("CMD-GA")["state"] == "failed"


def test_abort_that_cannot_confirm_the_stop_is_not_reported_as_stopped(build, monkeypatch):
    import driver.device as device_module

    monkeypatch.setattr(device_module, "ABORT_WAIT_SEC", 1.0)
    station, sim = build(link_timeout=0.2)
    command = started(station, job("CMD-U", temp=50, time=60, rpm=400, position=1))
    sim.plates["1"].mute = True  # 线断了：停止命令到不了
    with pytest.raises(RuntimeError, match="还没确认每块板都停下"):
        station.abort(command)
    assert station.status(command).state == "running", "停止没确认，不报终态"
    sim.plates["1"].mute = False
    until(lambda: station.status(command).state == "failed", timeout=8)
    assert not running(sim, "1")


def test_simulated_fail_and_stuck(build):
    station, sim = build()
    station.faults.set_fault("fail")
    failing = started(station, job("CMD-F", temp=40, time=0.2, rpm=300, position=1))
    until(lambda: station.status(failing).state != "running")
    assert station.status(failing).state == "failed" and "模拟故障" in station.status(failing).error
    assert not running(sim, "1")
    station.faults.set_fault("stuck")
    stuck = started(station, job("CMD-K", temp=40, time=0.2, rpm=300, position=2))
    time.sleep(0.5)
    assert station.status(stuck).state == "running" and running(sim, "2"), "一直不结束"
    station.abort(stuck)
    assert station.status(stuck).state == "failed" and not running(sim, "2")


def test_lost_receipt_starts_the_plate_and_lookup_finds_the_job(build):
    station, sim = build()
    station.faults.set_fault("lost_receipt")
    command = job("CMD-L", temp=40, time=0.3, rpm=300, position=2)
    with pytest.raises(ReceiptLost) as lost:
        station.start(command)
    assert lost.value.handle == "CMD-L" and running(sim, "2") and station.faults.motions == 1
    assert station.lookup(command) == "CMD-L"
    assert station.lookup(job("CMD-NEVER")) is None


def test_setpoint_reset_after_start_is_read_back_and_resent_once(build):
    """有的型号 START_1 之后温度设定值会复位：启动后回读，不对重发一次。"""
    station, sim = build()
    plate = sim.plates["1"]
    plate.reset_on_start = True
    started(station, job("CMD-Q", temp=55, time=30, rpm=300, position=1))
    assert plate.snapshot()["temp_setpoint"] == 55
    assert plate.log.count("OUT_SP_1 55") == 2 and plate.log.count("OUT_SP_4 300") == 1, "温度重发了一次，转速没动"
    order = [line for line in plate.log if line.startswith(("OUT_SP_1", "START_1", "IN_SP_1"))]
    assert order == ["OUT_SP_1 55", "IN_SP_1", "START_1", "IN_SP_1", "OUT_SP_1 55", "IN_SP_1"]


def test_watchdog_is_armed_fed_and_cleared(build):
    station, sim = build(watchdog_sec=20, watchdog_temp_c=25, watchdog_rpm=0)
    command = started(station, job("CMD-W", temp=50, time=0.3, rpm=300, position=1))
    plate = sim.plates["1"]
    assert plate.snapshot()["watchdog"] == 20 and plate.safe_temp == 25 and plate.safe_speed == 0
    assert plate.log.index("OUT_WD2@20") < plate.log.index("START_4"), "先开看门狗再启动"
    until(lambda: station.status(command).state == "done")
    assert plate.snapshot()["watchdog"] == 0 and plate.log[-1] == "OUT_WD2@0", "停下以后关掉看门狗"


# ---------- 一条指令几瓶（ILCS 逐孔参数 wells） ----------

def test_each_well_runs_on_its_own_position_with_its_own_parameters(build):
    station, sim = build(auto_position=False)
    command = started(station, wells_job("CMD-W", {
        "A1": {"position": 1, "temp": 40},
        "A2": {"position": 3.0, "rpm": 500, "time": 0.8},
    }, temp=25, time=0.3, rpm=300))
    one, three = sim.plates["1"].snapshot(), sim.plates["3"].snapshot()
    assert one["heater"] and one["speed_setpoint"] == 300 and one["temp_setpoint"] == 40
    assert three["motor"] and not three["heater"] and three["speed_setpoint"] == 500
    assert station.faults.motions == 2 and not running(sim, "2")
    until(lambda: not running(sim, "1"))
    assert running(sim, "3") and station.status(command).state == "running", "A2 还在搅，整条指令就还在跑"
    until(lambda: station.status(command).state == "done")
    status = station.status(command)
    wells = status.actuals["wells"]
    assert wells["A1"]["position"] == "1" and wells["A2"]["position"] == "3"
    assert wells["A1"]["duration_s"] == pytest.approx(0.3, abs=0.15) and wells["A2"]["duration_s"] == pytest.approx(0.8, abs=0.15)
    assert {point["metric"] for point in status.telemetry} >= {"temp@A1", "rpm@A1", "temp@A2", "rpm@A2"}


def test_wells_fall_back_to_the_step_position_but_never_share_one(build):
    station, sim = build(auto_position=False)
    assert started(station, wells_job("CMD-1", {"A1": {}}, temp=40, time=0.2, rpm=300, position=2)).handle == "CMD-1"
    with pytest.raises(Rejected, match="一个位置只能放一瓶"):
        station.start(wells_job("CMD-2", {"B1": {"position": 4}, "B2": {"position": 4}}, temp=40, time=1, rpm=300))
    with pytest.raises(Rejected, match="一个位置只能放一瓶"):
        station.start(wells_job("CMD-3", {"C1": {"position": 3}, "C2": {}}, temp=40, time=1, rpm=300, position=3))
    with pytest.raises(Rejected, match="孔位 D2"):
        station.start(wells_job("CMD-4", {"D1": {"position": 1}, "D2": {}}, temp=40, time=1, rpm=300))
    with pytest.raises(Rejected, match="孔位 E2 要 5 ℃"):
        station.start(wells_job("CMD-5", {"E1": {"position": 1}, "E2": {"position": 3, "temp": 5}}, temp=40, time=1, rpm=300))
    assert station.faults.motions == 1


@pytest.mark.parametrize("wells", [{}, {"A1": 3}, {"A1": {"position": 1, "speed": 1}}, {"": {"position": 1}}])
def test_malformed_wells_are_rejected(build, wells):
    station, sim = build()
    with pytest.raises(Rejected) as caught:
        station.start(wells_job("CMD-X", wells, temp=40, time=1, rpm=300))
    assert caught.value.kind == "invalid" and station.faults.motions == 0


def test_one_busy_position_blocks_the_whole_command(build):
    station, sim = build(auto_position=False)
    started(station, job("CMD-A", temp=40, time=30, rpm=300, position=2))
    with pytest.raises(Rejected) as busy:
        station.start(wells_job("CMD-W", {"A1": {"position": 1}, "A2": {"position": 2}}, temp=40, time=1, rpm=300))
    assert busy.value.kind == "busy" and station.faults.motions == 1 and not running(sim, "1"), "一个都不启动"


def test_auto_position_gives_each_well_a_distinct_free_position(build):
    station, sim = build(positions={"1": {}, "2": {}, "3": {}})
    started(station, job("CMD-A", temp=40, time=30, rpm=300, position=2))
    command = started(station, wells_job("CMD-W", {"A1": {}, "A2": {"position": 3}}, temp=40, time=30, rpm=300))
    assert running(sim, "1") and running(sim, "3")
    assert {row["position"] for row in station.runs[command.handle].public()["bottles"].values()} == {"1", "3"}
    with pytest.raises(Rejected, match="空闲的位置只有 0 个"):
        station.start(wells_job("CMD-X", {"B1": {}}, temp=40, time=1, rpm=300))


def _wrap(plate, rule):
    original = plate.handle
    plate.handle = lambda line: rule(line, original)


def test_partial_start_stops_what_was_started_and_reports_unknown(build):
    """第二块板启动后设定值回读一直不对：第一块已经在转了，不能报「没动」——两块都停下，按结果未知抛出。"""
    station, sim = build(auto_position=False)
    _wrap(sim.plates["2"], lambda line, original: "200 4" if line == "IN_SP_4" and sim.plates["2"].motor
          else original(line))
    command = wells_job("CMD-P", {"A1": {"position": 1}, "A2": {"position": 2}, "A3": {"position": 3}},
                        temp=40, time=30, rpm=300)
    with pytest.raises(RuntimeError) as caught:
        station.start(command)
    assert not isinstance(caught.value, Rejected)
    message = str(caught.value)
    assert "位置 1（模拟板 1） 已经启动" in message and "只做了一部分" in message and "停止已确认" in message, message
    assert not running(sim, "1") and not running(sim, "2") and not running(sim, "3")
    assert "START_4" not in sim.plates["3"].log, "第三块还没轮到就停了手"
    assert station.lookup(command) is None, "只做了一部分：不认，交人核查"
    assert not station.owner, "启动过的停下了、没启动的也放出来了"
    assert started(station, job("CMD-N", temp=40, time=0.2, rpm=300, position=1)).handle == "CMD-N"
    assert started(station, job("CMD-O", temp=40, time=0.2, rpm=300, position=3)).handle == "CMD-O"


def test_partial_start_keeps_retrying_a_stop_it_could_not_confirm(build):
    """第二块板启动后线断了：先停第一块；第二块的停止没确认就一直重试、位置一直占着，线通了再停下。"""
    station, sim = build(auto_position=False, link_timeout=0.3)
    second = sim.plates["2"]

    def cut_after_start(line, original):
        reply = original(line)
        if line == "START_4":
            second.mute = True
        return reply

    _wrap(second, cut_after_start)
    with pytest.raises(RuntimeError, match="还没确认停下"):
        station.start(wells_job("CMD-P", {"A1": {"position": 1}, "A2": {"position": 2}}, temp=40, time=30, rpm=300))
    assert not running(sim, "1") and running(sim, "2")
    with pytest.raises(Rejected) as busy:
        station.start(job("CMD-B", temp=40, time=1, rpm=300, position=2))
    assert busy.value.kind == "busy", "停止没确认的位置不能再派活"
    second.mute = False
    until(lambda: not running(sim, "2"), timeout=8)
    until(lambda: "2" not in station.owner, timeout=3)


def test_losing_a_plate_mid_run_fails_that_bottle_and_stops_it(build):
    station, sim = build(auto_position=False, poll_sec=0.05, lost_after_sec=0.5, link_timeout=0.2)
    command = started(station, wells_job("CMD-M", {"A1": {"position": 1}, "A2": {"position": 2}},
                                         temp=40, time=2.5, rpm=300))
    sim.plates["1"].mute = True
    status = until(lambda: (s := station.status(command)).error and s, timeout=5)
    assert status.state == "running" and "位置 1" in status.error
    sim.plates["1"].mute = False
    until(lambda: not running(sim, "1"), timeout=5)
    assert running(sim, "2"), "别的瓶照常搅到点"
    final = until(lambda: (s := station.status(command)).state != "running" and s, timeout=8)
    assert final.state == "failed" and "失去监视" in final.error
    assert final.actuals["wells"]["A2"]["duration_s"] == pytest.approx(2.5, abs=0.3)
    assert "error" in final.actuals["wells"]["A1"] and "error" not in final.actuals["wells"]["A2"]


def test_a_short_glitch_does_not_fail_the_bottle(build):
    """串口抖一下（连续几次读不到，但没到 lost_after_sec）：板子自己照转，这一瓶照常到点完成。"""
    station, sim = build(poll_sec=0.05, link_timeout=0.1)
    command = started(station, job("CMD-G", temp=40, time=1.5, rpm=300, position=1))
    sim.plates["1"].mute = True
    time.sleep(0.6)
    sim.plates["1"].mute = False
    assert station.runs[command.handle].over.wait(5)
    status = station.status(command)
    assert status.state == "done" and status.error == "", status.error


def test_gateway_runs_a_multi_well_command_end_to_end(build, tmp_path: Path):
    station, sim = build(auto_position=False)
    server, _ = _serve(station, tmp_path)
    try:
        body = {"command_id": "CMD-E2E", "capability": CAPABILITY, "method": {"program": "STIR-FINAL"},
                "params": {"temp": 25, "time": 0.3, "rpm": 500,
                           "wells": {"A1": {"position": 1.0}, "A2": {"position": 2.0, "temp": 45}}}}
        assert server.gateway.submit(body)["state"] == "running"
        receipt = until(lambda: (r := server.gateway.query("CMD-E2E"))["state"] == "done" and r)
        assert set(receipt["delivered"]["wells"]) == {"A1", "A2"}
        assert server.gateway.submit(body)["state"] == "done" and station.faults.motions == 2, "重投回放，不再启动"
    finally:
        server.stop()


# ---------- 身份与配置 ----------

def test_identity_reports_unreachable_positions_and_fails_only_when_none_answer(build):
    station, sim = build(link_timeout=0.2)
    sim.plates["2"].mute = True
    identity = station.identity()
    assert identity["simulator"] is True and identity["channels"] == 4
    assert [row["reachable"] for row in identity["positions"]] == [True, False, True, True]
    assert identity["commands"] == ["dispatch", "retry", "abort", "query"]
    for plate in sim.plates.values():
        plate.mute = True
    with pytest.raises(RuntimeError, match="一块加热板都连不上"):
        station.identity()


def test_config_problems_are_reported_together():
    with pytest.raises(ValueError) as caught:
        Config.parse({"positions": {"1": {"link": {"kind": "usb"}}, "2": {"link": {"kind": "serial", "port": "COM5"},
                                                                            "sensor": "probe", "max_temp_c": 10}},
                      "programs": {}, "default_program": "X", "watchdog_sec": 5})
    message = str(caught.value)
    for fragment in ("device_id", "位置 1 的 link", "sensor 只能是", "低于 ambient_c", "programs 是空的",
                     "default_program", "watchdog_sec"):
        assert fragment in message, fragment


def test_serial_links_default_to_namur_7e1():
    config = Config.parse({"device_id": "X", "positions": {"1": {"link": {"kind": "serial", "port": "COM5"}}},
                           "programs": {"STIR": {}}})
    link = config.positions["1"].link
    assert (link["baudrate"], link["bytesize"], link["parity"], link["stopbits"]) == (9600, 7, "E", 1)
    assert config.positions["1"].sensor == "plate" and config.vendor == "IKA" and config.capability == CAPABILITY


def test_example_and_simulator_configs_parse():
    module = Path(__file__).resolve().parents[1]
    example = Config.load(module / "config.example.json")
    assert example.keys == ("1", "2", "3", "4") and example.default_program in example.programs
    station, sim = simulated_station(module / "simulator" / "stirrer-sim.json")
    try:
        assert station.config.device_id == "SIM-IKA-STIR-01" and not station.config.auto_position
        assert station.identity()["simulator"] is True
    finally:
        station.close()
        sim.stop()


# ---------- 作业记录 ----------

def test_run_record_that_cannot_be_written_is_a_rejection(build, tmp_path):
    station, sim = build(state_dir=tmp_path / "state")
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    station.store = blocker / "runs"  # 写不进去（比如盘满、权限不对）
    with pytest.raises(Rejected) as caught:
        station.start(job("CMD-X", temp=40, time=1, rpm=300, position=1))
    assert caught.value.kind == "busy" and "没有记录就不动设备" in caught.value.message
    assert not running(sim, "1") and station.faults.motions == 0 and not station.owner


def test_crash_during_start_stops_every_position_of_the_command(build, tmp_path):
    """网关在启动途中崩掉（记录还停在 starting）：重启后把这条指令的每个位置都停下；不认这个作业，留给人核查。"""
    import json as _json

    station, sim = build(state_dir=tmp_path / "state", auto_position=False)
    command = started(station, wells_job("CMD-C", {"A1": {"position": 1}, "A2": {"position": 2}},
                                         temp=40, time=60, rpm=300))
    station.close(stop=False)
    path = station._path(command.handle)
    record = _json.loads(path.read_text(encoding="utf-8"))
    record.update(state="starting", started_ok=False)
    path.write_text(_json.dumps(record), encoding="utf-8")
    assert running(sim, "1") and running(sim, "2")
    again = sim.station(state_dir=tmp_path / "state", faults=station.faults)
    try:
        until(lambda: not running(sim, "1") and not running(sim, "2"))
        assert again.lookup(command) is None
        assert again.runs[command.handle].over.wait(2)
        status = again.status(command)
        assert status.state == "failed" and "网关在启动途中重启" in status.error
    finally:
        again.close()


def test_profile_matches_the_module():
    """profile.json 改过之后要重算摘要；支持标志、验收参数要和模块一致（验收参数网关得认）。"""
    import json as _json
    import sys

    from ilcs_gateway.testing import _ilcs_api

    from driver.device import PARAMETERS

    api = str(_ilcs_api())
    if api not in sys.path:
        sys.path.insert(0, api)
    from app.services.template_service import template_check, template_digest

    module = Path(__file__).resolve().parents[1]
    profile = _json.loads((module / "profile.json").read_text(encoding="utf-8"))
    assert profile["digest"] == template_digest(profile), "profile.json 改过之后要重算摘要"
    assert template_check(profile)["ok"] and profile["supports"] == SUPPORTS
    assert profile["acceptance"]["capability"] == CAPABILITY and set(profile["acceptance"]["params"]) <= set(PARAMETERS)
    example = Config.load(module / "config.example.json")
    params = profile["acceptance"]["params"]
    assert example.ambient_c < params["temp"] <= example.positions["1"].max_temp_c, "验收温度在样例配置位置 1 的范围里"
