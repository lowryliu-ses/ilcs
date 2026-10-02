"""设备模块自测：对假光谱仪跑 ILCS 的接入验收清单（含故障项目），再测驱动自己的判断与回报的谱图。CI 里必须全过。

    pytest devices/gateway/raman-seabreeze/tests
"""
from __future__ import annotations

import json
from pathlib import Path
import statistics
import sys
import time

import pytest

from ilcs_gateway import Job, ReceiptLost, Rejected, serve
from ilcs_gateway.testing import _ilcs_api, acceptance

from driver.config import Config
from driver.device import SATURATION, Instrument
from driver.spectro_api import SpectrometerError
from simulator import simulated_instrument
from simulator.fake_spectrometer import FakeSpectrometer, default_config

CONFIG = Config.parse(default_config())
DEVICE_ID = CONFIG.device_id
CAPABILITY = CONFIG.capability
SUPPORTS = {"hold": False, "abort": True, "query": True, "dedup": True}
# ILCS 侧给谱图建的曲线指标规则（README「ILCS 里怎么建」）
CURVE_RULES = {"x_label": "拉曼位移", "x_unit": "cm-1"}
OUTPUTS = [
    {"key": "spectrum", "label": "拉曼谱", "unit": "counts", "kind": "series", "required": True},
    {"key": "max_counts", "label": "最高计数", "unit": "counts", "hi": SATURATION * 65535, "required": True},
]


def instrument(state_dir: Path | None = None, *, time_scale: float = 0.001, fake: FakeSpectrometer | None = None,
               **changes) -> tuple[Instrument, FakeSpectrometer]:
    """直接对驱动测（不经网关）。缺省一张谱几毫秒；repeats 放宽到 10 次。"""
    config = Config.parse({**default_config(), "max_repeats": 10, **changes})
    fake = fake or FakeSpectrometer(laser_nm=config.laser_nm, time_scale=time_scale)
    return Instrument(fake, config, state_dir=state_dir), fake


def job(command_id: str, program: str = "RAMAN", **params) -> Job:
    return Job(command_id=command_id, capability=CAPABILITY, params=params, method={"program": program})


def run(device: Instrument, command: Job, timeout: float = 10.0):
    """启动、等到出结论。"""
    command.handle = device.start(command)
    deadline = time.monotonic() + timeout
    status = device.status(command)
    while status.state == "running" and time.monotonic() < deadline:
        time.sleep(0.005)
        status = device.status(command)
    return status


def value_at(spectrum: dict, shift: float) -> float:
    return min(zip(spectrum["x"], spectrum["y"]), key=lambda point: abs(point[0] - shift))[1]


def noise(spectrum: dict) -> float:
    """1850–2000 cm-1 没有峰：相邻点之差的标准差（差分去掉了缓变的基线）。"""
    flat = [y for x, y in zip(spectrum["x"], spectrum["y"]) if 1850 <= x <= 2000]
    return statistics.pstdev([b - a for a, b in zip(flat, flat[1:])])


def ilcs_domain():
    """ILCS 自己的曲线校验与输出核对（api/app/domain，纯函数，不连库）。"""
    api = str(_ilcs_api())
    if api not in sys.path:
        sys.path.insert(0, api)
    from app.domain import dataquality, metrics, series

    return series, metrics, dataquality


@pytest.fixture()
def gateway(tmp_path: Path):
    secrets = tmp_path / "secrets"
    # 采一张谱 0.5 s（积分 1 s × 0.5）：验收的终止项目要在采谱途中打断它
    fake = FakeSpectrometer(time_scale=0.5)
    server = serve(Instrument(fake, CONFIG, state_dir=tmp_path / "state"), device_id=DEVICE_ID,
                   state_dir=tmp_path / "state", address="127.0.0.1", port=0, token_file=secrets / f"{DEVICE_ID}.token",
                   cert=secrets / f"{DEVICE_ID}.crt", key=secrets / f"{DEVICE_ID}.key", host_name="localhost")
    try:
        yield server, fake, secrets
    finally:
        server.stop()


# ---------- ILCS 接入验收、网关重启 ----------

