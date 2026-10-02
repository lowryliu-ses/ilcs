"""设备模块自测：对三家假冷水机各跑一遍 ILCS 的接入验收清单（含故障项目），再测控温、制冷搅拌与驱动自己的判断。
CI 里必须全过。模拟的浴温时间常数取得很小（0.3 s），到温、计时都按秒以下算。

    pytest devices/gateway/thermostat/tests
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
import threading
import time

import pytest

from ilcs_gateway import Job, ReceiptLost, Rejected, serve
from ilcs_gateway.testing import acceptance

from driver.config import Config
from simulator import default_config, simulated_station

THERMOSTAT, STIR = "cap.thermostat", "cap.ely.stir"
DEVICE_ID = default_config()["device_id"]
SUPPORTS = {"hold": False, "abort": True, "query": True, "dedup": True}
KINDS = ("huber", "julabo", "lauda")
FAST = {"settle_sec": 0.2, "reach_timeout_sec": 10}


def job(command_id: str, capability: str = THERMOSTAT, program: str = "", **params) -> Job:
    return Job(command_id=command_id, capability=capability, params=params,
               method={"program": program} if program else {})


def stir(command_id: str, wells: dict | None = None, **params) -> Job:
    return job(command_id, STIR, "STIR-CHILL", **({"wells": wells} if wells is not None else {}), **params)


def started(station, command: Job) -> Job:
    command.handle = station.start(command)
    return command


def until(predicate, timeout: float = 8.0, interval: float = 0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError(f"{timeout:g} 秒内没等到")


def finished(station, command: Job, timeout: float = 8.0):
    """等后台线程出结论（不经 status 轮询），再读一次状态。"""
    assert station.runs[command.handle].over.wait(timeout), "后台线程没出结论"
    return station.status(command)


def spinning(sim, key: str) -> bool:
    return sim.plates[key].snapshot()["motor"]


def config(kind: str = "huber", *, chiller: dict | None = None, **changes) -> dict:
    data = default_config(kind)
    data["chiller"].update({**FAST, **(chiller or {})})
    data["poll_sec"] = 0.05
    data.update(changes)
    return data


@pytest.fixture()
def build():
    """造模拟工位（假冷水机 + 4 块假板），测试结束时都停掉。"""
    made = []

    def factory(kind: str = "huber", *, state_dir=None, link_timeout: float = 1.0, tau_s: float = 0.3,
                floor_c: float = -40.0, chiller: dict | None = None, **changes):
        station, sim = simulated_station(config(kind, chiller=chiller, **changes), state_dir=state_dir,
                                         link_timeout=link_timeout, tau_s=tau_s, floor_c=floor_c)
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


# ---------- ILCS 接入验收：三家各一遍 ----------

@pytest.mark.parametrize("kind", KINDS)
def test_module_passes_the_ilcs_acceptance_checklist(build, tmp_path, kind):
    station, sim = build(kind, state_dir=tmp_path / "state")
    server, secrets = _serve(station, tmp_path)
    try:
        report = acceptance(
            f"https://localhost:{server.port}/api/v1", token_file=secrets / f"{DEVICE_ID}.token",
            ca_file=secrets / f"{DEVICE_ID}.crt", capability=THERMOSTAT, params={"temp": 18, "time": 0.3},
            expected_device_id=DEVICE_ID, state_root=tmp_path / "ilcs", supports=SUPPORTS,
        )
    finally:
        server.stop()
    states = {check.key: check.state for check in report.checks}
    assert report.ok, report.markdown()
    assert states.pop("hold") == "skip", "不做保持，契约如实声明"
    assert all(state == "pass" for state in states.values()), states
    assert report.simulator and report.identity["methods"], "方法目录由网关配置的程序自报"
    bath = sim.bath.snapshot()
    assert bath["running"] and bath["setpoint"] == pytest.approx(18), "after = keep：验收之后照最后的设定值接着控温"


# ---------- 控温 ----------

def test_thermostat_reaches_settles_holds_and_keeps_running(build):
    station, sim = build()
    command = started(station, job("CMD-T", temp=5, time=0.5))
    status = until(lambda: (s := station.status(command)).telemetry and s)
    assert status.state == "running" and status.actuals == {}
    assert status.telemetry[0]["metric"] == "temp" and status.telemetry[0]["setpoint"] == 5
    assert station.runs[command.handle].phase == "reaching"
    final = finished(station, command)
    assert final.state == "done" and final.error == ""
    actuals = final.actuals
    assert actuals["setpoint"] == 5 and actuals["temp"] == pytest.approx(5, abs=0.5)
    assert actuals["time_to_reach_s"] > 0.2 and actuals["hold_s"] == pytest.approx(0.5, abs=0.15)
    assert actuals["deviation_c"] <= 0.5, "到温之后一直在容差里"
    bath = sim.bath.snapshot()
    assert bath["running"] and bath["setpoint"] == 5, "after = keep：接着按 5 ℃ 控温"
    assert not any(spinning(sim, key) for key in sim.plates), "控温不碰搅拌板"


def test_reached_means_inside_the_tolerance_for_settle_sec(build):
    station, sim = build(chiller={"settle_sec": 0.6, "tolerance_c": 0.3})
    command = started(station, job("CMD-S", temp=10, time=0))
    run = station.runs[command.handle]
    until(lambda: run.inside_since is not None)
    entered = time.monotonic()
    final = finished(station, command)
    assert final.state == "done" and time.monotonic() - entered >= 0.55, "进了容差还要再等 settle_sec"
    assert final.actuals["hold_s"] == pytest.approx(0, abs=0.1), "time 0：到温即完成"


def test_after_standby_returns_the_bath_to_the_standby_temperature(build):
    station, sim = build(chiller={"after": "standby", "standby_c": 20})
    command = started(station, job("CMD-B", temp=10, time=0.2))
    assert finished(station, command).state == "done"
    bath = sim.bath.snapshot()
    assert bath["running"] and bath["setpoint"] == 20, "结束之后回到待机温度、接着控温"


def test_reach_timeout_fails_with_the_last_reading(build):
    """冷水机冷不下去（制冷能力只到 0 ℃）：reach_timeout_sec 秒没到温判失败，写明最后读数。"""
    station, sim = build(floor_c=0.0, chiller={"reach_timeout_sec": 1.5})
    command = started(station, job("CMD-R", temp=-10, time=1))
    final = finished(station, command)
    assert final.state == "failed" and "1.5 秒内没到温" in final.error and "最后读数" in final.error
    assert "time_to_reach_s" not in final.actuals and final.actuals["temp"] == pytest.approx(0, abs=0.6)


def test_wells_on_a_thermostat_step_report_the_same_bath_for_each_well(build):
    station, sim = build()
    command = started(station, job("CMD-W", temp=18, time=0.2, wells={"A1": {}, "A2": {"temp": 18}}))
    final = finished(station, command)
    assert final.state == "done" and set(final.actuals["wells"]) == {"A1", "A2"}
    assert final.actuals["wells"]["A1"]["setpoint"] == 18 == final.actuals["wells"]["A2"]["setpoint"]


def test_setpoint_is_written_only_when_it_changes(build):
    """LAUDA 的 WK / WKL 冷水机一小时只许改 20 次设定值：同一个温度连着做，不重复写。"""
    station, sim = build("lauda")
    for index in range(2):
        assert finished(station, started(station, job(f"CMD-{index}", temp=18, time=0.1))).state == "done"
    assert sim.chiller.changes == 1 and sum(line == "START" for line in sim.chiller.log) == 1, "已经在控温：不再 START"


# ---------- 拒绝：设备没动 ----------

@pytest.mark.parametrize(("capability", "params", "program", "kind", "fragment"), [
    ("cap.not_this_device", {"temp": 5, "time": 1}, "", "unsupported", "只做"),
    (THERMOSTAT, {"temp": 5, "time": 1}, "UNKNOWN", "invalid", "没有登记"),
    (THERMOSTAT, {"temp": 5, "time": 1}, "STIR-CHILL", "invalid", "没有登记"),
    (THERMOSTAT, {"temp": 5, "time": 1, "pressure": 1}, "", "invalid", "pressure"),
    (THERMOSTAT, {"temp": 5, "time": 1, "position": 1}, "", "invalid", "position"),
    (THERMOSTAT, {"temp": -30, "time": 1}, "", "invalid", "-20–25 ℃"),
    (THERMOSTAT, {"temp": 30, "time": 1}, "", "invalid", "-20–25 ℃"),
    (THERMOSTAT, {"temp": 5}, "", "invalid", "缺 time"),
    (THERMOSTAT, {"temp": "cold", "time": 1}, "", "invalid", "不是数"),
    (THERMOSTAT, {"temp": float("nan"), "time": 1}, "", "invalid", "不是数"),
    (THERMOSTAT, {"temp": 5, "time": -1}, "", "invalid", "不能是负数"),
    (THERMOSTAT, {"temp": 5, "time": 1, "wells": {"A1": {}, "A2": {"time": 2}}}, "", "invalid", "保温时长"),
    (STIR, {"temp": -10, "time": 0, "rpm": 400, "position": 1}, "STIR-CHILL", "invalid", "正数"),
    (STIR, {"temp": -10, "time": 60, "rpm": 2000, "position": 1}, "STIR-CHILL", "invalid", "最高 1500 rpm"),
    (STIR, {"temp": -10, "time": 60, "rpm": 30, "position": 1}, "STIR-CHILL", "invalid", "最低 50 rpm"),
    (STIR, {"temp": -10, "time": 60, "rpm": -5, "position": 1}, "STIR-CHILL", "invalid", "负数"),
    (STIR, {"temp": -10, "time": 60, "rpm": 400, "position": 9}, "STIR-CHILL", "invalid", "不是登记的位置"),
    (STIR, {"temp": -10, "time": 60, "rpm": 400, "position": True}, "STIR-CHILL", "invalid", "不是位置"),
    (STIR, {"temp": -10, "time": 60, "rpm": 400, "speed": 1}, "STIR-CHILL", "invalid", "speed"),
    (STIR, {"temp": -10, "time": 60, "rpm": 400, "wells": {"A1": {"position": 1, "torque": 3}}}, "STIR-CHILL",
     "invalid", "torque"),
])
def test_driver_rejects_what_the_device_cannot_do(build, capability, params, program, kind, fragment):
    station, sim = build()
    with pytest.raises(Rejected) as caught:
        station.start(Job(command_id="CMD-X", capability=capability, params=params,
                          method={"program": program} if program else {}))
    assert caught.value.kind == kind and fragment in caught.value.message, caught.value.message
    assert station.faults.motions == 0 and not sim.bath.snapshot()["running"]
    assert not any(line.startswith("{M00") and not line.endswith("****") for line in sim.chiller.log), "没写设定值"


def test_wells_asking_different_temperatures_are_rejected(build):
    station, sim = build(auto_position=False)
    with pytest.raises(Rejected) as caught:
        station.start(stir("CMD-X", {"A1": {"position": 1, "temp": -10}, "A2": {"position": 2, "temp": 5},
                                     "A3": {"position": 3}}, temp=-10, time=60, rpm=400))
    assert caught.value.kind == "invalid" and "同一个冷浴只能一个温度" in caught.value.message
    assert "A1、A3 要 -10 ℃" in caught.value.message and "A2 要 5 ℃" in caught.value.message
    assert station.faults.motions == 0 and not sim.bath.snapshot()["running"]


def test_material_on_a_chilling_step_is_rejected(build):
    station, sim = build()
    command = job("CMD-M", temp=5, time=1)
    command.material = {"name": "EMC", "unit": "g", "param": "mass"}
    with pytest.raises(Rejected, match="不投料"):
        station.start(command)


def test_stir_is_unsupported_without_stirrers():
    data = config()
    for key in ("stirrers", "capabilities"):
        data.pop(key)
    data["programs"] = {"CHILL": {"name": "控温"}}
    station, sim = simulated_station(data, tau_s=0.3)
    try:
        assert station.config.capabilities == {"thermostat": THERMOSTAT}
        assert station.identity()["methods"] == [{"program": "CHILL", "name": "控温", "capability": THERMOSTAT}]
        with pytest.raises(Rejected) as caught:
            station.start(job("CMD-X", STIR, temp=-10, time=60, rpm=400, position=1))
        assert caught.value.kind == "unsupported"
    finally:
        station.close()
        sim.stop()


def test_one_job_at_a_time_on_the_bath(build):
    station, sim = build()
    first = started(station, job("CMD-A", temp=10, time=30))
    for second in (job("CMD-B", temp=10, time=1), stir("CMD-C", temp=10, time=1, rpm=300)):
        with pytest.raises(Rejected) as caught:
            station.start(second)
        assert caught.value.kind == "busy" and "冷浴上还有作业 CMD-A" in caught.value.message
    station.abort(first)
    assert started(station, job("CMD-D", temp=10, time=0.1)).handle == "CMD-D", "终止之后冷浴空出来了"


def test_interlock_and_chiller_alarm_reject_and_show_in_identity(build):
    station, sim = build()
    station.faults.set_fault("interlock")
    identity = station.identity()
    assert identity["interlock"] is True and identity["accepts_commands"] is False
    with pytest.raises(Rejected) as caught:
        station.start(job("CMD-X", temp=5, time=1))
    assert caught.value.kind == "interlocked"
    station.faults.set_fault("none")
    sim.chiller.set_alarm()
    identity = station.identity()
    assert identity["interlock"] is True and identity["chiller"]["alarm"] == "Huber 报错 -1331"
    with pytest.raises(Rejected) as caught:
        station.start(job("CMD-Y", temp=5, time=1))
    assert caught.value.kind == "interlocked" and "报警" in caught.value.message
    assert station.faults.motions == 0 and not sim.bath.snapshot()["running"]


def test_julabo_in_manual_mode_is_interlocked(build):
    station, sim = build("julabo")
    sim.chiller.remote = False
    assert station.identity()["accepts_commands"] is False
    with pytest.raises(Rejected) as caught:
        station.start(job("CMD-X", temp=5, time=1))
    assert caught.value.kind == "interlocked" and "面板控制模式" in caught.value.message
    assert not any(line.startswith("out_") for line in sim.chiller.log), "一条 out 命令都没发"


def test_unreachable_chiller_before_start_is_a_busy_rejection(build):
    station, sim = build(link_timeout=0.3)
    sim.chiller.mute = True
    with pytest.raises(Rejected) as caught:
        station.start(job("CMD-X", temp=5, time=1))
    assert caught.value.kind == "busy" and "读不到" in caught.value.message and station.faults.motions == 0
    with pytest.raises(RuntimeError, match="连不上"):
        station.identity()


@pytest.mark.parametrize("kind", KINDS)
def test_setpoint_outside_the_chillers_own_range_is_rejected_before_motion(build, kind):
    """冷水机菜单里把设定范围收窄到 -5 ℃ 以上：Huber 预读 0x30 / 0x31 就拒；Julabo 回 -10 VALUE TOO SMALL、
    LAUDA 回 ERR_6，冷水机都没收这个值。三家都是明确拒绝、设备没动。"""
    station, sim = build(kind)
    sim.chiller.min_setpoint = -5.0
    with pytest.raises(Rejected) as caught:
        station.start(job("CMD-X", temp=-10, time=1))
    assert caught.value.kind == "invalid", caught.value.message
    bath = sim.bath.snapshot()
    assert not bath["running"] and bath["setpoint"] == pytest.approx(22) and station.faults.motions == 0
    assert started(station, job("CMD-Y", temp=-4, time=0.1)).handle == "CMD-Y", "冷浴没被占着"


# ---------- 制冷搅拌 ----------

def test_chilled_stirring_end_to_end(build):
    """几瓶共用一个冷浴：先到温，再按各瓶的转速启动各自的板；各瓶自己计时、到点就停——这期间一次都不查状态。"""
    station, sim = build(auto_position=False)
    command = started(station, stir("CMD-E", {"A1": {"position": 1, "rpm": 400, "time": 0.4},
                                              "A2": {"position": 3, "rpm": 300, "time": 0.9}}, temp=-10))
    assert not spinning(sim, "1") and not spinning(sim, "3"), "冷浴还没到温：板子不转"
    until(lambda: spinning(sim, "1"))
    bath = sim.bath.snapshot()["temp"]
    assert abs(bath + 10) <= 0.5, f"到温（-10 ± 0.5 ℃）之后才开始搅：此刻浴温 {bath:.2f}"
    assert spinning(sim, "3") and not spinning(sim, "2") and not spinning(sim, "4")
    until(lambda: not spinning(sim, "1"))
    assert spinning(sim, "3"), "A1 到点停了，A2 还在搅"
    final = finished(station, command)
    assert final.state == "done" and final.error == "", final.error
    wells = final.actuals["wells"]
    assert wells["A1"]["position"] == "1" and wells["A2"]["position"] == "3"
    assert wells["A1"]["rpm"] == pytest.approx(400, abs=10) and wells["A2"]["rpm"] == pytest.approx(300, abs=10)
    assert wells["A1"]["duration_s"] == pytest.approx(0.4, abs=0.15) and wells["A2"]["duration_s"] == pytest.approx(0.9, abs=0.15)
    assert wells["A1"]["temp"] == pytest.approx(-10, abs=0.5)
    assert {point["metric"] for point in final.telemetry} == {"temp", "rpm@A1", "rpm@A2"}
    plate = sim.plates["1"]
    assert plate.log.index("OUT_SP_4 400") < plate.log.index("START_4") < plate.log.index("STOP_4") < plate.log.index("STOP_1")
    assert "START_1" not in plate.log, "冷块上的板不开加热"


def test_single_bottle_stir_reports_flat_actuals(build):
    station, sim = build()
    final = finished(station, started(station, stir("CMD-1", temp=15, time=0.3, rpm=500)))
    assert final.state == "done" and final.actuals["position"] == "1" and final.actuals["rpm"] == pytest.approx(500, abs=10)
    assert {point["metric"] for point in final.telemetry} == {"temp", "rpm"}


def test_a_bottle_at_zero_rpm_only_chills_and_never_touches_its_plate(build):
    station, sim = build(auto_position=False)
    command = started(station, stir("CMD-Z", {"A1": {"position": 1, "rpm": 0}, "A2": {"position": 2}},
                                    temp=15, time=0.3, rpm=300))
    final = finished(station, command)
    assert final.state == "done" and final.actuals["wells"]["A1"]["rpm"] == 0.0
    assert final.actuals["wells"]["A1"]["duration_s"] == pytest.approx(0.3, abs=0.15)
    assert sim.plates["1"].log == [] and "START_4" in sim.plates["2"].log


def test_auto_position_and_busy_positions(build):
    station, sim = build()
    command = started(station, stir("CMD-A", {"A1": {}, "A2": {}}, temp=15, time=0.3, rpm=300))
    layout = {row["well"]: row["position"] for row in station.runs[command.handle].public()["bottles"].values()}
    assert layout == {"A1": "1", "A2": "2"}, "没带位置时按登记顺序挑空闲的"
    assert finished(station, command).state == "done"
    with pytest.raises(Rejected, match="一个位置只能放一瓶"):
        station.start(stir("CMD-B", {"B1": {"position": 2}, "B2": {"position": "2"}}, temp=15, time=1, rpm=300))
    with pytest.raises(Rejected) as full:
        station.start(stir("CMD-C", {f"C{i}": {} for i in range(5)}, temp=15, time=1, rpm=300))
    assert full.value.kind == "busy" and "空闲的位置只有 4 个" in full.value.message


def test_position_is_required_unless_auto_position_is_on(build):
    station, sim = build(auto_position=False)
    with pytest.raises(Rejected, match="由 ILCS 指定"):
        station.start(stir("CMD-X", temp=-10, time=60, rpm=400))


def test_a_plate_someone_started_by_hand_is_busy(build):
    station, sim = build()
    plate = sim.plates["1"]
    plate.handle("OUT_SP_4 400")
    plate.handle("START_4")
    until(lambda: plate.snapshot()["speed"] > 100)
    with pytest.raises(Rejected) as caught:
        station.start(stir("CMD-X", temp=-10, time=1, rpm=300, position=1))
    assert caught.value.kind == "busy" and "搅拌子在转" in caught.value.message
    assert not sim.bath.snapshot()["running"], "冷水机也没动"


def test_speed_setpoint_reset_after_start_is_read_back_and_resent_once(build):
    station, sim = build()
    plate = sim.plates["1"]
    plate.reset_on_start = True
    final = finished(station, started(station, stir("CMD-Q", temp=15, time=0.3, rpm=400, position=1)))
    assert final.state == "done" and plate.log.count("OUT_SP_4 400") == 2, "启动后回读不对，重发一次"


def _wrap(target, attribute, rule):
    original = getattr(target, attribute)
    setattr(target, attribute, lambda line: rule(line, original))


def test_partial_motor_start_stops_what_was_started_and_fails(build):
    """第二块板启动后转速设定值一直回读不对：第一块已经在转了——都停下，判失败，第三块不再启动。"""
    station, sim = build(auto_position=False)
    second = sim.plates["2"]
    _wrap(second, "handle", lambda line, original: "200 4" if line == "IN_SP_4" and second.motor else original(line))
    command = started(station, stir("CMD-P", {"A1": {"position": 1}, "A2": {"position": 2}, "A3": {"position": 3}},
                                    temp=15, time=30, rpm=300))
    final = finished(station, command)
    assert final.state == "failed" and "只做了一部分" in final.error and "位置 1（模拟板 1） 已经开始搅拌" in final.error
    assert not spinning(sim, "1") and not spinning(sim, "2") and "START_4" not in sim.plates["3"].log
    assert not station.owner and not station.bath_owner, "停下之后位置、冷浴都放出来了"


def test_motors_stop_at_their_deadline_even_if_the_stop_needs_retries(build):
    """停的时候线断了：停止没确认就一直重试、一直报在跑；线通了停下，才出结论。"""
    station, sim = build(link_timeout=0.2)
    plate = sim.plates["1"]
    command = started(station, stir("CMD-U", temp=15, time=0.3, rpm=300, position=1))
    until(lambda: spinning(sim, "1"))
    plate.mute = True
    status = until(lambda: (s := station.status(command)).error and "停止还没确认" in s.error and s, timeout=6)
    assert status.state == "running" and spinning(sim, "1")
    plate.mute = False
    final = finished(station, command, timeout=8)
    assert final.state == "done" and not spinning(sim, "1")


# ---------- 终止 ----------

def test_abort_stops_the_motors_and_returns_the_bath_to_standby(build):
    station, sim = build(chiller={"after": "standby", "standby_c": 20})
    command = started(station, stir("CMD-A", {"A1": {}, "A2": {}}, temp=10, time=30, rpm=400))
    until(lambda: spinning(sim, "1") and spinning(sim, "2"))
    station.abort(command)
    assert not spinning(sim, "1") and not spinning(sim, "2"), "终止返回时板子已经停了（停止已确认）"
    status = station.status(command)
    assert status.state == "failed" and "被终止" in status.error
    assert "提前停下" in status.actuals["wells"]["A1"]["error"]
    assert sim.bath.snapshot()["setpoint"] == 20, "终止之后照样回到待机温度"
    station.abort(command)  # 已经停了：照样确认


def test_abort_while_reaching_never_starts_the_motors(build):
    station, sim = build()
    command = started(station, stir("CMD-R", temp=-15, time=30, rpm=400, position=1))
    station.abort(command)
    assert station.status(command).state == "failed" and "START_4" not in sim.plates["1"].log


def test_abort_after_the_step_finished_is_too_late(build):
    """作业已经做完、正在收尾（回待机）时来的终止：如实拒绝，原作业照报完成。"""
    station, sim = build(chiller={"after": "standby", "standby_c": 20})
    gate = threading.Event()
    original = station._after
    station._after = lambda run: (gate.wait(5), original(run))
    command = started(station, job("CMD-L", temp=15, time=0.1))
    until(lambda: station.runs[command.handle].outcome == "done")
    threading.Timer(0.3, gate.set).start()
    with pytest.raises(Rejected, match="来不及终止"):
        station.abort(command)
    assert station.status(command).state == "done"


def test_abort_through_the_gateway_confirms_only_after_the_motors_stopped(build, tmp_path):
    station, sim = build(state_dir=tmp_path / "state")
    server, _ = _serve(station, tmp_path)
    try:
        body = {"command_id": "CMD-GA", "capability": STIR, "params": {"temp": 15, "time": 60, "rpm": 400, "position": 4}}
        assert server.gateway.submit(body)["state"] == "running"
        until(lambda: spinning(sim, "4"))
        receipt = server.gateway.control("abort", "CMD-GA-ABORT", {"target_command_id": "CMD-GA"})
        assert receipt["state"] == "done" and not spinning(sim, "4")
        assert server.gateway.query("CMD-GA")["state"] == "failed"
    finally:
        server.stop()


# ---------- 运行中出事 ----------

def test_chiller_alarm_during_the_run_fails_it_and_stops_the_motors(build):
    station, sim = build(chiller={"after": "standby", "standby_c": 20})
    command = started(station, stir("CMD-AL", temp=15, time=30, rpm=400, position=1))
    until(lambda: spinning(sim, "1"))
    sim.chiller.set_alarm()
    final = finished(station, command)
    assert final.state == "failed" and "Huber 报错 -1331" in final.error and not spinning(sim, "1")
    assert sim.bath.snapshot()["setpoint"] == 15, "冷水机报警：不再给它发命令（不改待机温度）"


@pytest.mark.parametrize(("event", "fragment"), [
    ("setpoint", "设定值变成了 12 ℃"), ("stopped", "在作业途中停了"), ("restart", "重启过"),
])
def test_changes_at_the_panel_fail_the_run(build, event, fragment):
    station, sim = build()
    command = started(station, job("CMD-P", temp=15, time=30))
    until(lambda: station.runs[command.handle].phase == "holding")
    with sim.bath.lock:
        if event == "setpoint":
            sim.bath.setpoint = 12.0
        elif event == "stopped":
            sim.bath.running = False
        else:
            sim.chiller.restart()
    final = finished(station, command)
    assert final.state == "failed" and fragment in final.error, final.error


def test_julabo_switched_to_panel_control_mid_run_fails_and_says_standby_was_refused(build):
    """Julabo 在作业途中被切到面板控制：远程命令不再执行，作业判失败；回待机温度冷水机不收，重试也没用——写进结论。"""
    station, sim = build("julabo", chiller={"after": "standby", "standby_c": 20})
    command = started(station, job("CMD-J", temp=15, time=30))
    until(lambda: station.runs[command.handle].phase == "holding")
    sim.chiller.remote = False
    final = finished(station, command)
    assert final.state == "failed" and "切到了面板控制模式" in final.error
    assert "没接受待机温度 20 ℃" in final.error and sim.bath.snapshot()["setpoint"] == 15
    assert not station.bath_owner, "结论出了：冷浴放出来（下一条指令会被面板控制模式拒绝）"


def test_losing_the_chiller_mid_run_fails_after_lost_after_sec(build):
    station, sim = build(link_timeout=0.1, lost_after_sec=0.5)
    command = started(station, job("CMD-LOST", temp=15, time=30))
    until(lambda: station.runs[command.handle].phase == "holding")
    sim.chiller.mute = True
    status = until(lambda: (s := station.status(command)).error and s)
    assert status.state == "running" and "读不到" in status.error, "刚读不到只提示，不判失败"
    final = finished(station, command, timeout=8)
    assert final.state == "failed" and "失去监视" in final.error, "连续读不到超过 lost_after_sec：判失败"
    sim.chiller.mute = False


def test_losing_a_plate_mid_run_fails_that_bottle_only(build):
    station, sim = build(auto_position=False, link_timeout=0.1, lost_after_sec=0.4)
    command = started(station, stir("CMD-M", {"A1": {"position": 1}, "A2": {"position": 2}}, temp=15, time=2.0, rpm=300))
    until(lambda: spinning(sim, "1") and spinning(sim, "2"))
    sim.plates["1"].mute = True
    until(lambda: station.runs[command.handle].bottles["A1"].failed)
    sim.plates["1"].mute = False
    until(lambda: not spinning(sim, "1"))
    assert spinning(sim, "2"), "别的瓶照常搅到点"
    final = finished(station, command)
    assert final.state == "failed" and "失去监视" in final.error
    assert "error" in final.actuals["wells"]["A1"] and "error" not in final.actuals["wells"]["A2"]
    assert final.actuals["wells"]["A2"]["duration_s"] == pytest.approx(2.0, abs=0.3)


# ---------- 启动写出去了却没确认 ----------

def test_start_command_without_a_reply_is_unknown_and_the_chiller_is_stopped_again(build):
    """{M140001 冷水机收到了、应答丢了：不知道开没开，先停下、改回原设定值，再按结果未知抛出（不报「没动」）。"""
    station, sim = build(link_timeout=0.3)

    def lose_start_reply(line, original):
        reply = original(line)
        return None if line == "{M140001" else reply

    _wrap(sim.chiller, "answer", lose_start_reply)
    command = job("CMD-UNK", temp=5, time=1)
    with pytest.raises(RuntimeError) as caught:
        station.start(command)
    assert not isinstance(caught.value, Rejected)
    message = str(caught.value)
    assert "结果未知" in message and "已发停止并确认停下" in message and "设定值已改回 22 ℃" in message, message
    bath = sim.bath.snapshot()
    assert not bath["running"] and bath["setpoint"] == pytest.approx(22)
    assert station.lookup(command) is None, "启动没做完：不认，交人核查"
    del sim.chiller.answer  # 线好了
    assert not station.bath_owner and started(station, job("CMD-NEXT", temp=18, time=0.1)).handle == "CMD-NEXT"


def test_setpoint_without_a_reply_on_a_running_chiller_is_unknown(build):
    station, sim = build(link_timeout=0.3)
    assert finished(station, started(station, job("CMD-0", temp=18, time=0.1))).state == "done"
    # 5 ℃（{M0001F4）冷水机收下了、应答丢了；改回 18 ℃ 的那条照常回
    _wrap(sim.chiller, "answer", lambda line, original: (original(line), None)[1] if line == "{M0001F4"
          else original(line))
    with pytest.raises(RuntimeError) as caught:
        station.start(job("CMD-1", temp=5, time=1))
    assert not isinstance(caught.value, Rejected), "冷水机开着：设定值一改就在动，不能报「没动」"
    assert "结果未知" in str(caught.value) and "设定值已改回 18 ℃" in str(caught.value), str(caught.value)
    bath = sim.bath.snapshot()
    assert bath["running"] and bath["setpoint"] == pytest.approx(18)


def test_setpoint_without_a_reply_on_a_stopped_chiller_is_a_rejection(build):
    """冷水机关着、设定值写出去没应答：冷水机没动（没开控温），照样拒绝；设定值改回原来的。"""
    station, sim = build(link_timeout=0.3)
    _wrap(sim.chiller, "answer", lambda line, original: (original(line), None)[1] if line == "{M00FE0C"
          else original(line))
    with pytest.raises(Rejected) as caught:
        station.start(job("CMD-X", temp=-5, time=1))
    assert caught.value.kind == "busy" and "原来没开着，设备没动" in caught.value.message
    bath = sim.bath.snapshot()
    assert not bath["running"] and bath["setpoint"] == pytest.approx(22)


# ---------- 模拟故障、回执丢失 ----------

def test_lost_receipt_starts_the_job_and_lookup_finds_it(build):
    station, sim = build()
    station.faults.set_fault("lost_receipt")
    command = job("CMD-L", temp=18, time=0.2)
    with pytest.raises(ReceiptLost) as lost:
        station.start(command)
    assert lost.value.handle == "CMD-L" and sim.bath.snapshot()["running"] and station.faults.motions == 1
    assert station.lookup(command) == "CMD-L" and station.lookup(job("CMD-NEVER")) is None


def test_simulated_fail_and_stuck(build):
    station, sim = build()
    station.faults.set_fault("fail")
    failing = started(station, stir("CMD-F", temp=18, time=0.2, rpm=300, position=1))
    final = finished(station, failing)
    assert final.state == "failed" and "模拟故障" in final.error and not spinning(sim, "1")
    station.faults.set_fault("stuck")
    stuck = started(station, job("CMD-K", temp=18, time=0.1))
    until(lambda: station.runs[stuck.handle].phase == "holding")
    time.sleep(0.4)
    assert station.status(stuck).state == "running", "一直不结束"
    station.abort(stuck)
    assert station.status(stuck).state == "failed"


# ---------- 网关重启、停服务、作业记录 ----------

def test_gateway_restart_still_answers_by_command_id(build, tmp_path):
    """作业跑完时没人来查、网关随后重启：按原指令号照样答得上完成与实测值；重投不会让冷水机再动一次。"""
    station, sim = build(state_dir=tmp_path / "state")
    server, _ = _serve(station, tmp_path)
    body = {"command_id": "CMD-1", "capability": STIR, "method": {"program": "STIR-COLD"},
            "params": {"temp": 15, "time": 0.3, "wells": {"A1": {"rpm": 300}, "A2": {"rpm": 500}}}}
    assert server.gateway.submit(body)["state"] == "running" and station.faults.motions == 1
    assert station.runs["CMD-1"].over.wait(8), "后台线程收尾、结论写进作业记录"
    server.stop()
    station.close(stop=False)  # 进程崩掉：不动设备
    again = sim.station(state_dir=tmp_path / "state", faults=station.faults)
    restarted, _ = _serve(again, tmp_path)
    try:
        receipt = restarted.gateway.query("CMD-1")
        assert receipt["state"] == "done", receipt
        wells = receipt["delivered"]["wells"]
        assert wells["A2"]["rpm"] == pytest.approx(500, abs=10) and wells["A1"]["temp"] == pytest.approx(15, abs=0.5)
        assert restarted.gateway.submit(body)["state"] == "done" and station.faults.motions == 1, "重投回放台账，不再动设备"
    finally:
        restarted.stop()
        again.close()


def test_gateway_restart_mid_run_stops_the_motors_and_fails_the_step(build, tmp_path):
    """网关在作业途中重启：不续做——先停板、按 after 处理冷水机，再判失败（写明计划），不留一块没人计时的板。"""
    station, sim = build(state_dir=tmp_path / "state", chiller={"after": "standby", "standby_c": 20})
    server, _ = _serve(station, tmp_path)
    body = {"command_id": "CMD-2", "capability": STIR, "params": {"temp": 15, "time": 60, "rpm": 400, "position": 2}}
    assert server.gateway.submit(body)["state"] == "running"
    until(lambda: spinning(sim, "2"))
    server.stop()
    station.close(stop=False)
    assert spinning(sim, "2"), "网关没了，板子还在转"
    again = sim.station(state_dir=tmp_path / "state", faults=station.faults)
    restarted, _ = _serve(again, tmp_path)
    try:
        until(lambda: not spinning(sim, "2"))
        receipt = until(lambda: (r := restarted.gateway.query("CMD-2"))["state"] == "failed" and r)
        assert "网关重启时作业还没结束" in receipt["error"] and "15 ℃ 制冷搅拌 1 瓶" in receipt["error"]
        assert sim.bath.snapshot()["setpoint"] == 20, "重启后按 after 回到待机温度"
        assert restarted.gateway.submit(body)["state"] == "failed" and station.faults.motions == 1
        assert again.start(stir("CMD-3", temp=15, time=0.2, rpm=300, position=2)), "位置 2、冷浴都空出来了"
    finally:
        restarted.stop()
        again.close()


def test_gateway_restart_after_the_motor_start_was_sent_but_not_recorded(build, tmp_path):
    """记录停在「开始搅拌」、启动命令发了却没来得及记下：重启后照样把要搅的板停一遍。"""
    station, sim = build(state_dir=tmp_path / "state")
    command = started(station, stir("CMD-S", temp=15, time=60, rpm=400, position=1))
    until(lambda: spinning(sim, "1"))
    station.close(stop=False)
    path = station._path(command.handle)
    record = json.loads(path.read_text(encoding="utf-8"))
    for bottle in record["bottles"].values():
        bottle.update(moved=False, wall_started=0)
    path.write_text(json.dumps(record), encoding="utf-8")
    again = sim.station(state_dir=tmp_path / "state", faults=station.faults)
    try:
        until(lambda: not spinning(sim, "1"))
        assert finished(again, command).state == "failed"
    finally:
        again.close()


def test_gateway_shutdown_stops_running_motors(build, tmp_path):
    station, sim = build(state_dir=tmp_path / "state")
    command = started(station, stir("CMD-SD", temp=15, time=60, rpm=300, position=1))
    until(lambda: spinning(sim, "1"))
    station.close()  # SIGTERM：先停板
    assert not spinning(sim, "1")
    again = sim.station(state_dir=tmp_path / "state")
    try:
        status = again.status(command)
        assert status.state == "failed" and "网关停止服务" in status.error
    finally:
        again.close()


def test_crash_during_start_is_recovered_and_never_claimed(build, tmp_path):
    """网关在启动途中崩掉（记录还停在 starting）：重启后按 after 处理冷水机、判失败；不认这个作业，留给人核查。"""
    station, sim = build(state_dir=tmp_path / "state", chiller={"after": "standby", "standby_c": 20})
    command = started(station, job("CMD-C", temp=10, time=60))
    station.close(stop=False)
    path = station._path(command.handle)
    record = json.loads(path.read_text(encoding="utf-8"))
    record.update(state="starting", started_ok=False)
    path.write_text(json.dumps(record), encoding="utf-8")
    again = sim.station(state_dir=tmp_path / "state", faults=station.faults)
    try:
        assert again.lookup(command) is None
        status = finished(again, command)
        assert status.state == "failed" and "网关在启动途中重启" in status.error
        assert sim.bath.snapshot()["setpoint"] == 20
    finally:
        again.close()


def test_run_record_that_cannot_be_written_is_a_rejection(build, tmp_path):
    station, sim = build(state_dir=tmp_path / "state")
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    station.store = blocker / "runs"  # 写不进去（比如盘满、权限不对）
    with pytest.raises(Rejected) as caught:
        station.start(job("CMD-X", temp=5, time=1))
    assert caught.value.kind == "busy" and "没有记录就不动设备" in caught.value.message
    assert not sim.bath.snapshot()["running"] and station.faults.motions == 0 and not station.bath_owner


def test_gateway_runs_a_chilled_stir_command_end_to_end(build, tmp_path):
    station, sim = build(auto_position=False)
    server, _ = _serve(station, tmp_path)
    try:
        body = {"command_id": "CMD-E2E", "capability": STIR, "method": {"program": "STIR-CHILL"},
                "params": {"temp": 10, "time": 0.3, "rpm": 400,
                           "wells": {"A1": {"position": 1.0}, "A2": {"position": "2", "rpm": 600}}}}
        assert server.gateway.submit(body)["state"] == "running"
        receipt = until(lambda: (r := server.gateway.query("CMD-E2E"))["state"] == "done" and r)
        assert set(receipt["delivered"]["wells"]) == {"A1", "A2"}
        assert server.gateway.submit(body)["state"] == "done" and station.faults.motions == 1, "重投回放，不再启动"
    finally:
        server.stop()


# ---------- 身份与配置 ----------

@pytest.mark.parametrize("kind", KINDS)
def test_identity_reports_the_chiller_the_positions_and_simulator(build, kind):
    station, sim = build(kind, link_timeout=0.2)
    sim.plates["2"].mute = True
    identity = station.identity()
    assert identity["simulator"] is True and identity["channels"] == 4 and identity["chiller"]["kind"] == kind
    assert identity["chiller"]["range_c"] == [-20, 25] and identity["accepts_commands"] is True
    assert [row["reachable"] for row in identity["positions"]] == [True, False, True, True]
    assert identity["commands"] == ["dispatch", "retry", "abort", "query"], "不做保持、也就没有续跑"
    assert {row["capability"] for row in identity["methods"]} == {THERMOSTAT, STIR}
    if kind == "huber":
        assert identity["serial"] == "23456789"
    else:
        assert identity["firmware"] in {"JULABO CF41 VERSION 1.30", "V2.30"}


def test_config_problems_are_reported_together():
    with pytest.raises(ValueError) as caught:
        Config.parse({"chiller": {"kind": "haake", "link": {"kind": "serial", "port": "COM3"}},
                      "stirrers": {"1": {"link": {"kind": "usb"}}}, "programs": {}, "default_program": "X",
                      "tempo": 1})
    message = str(caught.value)
    for fragment in ("device_id", "huber、julabo 或 lauda", "位置 1 的 link", "programs 是空的", "default_program",
                     "不认识的配置项 tempo"):
        assert fragment in message, fragment
    with pytest.raises(ValueError) as caught:
        Config.parse({"device_id": "X", "chiller": {"kind": "julabo", "link": {"kind": "tcp", "host": "h"},
                                                    "min_c": 10, "max_c": -10, "after": "standby"},
                      "programs": {"S": {"action": "stir"}}})
    message = str(caught.value)
    for fragment in ("TCP 链路要写 host 与 port", "min_c 10 要低于 max_c -10", "要写 standby_c", "没有这个动作"):
        assert fragment in message, fragment


def test_capabilities_and_stirrers_must_agree():
    base = {"device_id": "X", "chiller": {"kind": "lauda", "link": {"kind": "tcp", "host": "h"}, "min_c": -20,
                                          "max_c": 25}, "programs": {"C": {}}}
    assert Config.parse(base).chiller.link["port"] == 54321
    with pytest.raises(ValueError, match="没配 stirrers"):
        Config.parse({**base, "capabilities": {"thermostat": THERMOSTAT, "stir": STIR}})
    with pytest.raises(ValueError, match="capabilities 里却没有 stir"):
        Config.parse({**base, "capabilities": {"thermostat": THERMOSTAT},
                      "stirrers": {"1": {"link": {"kind": "tcp", "host": "h", "port": 4001}}}})


def test_program_without_a_method_falls_back_per_action():
    config = Config.parse(json.loads((Path(__file__).resolve().parents[1] / "config.example.json").read_text("utf-8")))
    assert config.program_for("thermostat", "").code == "CHILL-CHECK", "没带方法：default_program"
    assert config.program_for("stir", "").code == "STIR-CHILL", "动作对不上 default_program：这个动作的第一个程序"
    assert config.program_for("stir", "CHILL-CHECK") is None


def test_example_and_simulator_configs_parse():
    module = Path(__file__).resolve().parents[1]
    example = Config.load(module / "config.example.json")
    assert example.keys == ("1", "2", "3", "4") and example.chiller.kind == "huber" and not example.auto_position
    assert example.chiller.link["baudrate"] == 9600 and example.positions["1"].link["bytesize"] == 7
    for kind in KINDS:
        station, sim = simulated_station(module / "simulator" / "thermostat-sim.json", kind=kind)
        try:
            assert station.config.device_id == "SIM-CHILL-01" and station.config.auto_position
            assert station.config.chiller.kind == kind and station.identity()["simulator"] is True
            assert set(station.config.capabilities.values()) == {THERMOSTAT, STIR}
        finally:
            station.close()
            sim.stop()


def test_profile_matches_the_module():
    """profile.json 改过之后要重算摘要；支持标志、验收参数要和模块一致（验收参数网关得认、在样例配置的范围里）。"""
    import sys

    from ilcs_gateway.testing import _ilcs_api

    from driver.device import PARAMETERS

    api = str(_ilcs_api())
    if api not in sys.path:
        sys.path.insert(0, api)
    from app.services.template_service import template_check, template_digest

    module = Path(__file__).resolve().parents[1]
    profile = json.loads((module / "profile.json").read_text(encoding="utf-8"))
    assert profile["digest"] == template_digest(profile), "profile.json 改过之后要重算摘要"
    assert template_check(profile)["ok"] and profile["supports"] == SUPPORTS
    assert profile["code"] == "TPL-THERMOSTAT" and profile["state"] == "draft"
    params = profile["acceptance"]["params"]
    assert profile["acceptance"]["capability"] == THERMOSTAT and set(params) <= set(PARAMETERS["thermostat"])
    example = Config.load(module / "config.example.json")
    assert example.chiller.min_c <= params["temp"] <= example.chiller.max_c, "验收温度在样例配置的范围里"
    assert copy.deepcopy(profile["connection"])["expected_device_id"] == "<设备编号>"