def test_module_passes_the_ilcs_acceptance_checklist(gateway, tmp_path):
    server, fake, secrets = gateway
    report = acceptance(
        f"https://localhost:{server.port}/api/v1", token_file=secrets / f"{DEVICE_ID}.token",
        ca_file=secrets / f"{DEVICE_ID}.crt", capability=CAPABILITY, params={"repeats": 1},
        expected_device_id=DEVICE_ID, state_root=tmp_path / "ilcs", supports=SUPPORTS,
    )
    states = {check.key: check.state for check in report.checks}
    assert report.ok, report.markdown()
    assert states.pop("hold") == "skip", "采谱停不在半截，契约如实声明不做保持"
    assert all(state == "pass" for state in states.values()), states
    assert report.simulator and report.identity["methods"], "方法目录由网关配置的程序自报"


def test_gateway_restart_still_answers_by_command_id(gateway, tmp_path):
    """网关进程重启：台账与采谱结论都在盘上，按原指令号照样查得到谱图；同一指令号再投不会再采一次。"""
    server, fake, secrets = gateway
    body = {"command_id": "CMD-1", "capability": CAPABILITY, "params": {"repeats": 1}}
    assert server.gateway.submit(body)["state"] == "running" and fake.faults.motions == 1
    deadline = time.monotonic() + 10
    while server.gateway.device.runs["CMD-1"].outcome is None and time.monotonic() < deadline:
        time.sleep(0.02)  # 等它采完；没人来查，台账里还是 running，结论只在状态目录里
    assert server.gateway.ledger.find("CMD-1")["state"] == "running"
    server.stop()
    again = serve(Instrument(fake, CONFIG, state_dir=tmp_path / "state"), device_id=DEVICE_ID,
                  state_dir=tmp_path / "state", address="127.0.0.1", port=0, token_file=secrets / f"{DEVICE_ID}.token",
                  cert=secrets / f"{DEVICE_ID}.crt", key=secrets / f"{DEVICE_ID}.key", host_name="localhost")
    try:
        receipt = again.gateway.query("CMD-1")
        assert receipt["state"] == "done", receipt
        assert len(receipt["delivered"]["spectrum"]["x"]) > 500 and receipt["delivered"]["repeats"] == 1
        replay = again.gateway.submit(body)
        assert replay["state"] == "done" and fake.faults.motions == 1 and fake.scans == 1, "重投回放台账，不再采谱"
    finally:
        again.stop()


def test_a_job_unfinished_before_a_restart_is_failed_not_running_forever(tmp_path):
    """重启前还没采完：进程没了采谱也就停了、没有谱图。判失败（可以重测），不一直报在采。"""
    device, _ = instrument(tmp_path / "state")
    orphan = job("CMD-OLD")
    orphan.handle = "CMD-OLD"
    status = device.status(orphan)
    assert status.state == "failed" and "网关重启前" in status.error
    assert device.lookup(orphan) is None


# ---------- 拒绝：设备确实没动 ----------

@pytest.mark.parametrize(("capability", "params", "program", "kind"), [
    ("cap.not_this_device", {"repeats": 1}, "RAMAN", "unsupported"),
    (CAPABILITY, {"repeats": 1}, "UNKNOWN-PROGRAM", "invalid"),
    (CAPABILITY, {"repeats": 1, "laser_power": 50}, "RAMAN", "invalid"),    # 不认的参数不悄悄忽略
    (CAPABILITY, {"repeats": 0}, "RAMAN", "invalid"),
    (CAPABILITY, {"repeats": 11}, "RAMAN", "invalid"),                      # max_repeats 10
    (CAPABILITY, {"repeats": 2.5}, "RAMAN", "invalid"),
    (CAPABILITY, {"repeats": True}, "RAMAN", "invalid"),
    (CAPABILITY, {"repeats": "3"}, "RAMAN", "invalid"),
    (CAPABILITY, {"integration_ms": 20000}, "RAMAN", "invalid"),            # 超过 max_integration_ms
    (CAPABILITY, {"integration_ms": 0}, "RAMAN", "invalid"),
    (CAPABILITY, {"integration_ms": 2}, "RAMAN", "invalid"),                # 低于光谱仪下限 8 ms
    (CAPABILITY, {"wells": {"A1": {"repeats": 1}, "A2": {"repeats": 1}}}, "RAMAN", "unsupported"),  # 一次一瓶
    (CAPABILITY, {"wells": {"A1": {"repeats": 1, "temp": 25}}}, "RAMAN", "invalid"),
    (CAPABILITY, {"wells": {"A1": {"repeats": 11}}}, "RAMAN", "invalid"),
    (CAPABILITY, {"wells": {}}, "RAMAN", "invalid"),
    (CAPABILITY, {"wells": {"A1": 3}}, "RAMAN", "invalid"),
    (CAPABILITY, {"wells": {"": {"repeats": 1}}}, "RAMAN", "invalid"),
])
def test_driver_rejects_what_the_device_cannot_do(capability, params, program, kind):
    device, fake = instrument()
    with pytest.raises(Rejected) as caught:
        device.start(Job(command_id="CMD-X", capability=capability, params=params, method={"program": program}))
    assert caught.value.kind == kind and fake.faults.motions == 0 and fake.scans == 0


def test_more_than_one_bottle_is_sent_back_to_be_split():
    device, _ = instrument()
    with pytest.raises(Rejected, match="一次只能测一瓶.*按瓶分开下发"):
        device.start(job("CMD-X", wells={"A1": {}, "A2": {}, "A3": {}}))


def test_integration_outside_the_spectrometer_range_is_rejected():
    fake = FakeSpectrometer(limits_us=(10_000, 5_000_000), time_scale=0.001)
    device, _ = instrument(fake=fake, max_integration_ms=60000)
    for value in (6000, 5):
        with pytest.raises(Rejected, match="超出这台光谱仪的范围") as caught:
            device.start(job("CMD-X", integration_ms=value))
        assert caught.value.kind == "invalid"
    assert fake.scans == 0


def test_wrong_laser_wavelength_is_caught_before_measuring():
    """配置写成 532 nm，光谱仪其实是给 785 nm 配的：换算出来的位移全在范围外，开始之前就拒绝。"""
    device, fake = instrument(fake=FakeSpectrometer(laser_nm=785, time_scale=0.001),
                              laser={"kind": "external", "wavelength_nm": 532})
    with pytest.raises(Rejected, match="核对激光波长") as caught:
        device.start(job("CMD-X"))
    assert caught.value.kind == "invalid" and fake.scans == 0


def test_one_measuring_position_one_job_at_a_time(tmp_path):
    device, fake = instrument(tmp_path, time_scale=0.05)  # 一张谱 0.05 s，4 张 0.2 s
    first = job("CMD-A", repeats=4)
    first.handle = device.start(first)
    with pytest.raises(Rejected) as busy:
        device.start(job("CMD-B"))
    assert busy.value.kind == "busy" and "一个测量位" in busy.value.message and fake.faults.motions == 1
    while device.status(first).state == "running":
        time.sleep(0.01)
    assert device.status(first).state == "done"
    assert run(device, job("CMD-B")).state == "done", "上一张采完，测量位放开"


def test_spectrometer_unreadable_before_start_is_a_rejection_not_unknown():
    device, fake = instrument()

    def unplugged():
        raise SpectrometerError("USB 断开")

    fake.integration_limits_us = unplugged
    with pytest.raises(Rejected, match="读不到光谱仪") as caught:
        device.start(job("CMD-X"))
    assert caught.value.kind == "busy" and fake.faults.motions == 0
    del fake.integration_limits_us
    assert run(device, job("CMD-Y")).state == "done", "测量位没有被占着"


def test_interlock_and_busy_faults_are_explicit_rejections():
    device, fake = instrument()
    fake.faults.set_fault("interlock")
    identity = device.identity()
    assert identity["interlock"] and not identity["accepts_commands"]
    with pytest.raises(Rejected) as interlock:
        device.start(job("CMD-A"))
    fake.faults.set_fault("busy")
    with pytest.raises(Rejected) as busy:
        device.start(job("CMD-B"))
    assert interlock.value.kind == "interlocked" and busy.value.kind == "busy"
    assert fake.faults.motions == 0 and fake.scans == 0


# ---------- 谱图 ----------

def test_spectrum_is_raman_shift_ascending_cropped_and_shows_the_pf6_peak(tmp_path):
    device, _ = instrument(tmp_path)
    status = run(device, job("CMD-SPEC", repeats=3))
    assert status.state == "done" and status.error == "", status.error
    delivered = status.actuals
    spectrum = delivered["spectrum"]
    x, y = spectrum["x"], spectrum["y"]
    assert len(x) == len(y) > 500
    assert all(b > a for a, b in zip(x, x[1:])), "x 严格递增"
    low, high = CONFIG.shift_range
    assert low <= x[0] < 170 and 1990 < x[-1] <= high, "裁到 150–2000 cm-1（低端到光谱仪覆盖的 ~160 cm-1 为止）"
    # PF6⁻（P–F 对称伸缩）741 cm-1：附近 ±12 cm-1 里最高的点就在 741 上，比两侧高出一截
    peak_x, peak_y = max(((a, b) for a, b in zip(x, y) if 729 <= a <= 753), key=lambda point: point[1])
    assert abs(peak_x - 741) <= 3
    assert peak_y > 1.5 * max(value_at(spectrum, 729), value_at(spectrum, 753))
    assert abs(x[y.index(max(y))] - 893) <= 3, "最强的是 EC 环呼吸 893 cm-1"
    assert delivered["repeats"] == 3 and delivered["integration_ms"] == 1000 and delivered["laser_nm"] == 785
    assert delivered["saturated"] is False and "note" not in delivered and delivered["program"] == "RAMAN"
    assert 0 < delivered["max_counts"] < SATURATION * 65535
    assert status.telemetry == [{"metric": "max_counts", "value": delivered["max_counts"], "setpoint": None}]


def test_averaging_more_scans_reduces_the_noise(tmp_path):
    device, _ = instrument(tmp_path)
    one = run(device, job("CMD-N1", repeats=1)).actuals["spectrum"]
    nine = run(device, job("CMD-N9", repeats=9)).actuals["spectrum"]
    assert noise(one) > 2 * noise(nine), (noise(one), noise(nine))  # 9 张平均，噪声约降到 1/3


def test_saturated_spectrum_is_still_done_but_flagged(tmp_path):
    device, fake = instrument(tmp_path)
    status = run(device, job("CMD-SAT", integration_ms=5000))
    assert status.state == "done" and status.error == "", "饱和不是失败：错误栏只给失败用"
    delivered = status.actuals
    assert delivered["saturated"] is True and "饱和" in delivered["note"] and "缩短积分时间" in delivered["note"]
    assert delivered["max_counts"] == fake.full_scale


def test_integration_comes_from_the_command_then_the_program_then_the_config(tmp_path):
    device, fake = instrument(tmp_path, integration_ms=500,
                              programs={"RAMAN": {"name": "按缺省积分"}, "FAST": {"name": "快", "integration_ms": 300}})
    assert run(device, job("CMD-1", program="RAMAN")).actuals["integration_ms"] == 500
    assert fake.integration_us == 500_000
    assert run(device, job("CMD-2", program="FAST")).actuals["integration_ms"] == 300
    assert run(device, job("CMD-3", program="FAST", integration_ms=1200)).actuals["integration_ms"] == 1200
    assert fake.integration_us == 1_200_000
    assert run(device, Job(command_id="CMD-4", capability=CAPABILITY, params={})).actuals["program"] == "RAMAN", \
        "没带设备方法（如接入验收）用 default_program"


def test_one_well_reports_under_wells_and_its_params_override_the_step(tmp_path):
    device, fake = instrument(tmp_path)
    status = run(device, job("CMD-W", repeats=1, wells={"B3": {"repeats": 2}}))
    assert status.state == "done" and set(status.actuals) == {"wells"}
    row = status.actuals["wells"]["B3"]
    assert row["repeats"] == 2 and fake.scans == 2 and len(row["spectrum"]["x"]) > 500
    defaults = run(device, job("CMD-W2", repeats=3, wells={"C1": {}})).actuals["wells"]["C1"]
    assert defaults["repeats"] == 3, "孔位没写的参数用步骤顶层的"


def test_too_many_pixels_are_merged_down_to_max_points(tmp_path):
    device, _ = instrument(tmp_path, max_points=200)
    spectrum = run(device, job("CMD-M")).actuals["spectrum"]
    assert 100 < len(spectrum["x"]) <= 200 and all(b > a for a, b in zip(spectrum["x"], spectrum["x"][1:]))


def test_delivered_spectrum_passes_the_ilcs_curve_validator(tmp_path):
    """回报的谱图按 ILCS 自己的口径核对：曲线写法与点数（series.issues）、曲线指标的值校验（metrics.check_value）、
    设备方法输出项核对（dataquality.output_flags）。饱和由 max_counts 的上限打标。"""
    series, metrics, dataquality = ilcs_domain()
    device, _ = instrument(tmp_path)
    single = run(device, job("CMD-V1", repeats=2)).actuals
    assert series.rule_issues(CURVE_RULES) == []
    assert series.issues(single["spectrum"], CURVE_RULES) == []
    assert metrics.check_value("series", "counts", CURVE_RULES, single["spectrum"], "counts") == []
    assert dataquality.output_flags(OUTPUTS, single) == []
    assert series.summary(series.normalize(single["spectrum"])[0])["points"] == len(single["spectrum"]["x"])
    well = run(device, job("CMD-V2", wells={"A1": {"repeats": 2}})).actuals
    assert dataquality.output_flags(OUTPUTS, well) == []
    saturated = run(device, job("CMD-V3", integration_ms=5000)).actuals
    assert [flag["key"] for flag in dataquality.output_flags(OUTPUTS, saturated)] == ["max_counts"]


# ---------- 终止、故障 ----------

def test_abort_stops_between_scans_and_reports_no_spectrum(tmp_path):
    device, fake = instrument(tmp_path, time_scale=0.1)  # 一张谱 0.1 s
    target = job("CMD-AB", repeats=10)
    target.handle = device.start(target)
    time.sleep(0.15)
    started = time.monotonic()
    device.abort(target)
    assert time.monotonic() - started < 0.5, "最多等正在读出的那一张"
    status = device.status(target)
    assert status.state == "failed" and "被终止" in status.error and "spectrum" not in status.actuals
    assert fake.scans < 10
    assert run(device, job("CMD-NEXT")).state == "done", "终止后测量位放开了"
    device.abort(target)  # 已经结束：照样确认，不报错


def test_spectrometer_error_while_measuring_fails_the_job(tmp_path):
    device, fake = instrument(tmp_path)
    original, calls = fake.intensities, []

    def flaky(dark, nonlinearity):
        calls.append(1)
        if len(calls) == 2:
            raise SpectrometerError("USB 读出超时")
        return original(dark, nonlinearity)

    fake.intensities = flaky
    status = run(device, job("CMD-E", repeats=3))
    assert status.state == "failed" and "USB 读出超时" in status.error and "已采 1 / 3 张" in status.error
    del fake.intensities
    assert run(device, job("CMD-OK")).state == "done", "测量位放开了"


def test_dark_correction_on_a_model_without_dark_pixels_fails_with_the_reason(tmp_path):
    device, _ = instrument(tmp_path, fake=FakeSpectrometer(dark_pixels=False, time_scale=0.001),
                           correct_dark_counts=True)
    status = run(device, job("CMD-D"))
    assert status.state == "failed" and "dark count" in status.error


def test_simulated_failure_and_stuck_runs(tmp_path):
    device, fake = instrument(tmp_path)
    fake.faults.set_fault("fail")
    failed = run(device, job("CMD-F"))
    assert failed.state == "failed" and "模拟故障" in failed.error and failed.actuals == {}
    fake.faults.set_fault("stuck")
    stuck = job("CMD-S")
    stuck.handle = device.start(stuck)
    time.sleep(0.1)
    assert device.status(stuck).state == "running", "一直不结束"
    device.abort(stuck)
    assert device.status(stuck).state == "failed" and fake.faults.motions == 2


def test_lost_receipt_still_measures_and_is_found_by_command_id(tmp_path):
    device, fake = instrument(tmp_path)
    fake.faults.set_fault("lost_receipt")
    lost = job("CMD-LOST")
    with pytest.raises(ReceiptLost) as caught:
        device.start(lost)
    assert caught.value.handle == "CMD-LOST" and fake.faults.motions == 1
    assert device.lookup(lost) == "CMD-LOST"
    lost.handle = caught.value.handle
    while device.status(lost).state == "running":
        time.sleep(0.005)
    assert device.status(lost).state == "done"
    fresh, _ = instrument(tmp_path, fake=fake)
    assert fresh.lookup(lost) == "CMD-LOST", "网关重启后按状态目录里的结论找回"


def test_slow_submit_delays_the_reply_but_starts_once(tmp_path):
    device, fake = instrument(tmp_path)
    fake.faults.set_fault("slow_submit", 0.2)
    started = time.monotonic()
    command = job("CMD-SLOW")
    command.handle = device.start(command)
    assert time.monotonic() - started >= 0.2 and fake.faults.motions == 1


# ---------- 身份、配置 ----------

def test_identity_reports_methods_laser_and_no_hold():
    device, _ = instrument()
    identity = device.identity()
    assert identity["simulator"] is True and "ILCS-SIMULATOR" in identity["serial"]
    assert identity["device_id"] == DEVICE_ID and identity["vendor"] == "Ocean Insight" and identity["model"] == "QE Pro"
    assert {method["program"] for method in identity["methods"]} == {"RAMAN", "RAMAN-FAST"}
    assert {method["capability"] for method in identity["methods"]} == {"cap.ely.raman"}
    assert "hold" not in identity["commands"] and "resume" not in identity["commands"]
    assert identity["laser"] == {"kind": "external", "wavelength_nm": 785.0} and identity["pixels"] == 1024
    assert identity["max_intensity"] == 65535.0 and identity["spectrometer_model"] == "QE-PRO"
    assert device.fault_target() is not None
    assert Instrument(object(), CONFIG).fault_target() is None, "真光谱仪没有模拟控制口"


def test_simulated_instrument_follows_the_container_config(tmp_path):
    module = Path(__file__).resolve().parents[1]
    device = simulated_instrument(module / "simulator" / "raman-sim.json", state_dir=tmp_path, time_scale=0.001)
    assert device.config.device_id == "SIM-RAMAN-01" and device.spectrometer.laser_nm == device.config.laser_nm
    assert run(device, job("CMD-SIM", repeats=2)).state == "done"
    example = Config.load(module / "config.example.json")
    assert example.serial and example.default_program in example.programs


def test_profile_matches_the_module_and_would_import():
    """profile.json 按 ILCS 导入时的口径核对：摘要对得上（改过要重算）、能发布，契约与验收缺省和网关一致。"""
    ilcs_domain()  # api/ 进 sys.path
    from app.services.template_service import template_check, template_digest

    profile = json.loads((Path(__file__).resolve().parents[1] / "profile.json").read_text(encoding="utf-8"))
    assert profile["digest"] == template_digest(profile), "profile.json 改过之后要重算摘要"
    assert template_check(profile)["ok"] and profile["state"] == "draft"
    assert profile["supports"] == SUPPORTS and profile["driver"] == "http_json_v1"
    assert profile["acceptance"] == {"capability": CAPABILITY, "params": {"repeats": 1}}


def test_config_problems_are_reported_together():
    with pytest.raises(ValueError) as caught:
        Config.parse({"laser": {"kind": "wasatch"}, "spectrometer": {"backend": "oceandirect", "flush_scans": -1},
                      "integration_ms": 20000, "max_integration_ms": 10000, "max_repeats": 0,
                      "shift_range_cm1": [2000, 150], "max_points": 50000, "correct_dark_counts": "yes",
                      "programs": {"A": {"integration_ms": 1, "power": 5}}, "default_program": "B"})
    message = str(caught.value)
    for fragment in ("device_id", "laser.kind", "wavelength_nm", "backend", "flush_scans", "integration_ms",
                     "max_repeats", "shift_range_cm1", "max_points", "correct_dark_counts", "不认 power",
                     "default_program"):
        assert fragment in message, fragment
