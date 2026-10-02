"""设备模块自测：对假 MethodSCRIPT 仪器（走真的协议代码）跑 ILCS 的接入验收清单（含故障项目），再测驱动的判断规则、
各技术的曲线与派生指标、终止与出错、网关重启。CI 里必须全过。

    pytest devices/gateway/potentiostat/tests
"""
from __future__ import annotations

import json
import math
from pathlib import Path
import sys
import time

import pytest

from ilcs_gateway import Job, ReceiptLost, Rejected, serve
from ilcs_gateway.testing import _ilcs_api, acceptance

from driver.backend import StartUnknown
from driver.config import Config
from driver.device import Instrument
from driver.palmsens import MethodScript
from simulator import CellModel, default_config, simulated_instrument

MODULE = Path(__file__).resolve().parents[1]
CONFIG = Config.parse(default_config())
DEVICE_ID = CONFIG.device_id
CAPABILITY = CONFIG.capability
SUPPORTS = {"hold": False, "abort": True, "query": True, "dedup": True}
# ILCS 侧给各技术建的输出项与曲线指标规则（README「ILCS 里怎么建」）
CURVES = {
    "ocp_curve": ("V", {"x_label": "时间", "x_unit": "s"}),
    "nyquist": ("Ω", {"x_label": "Z'", "x_unit": "Ω"}),
    "bode": ("", {"x_label": "频率", "x_unit": "Hz"}),
    "lsv": ("mA/cm2", {"x_label": "电位", "x_unit": "V"}),
    "cv": ("mA/cm2", {"x_label": "电位", "x_unit": "V"}),
    "ca_curve": ("mA/cm2", {"x_label": "时间", "x_unit": "s"}),
}
OUTPUTS = {
    "ocp": [{"key": "ocp_curve", "unit": "V", "kind": "series", "required": True},
            {"key": "ocp_V", "unit": "V", "required": True}],
    "eis": [{"key": "nyquist", "unit": "Ω", "kind": "series", "required": True},
            {"key": "r_bulk_ohm", "unit": "Ω", "required": True},
            {"key": "conductivity_mS_cm", "unit": "mS/cm", "lo": 1, "hi": 30, "required": True}],
    "lsv": [{"key": "lsv", "unit": "mA/cm2", "kind": "series", "required": True},
            {"key": "onset_potential_V", "unit": "V"}],
    "cv": [{"key": "cv", "unit": "mA/cm2", "kind": "series", "required": True},
           {"key": "jpa_mA_cm2", "unit": "mA/cm2"}, {"key": "jpc_mA_cm2", "unit": "mA/cm2"}],
    "ca": [{"key": "ca_curve", "unit": "mA/cm2", "kind": "series", "required": True},
           {"key": "i_end_mA", "unit": "mA", "required": True}],
}
QUALITY = {"key": "overload_points", "label": "过载点数", "hi": 0}


def ilcs_domain():
    """ILCS 自己的曲线校验与输出核对（api/app/domain，纯函数，不连库）。"""
    api = str(_ilcs_api())
    if api not in sys.path:
        sys.path.insert(0, api)
    from app.domain import dataquality, metrics, series

    return series, metrics, dataquality


@pytest.fixture()
def make(tmp_path):
    """直接对驱动测（不经网关）：起一台假仪器（缺省不等，出点不睡），网关配置可以改。用完都关掉。"""
    created = []

    def build(*, time_scale: float = 0.0, cell_model: CellModel | None = None, device_type: str = "es4_hr",
              state: str | None = "state", **changes):
        config = {**default_config(), **changes}
        device, simulation = simulated_instrument(state_dir=tmp_path / state if state else None, time_scale=time_scale,
                                                  cell=cell_model, device_type=device_type, config=config)
        created.append((device, simulation))
        return device, simulation.fake

    yield build
    for device, simulation in created:
        device.close()
        simulation.stop()


def job(command_id: str, program: str = "OCP-10", **params) -> Job:
    return Job(command_id=command_id, capability=CAPABILITY, params=params, method={"program": program})


def wait(device: Instrument, command: Job, timeout: float = 20.0):
    deadline = time.monotonic() + timeout
    status = device.status(command)
    while status.state == "running" and time.monotonic() < deadline:
        time.sleep(0.005)
        status = device.status(command)
    return status


def run(device: Instrument, command: Job, timeout: float = 20.0):
    """启动、等到出结论。"""
    command.handle = device.start(command)
    return wait(device, command, timeout)


@pytest.fixture()
def gateway(tmp_path):
    secrets = tmp_path / "secrets"
    # OCP-10 是 20 个点 × 0.5 s × 0.1 = 1 s：验收的终止项目要在测量途中打断它
    device, simulation = simulated_instrument(state_dir=tmp_path / "state", time_scale=0.1)
    server = serve(device, device_id=DEVICE_ID, state_dir=tmp_path / "state", address="127.0.0.1", port=0,
                   token_file=secrets / f"{DEVICE_ID}.token", cert=secrets / f"{DEVICE_ID}.crt",
                   key=secrets / f"{DEVICE_ID}.key", host_name="localhost")
    try:
        yield server, device, simulation, secrets
    finally:
        server.stop()
        device.close()
        simulation.stop()


# ---------- ILCS 接入验收、网关重启 ----------

def test_module_passes_the_ilcs_acceptance_checklist(gateway, tmp_path):
    server, device, simulation, secrets = gateway
    report = acceptance(
        f"https://localhost:{server.port}/api/v1", token_file=secrets / f"{DEVICE_ID}.token",
        ca_file=secrets / f"{DEVICE_ID}.crt", capability=CAPABILITY, params={}, expected_device_id=DEVICE_ID,
        state_root=tmp_path / "ilcs", supports=SUPPORTS,
    )
    states = {check.key: check.state for check in report.checks}
    assert report.ok, report.markdown()
    assert states.pop("hold") == "skip", "测量停不在半截，契约如实声明不做保持"
    assert all(state == "pass" for state in states.values()), states
    assert report.simulator and report.identity["methods"], "方法目录由网关配置的程序自报"
    aborted = [run_ for run_ in simulation.fake.runs if run_["aborted"]]
    assert aborted and simulation.fake.cell_on is False, "验收的终止项目真的让仪器停下了（Z），电池断开"


def test_gateway_restart_still_answers_by_command_id(gateway, tmp_path):
    """网关进程重启：台账与测量结论都在盘上，按原指令号照样查得到曲线；同一指令号再投不会再测一次。"""
    server, device, simulation, secrets = gateway
    body = {"command_id": "CMD-1", "capability": CAPABILITY, "params": {}}
    assert server.gateway.submit(body)["state"] == "running" and device.faults.motions == 1
    deadline = time.monotonic() + 10
    while device.runs["CMD-1"].outcome is None and time.monotonic() < deadline:
        time.sleep(0.02)  # 等它测完；没人来查，台账里还是 running，结论只在状态目录里
    assert server.gateway.ledger.find("CMD-1")["state"] == "running"
    server.stop()
    restarted = Instrument(MethodScript(CONFIG.link | {"port": simulation.server.port}, timeout=CONFIG.timeout_sec),
                           device.config, state_dir=tmp_path / "state", faults=device.faults)
    again = serve(restarted, device_id=DEVICE_ID, state_dir=tmp_path / "state", address="127.0.0.1", port=0,
                  token_file=secrets / f"{DEVICE_ID}.token", cert=secrets / f"{DEVICE_ID}.crt",
                  key=secrets / f"{DEVICE_ID}.key", host_name="localhost")
    try:
        receipt = again.gateway.query("CMD-1")
        assert receipt["state"] == "done", receipt
        assert len(receipt["delivered"]["ocp_curve"]["x"]) == 20 and receipt["delivered"]["program"] == "OCP-10"
        runs = len(simulation.fake.runs)
        replay = again.gateway.submit(body)
        assert replay["state"] == "done" and device.faults.motions == 1, "重投回放台账，不再测"
        assert len(simulation.fake.runs) == runs
    finally:
        again.stop()
        restarted.close()


def test_a_measurement_cut_off_by_a_restart_fails_only_after_the_instrument_is_stopped(make, tmp_path):
    """网关在测量途中崩了：仪器上的脚本还在跑（MethodSCRIPT 是仪器自己执行的）。重启后连上仪器之前不下结论；
    连上时先同步（Z），停掉那次测量、电池断开，才判失败。"""
    old, fake = make(time_scale=0.02, state="crashed")  # OCP-60：60 点 × 1 s × 0.02 = 1.2 s
    orphan = job("CMD-ORPHAN", "OCP-60")
    orphan.handle = old.start(orphan)
    time.sleep(0.1)
    old.closing.set()  # 「崩了」：老进程不再重连、不再读
    fake.drop()
    assert fake.running, "仪器照跑，电池还加着电位"
    restarted = Instrument(MethodScript(old.config.link, timeout=1.0), old.config, state_dir=tmp_path / "state")
    with pytest.raises(RuntimeError, match="还没连上仪器"):
        restarted.status(orphan)  # 不知道仪器停没停：不下结论（SDK 照报台账里的 running）
    restarted.identity()  # 健康检查连上仪器：同步
    status = restarted.status(orphan)
    assert status.state == "failed" and "网关重启前" in status.error and "已停掉" in status.error
    assert not fake.running and fake.runs[-1]["aborted"] is True and fake.cell_on is False
    assert restarted.lookup(orphan) is None
    restarted.close()


# ---------- 拒绝：仪器确实没动 ----------

@pytest.mark.parametrize(("capability", "params", "program", "kind"), [
    ("cap.not_this_device", {}, "OCP-10", "unsupported"),
    (CAPABILITY, {}, "UNKNOWN-PROGRAM", "invalid"),
    (CAPABILITY, {"temp": 25}, "OCP-10", "invalid"),                    # 不认的参数不悄悄忽略
    (CAPABILITY, {"duration_s": 1}, "OCP-10", "invalid"),               # params 登记的范围 [2, 3600]
    (CAPABILITY, {"duration_s": 5000}, "OCP-10", "invalid"),
    (CAPABILITY, {"duration_s": "10"}, "OCP-10", "invalid"),
    (CAPABILITY, {"duration_s": True}, "OCP-10", "invalid"),
    (CAPABILITY, {"scan_rate_V_s": 0.01}, "OCP-10", "invalid"),         # 开路电位用不上扫描速率
    (CAPABILITY, {"e_end_V": 6.5}, "LSV-ESW", "invalid"),               # 超出 params 登记的 [3.5, 6.0]
    (CAPABILITY, {"cycles": 2}, "CV-3", "invalid"),                     # cycles 没在 params 里登记
    (CAPABILITY, {"wells": {"A1": {}, "A2": {}}}, "OCP-10", "unsupported"),  # 一个通道只接一个电池
    (CAPABILITY, {"wells": {}}, "OCP-10", "invalid"),
    (CAPABILITY, {"wells": {"A1": 3}}, "OCP-10", "invalid"),
    (CAPABILITY, {"wells": {"A1": {"temp": 25}}}, "OCP-10", "invalid"),
    (CAPABILITY, {"wells": {"": {}}}, "OCP-10", "invalid"),
])
def test_driver_rejects_what_the_device_cannot_do(make, capability, params, program, kind):
    device, fake = make()
    with pytest.raises(Rejected) as caught:
        device.start(Job(command_id="CMD-X", capability=capability, params=params, method={"program": program}))
    assert caught.value.kind == kind and device.faults.motions == 0 and fake.runs == []


def test_more_than_one_cell_is_sent_back_to_be_split(make):
    device, _ = make()
    with pytest.raises(Rejected, match="一次只能测一个电池.*按电池分开下发"):
        device.start(job("CMD-X", wells={"A1": {}, "A2": {}, "A3": {}}))


def test_instrument_limits_are_checked_before_anything_moves(make):
    """EmStat4 LR 只有 ±3 V、EIS 振幅最大 0.9 Vrms（手册附录 B）：超了在发脚本之前就拒绝。"""
    eis = {"name": "大振幅", "technique": "eis", "freq_start_Hz": 1000, "freq_end_Hz": 10, "points_per_decade": 5,
           "amplitude_Vrms": 1.0}
    device, fake = make(device_type="es4_lr", model="EmStat4",
                        programs={**default_config()["programs"], "EIS-BIG": eis})
    with pytest.raises(Rejected, match="超出这台仪器的 -3–3 V") as caught:
        device.start(job("CMD-LSV", "LSV-ESW"))
    assert caught.value.kind == "invalid"
    with pytest.raises(Rejected, match="振幅 1 Vrms 超过这台仪器的 0.9 Vrms"):
        device.start(job("CMD-EIS", "EIS-BIG"))
    assert fake.runs == [] and device.faults.motions == 0
    assert run(device, job("CMD-OK", "OCP-10")).state == "done", "仪器范围之内的照常测"


def test_model_mismatch_refuses_commands(make):
    """配置写 EmStat4 HR，接的却是 LR：不同型号的电位范围不一样，不接指令。"""
    device, fake = make(device_type="es4_lr")
    identity = device.identity()
    assert identity["accepts_commands"] is False and "EmStat4 LR" in identity["warning"]
    with pytest.raises(Rejected, match="配置写的型号是 EmStat4 HR") as caught:
        device.start(job("CMD-X"))
    assert caught.value.kind == "invalid" and fake.runs == []


def test_one_cell_one_measurement_at_a_time(make):
    device, fake = make(time_scale=0.01)  # OCP-60 约 0.6 s
    first = job("CMD-A", "OCP-60")
    first.handle = device.start(first)
    with pytest.raises(Rejected) as busy:
        device.start(job("CMD-B"))
    assert busy.value.kind == "busy" and "一个通道接一个电池" in busy.value.message and device.faults.motions == 1
    assert wait(device, first).state == "done"
    assert run(device, job("CMD-B")).state == "done", "上一次测完，通道放开"
    assert len(fake.runs) == 2


def test_unreachable_instrument_before_start_is_busy_not_unknown(make):
    device, fake = make()
    fake.mute = True
    device.backend.link.timeout = 0.3
    device.backend.link.close()
    with pytest.raises(Rejected, match="读不到仪器") as caught:
        device.start(job("CMD-X"))
    assert caught.value.kind == "busy" and device.faults.motions == 0
    fake.mute = False
    assert run(device, job("CMD-Y")).state == "done", "通道没有被占着"


def test_interlock_and_busy_faults_are_explicit_rejections(make):
    device, fake = make()
    device.faults.set_fault("interlock")
    identity = device.identity()
    assert identity["interlock"] and not identity["accepts_commands"]
    with pytest.raises(Rejected) as interlock:
        device.start(job("CMD-A"))
    device.faults.set_fault("busy")
    with pytest.raises(Rejected) as busy:
        device.start(job("CMD-B"))
    assert interlock.value.kind == "interlocked" and busy.value.kind == "busy"
    assert device.faults.motions == 0 and fake.runs == []


def test_start_without_a_confirmation_is_unknown_and_the_instrument_is_stopped_later(make):
    """`r` 写出去了，回的不是 `r`：仪器可能已经在测——结果未知（不是明确拒绝），不重发；下次连仪器先同步停掉它。"""
    device, fake = make(time_scale=0.02)
    original = fake.send
    fake.send = lambda text: None if text == "r" else original(text)
    with pytest.raises(StartUnknown):
        device.start(job("CMD-U", "OCP-60"))
    fake.send = original
    assert fake.running and device.lookup(job("CMD-U")) is None
    device.identity()
    assert not fake.running and fake.runs[-1]["aborted"] is True
    assert run(device, job("CMD-NEXT")).state == "done"


# ---------- 各技术的曲线与派生指标 ----------

def test_ocp_curve_and_the_mean_of_the_last_tenth(make):
    device, _ = make()
    status = run(device, job("CMD-OCP"))
    assert status.state == "done" and status.error == "", status.error
    row = status.actuals
    curve = row["ocp_curve"]
    assert curve["x"][:2] == [0.5, 1.0] and curve["x"][-1] == 10.0 and len(curve["y"]) == 20
    assert row["ocp_V"] == pytest.approx(sum(curve["y"][-2:]) / 2, abs=1e-5), "最后 10% 的点（20 点里的 2 点）的平均"
    assert 2.98 < row["ocp_V"] < 3.06 and row["points"] == 20 and row["overload_points"] == 0
    assert row["program"] == "OCP-10" and row["technique"] == "ocp" and row["duration_s"] == 10.0


def test_eis_recovers_the_bulk_resistance_and_the_conductivity(make):
    """电导池：R_b = 80 Ω 串双电层 CPE。高频端 −Z'' 没有过零，取 |−Z''| 最小的点（最高频）；σ = 1000 × K / R_b。"""
    device, _ = make(cell_model=CellModel(r_bulk_ohm=80.0))
    status = run(device, job("CMD-EIS", "EIS-COND"))
    assert status.state == "done", status.error
    row = status.actuals
    assert row["points"] == 51 and row["freq_range_Hz"] == [1.0, 100000.0] and row["amplitude_Vrms"] == 0.01
    assert row["r_bulk_method"] == "min_imag_hf" and row["r_bulk_freq_Hz"] == 100000.0
    assert row["r_bulk_ohm"] == pytest.approx(80.0, rel=0.02)
    assert row["conductivity_mS_cm"] == pytest.approx(1000 * 1.0 / row["r_bulk_ohm"], rel=1e-5)
    assert row["conductivity_mS_cm"] == pytest.approx(12.5, rel=0.02) and row["cell_constant_per_cm"] == 1.0
    nyquist = row["nyquist"]
    assert nyquist["x"][0] == pytest.approx(80.0, rel=0.02) and all(y > 0 for y in nyquist["y"]), "阻塞电极：一根斜线"
    assert nyquist["y"][-1] > 1000 * nyquist["y"][0], "低频端 −Z'' 大得多（双电层）"
    assert [trace["name"] for trace in row["bode"]["traces"]] == ["|Z|（Ω）", "−相位（°）"]
    assert "略偏大" in row["note"] and row["e_dc_V"] == 0.0


def test_eis_with_lead_inductance_interpolates_the_zero_crossing(make):
    """引线电感让高频端 −Z'' 为负：在过零的两点之间插值。交点是 ωL 抵掉 CPE 容抗的那个频率上的 Z'，
    比 R_b 多出那里 CPE 的实部（物理上就是这样，不是算法误差）。"""
    cell = CellModel(r_bulk_ohm=40.0, inductance_H=2e-5)
    device, _ = make(cell_model=cell)
    row = run(device, job("CMD-EIS", "EIS-COND")).actuals
    low, high = 1e3, 1e5  # 对模型二分找 −Im Z = 0 的频率
    for _ in range(80):
        middle = math.sqrt(low * high)
        low, high = (middle, high) if -cell.impedance(middle).imag > 0 else (low, middle)
    intercept = cell.impedance(math.sqrt(low * high)).real
    assert row["r_bulk_method"] == "zero_crossing" and row["r_bulk_ohm"] == pytest.approx(intercept, rel=0.01)
    assert row["r_bulk_ohm"] == pytest.approx(40.0, rel=0.05)
    assert row["conductivity_mS_cm"] == pytest.approx(1000 / row["r_bulk_ohm"], rel=1e-5)
    assert any(y < 0 for y in row["nyquist"]["y"]), "高频端电感：−Z'' 是负的"
    assert "note" not in row


def test_eis_with_an_interface_semicircle_still_finds_the_high_frequency_intercept(make):
    device, _ = make(cell_model=CellModel(r_bulk_ohm=80.0, r_interface_ohm=150.0, q_interface=1e-6, alpha_interface=0.9))
    row = run(device, job("CMD-EIS", "EIS-COND")).actuals
    assert row["r_bulk_ohm"] == pytest.approx(80.0, rel=0.03), "R_b 不是半圆右端（80 + 150）"
    assert max(row["nyquist"]["x"]) > 230


def test_lsv_onset_of_the_electrochemical_stability_window(make):
    """从开路电位起扫到 6.0 V，1 mV/s：第一个 |j| ≥ 0.01 mA/cm² 的点就是起始电位；到 1 mA/cm² 提前停。"""
    cell = CellModel()
    device, fake = make(cell_model=cell)
    status = run(device, job("CMD-ESW", "LSV-ESW"))
    assert status.state == "done", status.error
    row = status.actuals
    lsv, onset = row["lsv"], row["onset_potential_V"]
    assert row["current_unit"] == "mA/cm2" and row["onset_threshold"] == pytest.approx(0.01)
    assert 4.45 < onset < 4.51, "模型里氧化电流 4.5 V 时到 0.01 mA/cm²（加上一点别的电流，略早一点）"
    index = lsv["x"].index(onset)
    assert lsv["y"][index] >= 0.01 and all(abs(y) < 0.01 for y in lsv["y"][:index]), "第一个到阈值的点"
    assert row["e_begin_V"] == row["rest_ocp_V"] and 2.9 < row["e_begin_V"] < 3.1, "从静置后的开路电位起扫"
    assert row["stopped_at_cutoff"] is True and 5.0 < row["e_end_V"] < 5.15 and "提前停" in row["note"]
    assert fake.runs[-1]["techniques"][0]["technique"] == "ocp" and row["area_cm2"] == 2.01


def test_lsv_that_never_reaches_the_threshold_reports_none(make):
    device, _ = make()
    status = run(device, job("CMD-ESW", "LSV-ESW", e_end_V=4.0))  # params 登记了 e_end_V [3.5, 6.0]
    row = status.actuals
    assert status.state == "done" and row["onset_potential_V"] is None and "都没到阈值" in row["note"]
    assert row["e_end_V"] == pytest.approx(4.0) and "stopped_at_cutoff" not in row


def test_lsv_without_an_electrode_area_reports_current_in_mA(make):
    programs = {"LSV": {"name": "不知道面积", "technique": "lsv", "e_begin_V": 3.0, "e_end_V": 5.0,
                        "scan_rate_V_s": 0.01, "e_step_V": 0.005, "onset_threshold_mA": 0.05}}
    device, _ = make(cell={}, programs=programs, default_program="LSV")
    row = run(device, job("CMD-L", "LSV")).actuals
    assert row["current_unit"] == "mA" and "area_cm2" not in row
    onset, lsv = row["onset_potential_V"], row["lsv"]
    index = lsv["x"].index(onset)
    # 10 mV/s 时可逆对的峰（约 0.022 mA）还在阈值 0.05 mA 以下：起始电位是电解液氧化（模型里约 4.59 V）
    assert 4.5 < onset < 4.65 and lsv["y"][index] >= 0.05 and all(abs(y) < 0.05 for y in lsv["y"][:index])


def test_cv_reports_one_trace_per_cycle_and_reversible_peaks(make):
    device, _ = make()
    status = run(device, job("CMD-CV", "CV-3"))
    row = status.actuals
    traces = row["cv"]["traces"]
    assert status.state == "done" and row["cycles"] == 3 and [t["name"] for t in traces] == ["第 1 圈", "第 2 圈", "第 3 圈"]
    assert all(len(trace["x"]) == 600 for trace in traces)
    assert row["epa_V"] - row["epc_V"] == pytest.approx(0.059, abs=0.01), "可逆对：峰电位差约 59 mV"
    assert (row["epa_V"] + row["epc_V"]) / 2 == pytest.approx(3.25, abs=0.01)
    assert row["jpa_mA_cm2"] > 0 > row["jpc_mA_cm2"] and row["current_unit"] == "mA/cm2"


def test_cv_scan_rate_can_come_from_the_command(make):
    """峰电流 ∝ √v：ILCS 指令带的 scan_rate_V_s（params 登记了范围）覆盖程序里的。"""
    device, _ = make()
    slow = run(device, job("CMD-S", "CV-3", scan_rate_V_s=0.0125)).actuals
    fast = run(device, job("CMD-F", "CV-3", scan_rate_V_s=0.05)).actuals
    assert slow["scan_rate_V_s"] == 0.0125 and fast["scan_rate_V_s"] == 0.05
    assert fast["jpa_mA_cm2"] / slow["jpa_mA_cm2"] == pytest.approx(2.0, rel=0.15)


def test_ca_reports_the_end_current(make):
    cell = CellModel()
    device, _ = make(cell_model=cell)
    row = run(device, job("CMD-CA", "CA-4V2")).actuals
    curve = row["ca_curve"]
    assert len(curve["x"]) == 120 and curve["x"][-1] == 60.0 and row["e_V"] == 4.2
    # 最后 10%（12 点，54.5–60 s）：氧化电流 + 可逆对的 Cottrell 衰减 M/√(πt)
    expected = sum(cell.oxidation_A(4.2) + cell.redox_M / math.sqrt(math.pi * t) for t in curve["x"][-12:]) / 12
    assert row["i_end_mA"] == pytest.approx(expected * 1000, rel=0.1)
    assert row["j_end_mA_cm2"] == pytest.approx(row["i_end_mA"] / 2.01, rel=1e-5)
    assert curve["y"][0] > curve["y"][-1], "电流随时间衰减"


def test_delivered_curves_pass_the_ilcs_validator(make):
    """回报的曲线按 ILCS 自己的口径核对：写法与点数（series.issues）、曲线指标的值校验（metrics.check_value）、
    设备方法输出项核对（dataquality.output_flags）。"""
    series, metrics, dataquality = ilcs_domain()
    device, _ = make()
    for program, technique in (("OCP-10", "ocp"), ("EIS-COND", "eis"), ("LSV-ESW", "lsv"), ("CV-3", "cv"),
                               ("CA-4V2", "ca")):
        row = run(device, job(f"CMD-{program}", program)).actuals
        for key, (unit, rules) in CURVES.items():
            if key not in row:
                continue
            assert series.rule_issues(rules) == []
            assert series.issues(row[key], rules) == [], (program, key)
            assert metrics.check_value("series", unit, rules, row[key], unit) == []
        assert dataquality.output_flags(OUTPUTS[technique] + [QUALITY], row) == [], program
    well = run(device, job("CMD-W", "EIS-COND", wells={"A1": {}})).actuals
    assert dataquality.output_flags(OUTPUTS["eis"], well) == []


def test_overloaded_points_are_flagged_not_failed(make):
    """自动量程上限压得太低：超出量程的点置过载位。照样完成，回报 overload_points 与说明，ILCS 按上限 0 打标。"""
    _, _, dataquality = ilcs_domain()
    programs = {"LSV": {"name": "量程太小", "technique": "lsv", "e_begin_V": 3.0, "e_end_V": 5.0,
                        "scan_rate_V_s": 0.01, "e_step_V": 0.005, "current": {"start_A": 1e-6, "autorange_A": None}}}
    device, _ = make(programs=programs, default_program="LSV")
    status = run(device, job("CMD-O", "LSV"))
    row = status.actuals
    assert status.state == "done" and status.error == "" and row["overload_points"] > 0 and "过载" in row["note"]
    assert [flag["key"] for flag in dataquality.output_flags([QUALITY], row)] == ["overload_points"]


def test_too_many_points_are_merged_down_to_max_points(make):
    device, _ = make(max_points=25)
    row = run(device, job("CMD-M", "OCP-60")).actuals
    assert len(row["ocp_curve"]["x"]) <= 25 and row["points"] == 60
    assert all(b > a for a, b in zip(row["ocp_curve"]["x"], row["ocp_curve"]["x"][1:]))


def test_one_well_reports_under_wells_and_its_params_override_the_step(make):
    device, _ = make()
    status = run(device, job("CMD-W", "OCP-10", duration_s=4, wells={"B3": {"duration_s": 3}}))
    assert status.state == "done" and set(status.actuals) == {"wells"}
    assert status.actuals["wells"]["B3"]["points"] == 6, "孔位里的 duration_s 3 s / 0.5 s"
    defaults = run(device, job("CMD-W2", "OCP-10", duration_s=4, wells={"C1": {}})).actuals["wells"]["C1"]
    assert defaults["points"] == 8, "孔位没写的参数用步骤顶层的"


# ---------- 终止、出错、故障 ----------

def test_abort_stops_the_instrument_and_reports_no_data(make):
    device, fake = make(time_scale=0.02)  # OCP-60 约 1.2 s
    target = job("CMD-AB", "OCP-60")
    target.handle = device.start(target)
    time.sleep(0.15)
    started = time.monotonic()
    device.abort(target)
    assert time.monotonic() - started < 1.0
    status = device.status(target)
    assert status.state == "failed" and "被终止" in status.error and status.actuals == {}
    assert fake.runs[-1]["aborted"] is True and fake.cell_on is False, "仪器收到 Z：测量收尾、on_finished 断开电池"
    assert run(device, job("CMD-NEXT")).state == "done", "终止后通道放开了"
    device.abort(target)  # 已经失败结束：照样确认，不报错


def test_abort_after_the_measurement_finished_is_refused(make):
    """终止到达之前已经测完：如实拒绝终止（来不及），原作业照报完成、数据照常回报。"""
    device, _ = make()
    done = job("CMD-DONE")
    assert run(device, done).state == "done"
    with pytest.raises(Rejected, match="来不及终止") as caught:
        device.abort(done)
    assert caught.value.kind == "invalid" and device.status(done).state == "done"


def test_slow_abort_is_reported_unknown_then_settles(make):
    device, fake = make(time_scale=0.05, backend={**default_config()["backend"], "abort_timeout_sec": 0.2})
    fake.abort_delay = 0.6  # 仪器收到 Z 之后拖 0.6 s 才停
    target = job("CMD-SLOW-ABORT", "OCP-60")
    target.handle = device.start(target)
    time.sleep(0.1)
    with pytest.raises(RuntimeError, match="还没结束测量"):
        device.abort(target)  # 等不到仪器停：结果未知，不谎报已停
    status = wait(device, target, 5)
    assert status.state == "failed" and "被终止" in status.error and not fake.running


def test_instrument_error_mid_measurement_fails_and_turns_the_cell_off(make):
    device, fake = make()
    fake.fail_after = 5
    status = run(device, job("CMD-E", "LSV-ESW"))
    assert status.state == "failed" and "!0032" in status.error and "已收到 5 个点" in status.error
    assert fake.cell_on is False and "补发" not in status.error, "补发的 cell_off 确认了"
    fake.fail_after = None
    assert run(device, job("CMD-OK")).state == "done"


def test_link_lost_mid_measurement_fails_after_stopping_the_instrument(make):
    device, fake = make(time_scale=0.02)
    fake.drop_after = 5
    status = run(device, job("CMD-DROP", "OCP-60"))
    assert status.state == "failed" and "链路断了" in status.error and "重连后已停掉" in status.error
    assert not fake.running and fake.runs[-1]["aborted"] is True and fake.cell_on is False
    fake.drop_after = None
    assert run(device, job("CMD-AFTER")).state == "done"


def test_simulated_failure_and_stuck_runs(make):
    device, fake = make()
    device.faults.set_fault("fail")
    failed = run(device, job("CMD-F"))
    assert failed.state == "failed" and "模拟故障" in failed.error and failed.actuals == {}
    device.faults.set_fault("stuck")
    stuck = job("CMD-S")
    stuck.handle = device.start(stuck)
    time.sleep(0.2)
    assert device.status(stuck).state == "running", "一直不结束"
    device.abort(stuck)
    assert device.status(stuck).state == "failed" and device.faults.motions == 2


def test_lost_receipt_still_measures_and_is_found_by_command_id(make, tmp_path):
    device, fake = make()
    device.faults.set_fault("lost_receipt")
    lost = job("CMD-LOST")
    with pytest.raises(ReceiptLost) as caught:
        device.start(lost)
    assert caught.value.handle == "CMD-LOST" and device.faults.motions == 1
    assert device.lookup(lost) == "CMD-LOST"
    lost.handle = caught.value.handle
    assert wait(device, lost).state == "done"
    fresh = Instrument(MethodScript(device.config.link, timeout=1.0), device.config, state_dir=tmp_path / "state")
    assert fresh.lookup(lost) == "CMD-LOST", "网关重启后按状态目录里的结论找回"
    fresh.close()


def test_slow_submit_delays_the_reply_but_starts_once(make):
    device, fake = make()
    device.faults.set_fault("slow_submit", 0.2)
    started = time.monotonic()
    command = job("CMD-SLOW")
    command.handle = device.start(command)
    assert time.monotonic() - started >= 0.2 and device.faults.motions == 1 and len(fake.runs) == 1


def test_status_and_identity_do_not_talk_to_the_instrument_while_measuring(make):
    device, fake = make(time_scale=0.02)
    target = job("CMD-Q", "OCP-60")
    target.handle = device.start(target)
    time.sleep(0.1)
    before = len(fake.received)
    for _ in range(20):
        running = device.status(target)
        identity = device.identity()
    assert len(fake.received) == before, "测量进行中：状态、身份都不碰仪器"
    assert running.state == "running" and identity["serial"].startswith("ILCS-SIMULATOR")
    metrics = {item["metric"]: item for item in running.telemetry}
    assert metrics["points"]["value"] > 0 and metrics["points"]["setpoint"] == 60.0
    assert 2.9 < metrics["potential_V"]["value"] < 3.1
    assert wait(device, target).state == "done"


# ---------- 身份、配置、模板 ----------

def test_identity_reports_methods_and_no_hold(make):
    device, _ = make()
    identity = device.identity()
    assert identity["simulator"] is True and identity["serial"] == "ILCS-SIMULATOR-ES4HR-01"
    assert identity["device_id"] == DEVICE_ID and identity["vendor"] == "PalmSens" and identity["model"] == "EmStat4 HR"
    assert identity["device_type"] == "es4_hr" and identity["script_version"] == "01.08.00"
    assert identity["firmware"].startswith("es4_hr 1.4.00")
    assert {method["program"] for method in identity["methods"]} == set(default_config()["programs"])
    assert {method["capability"] for method in identity["methods"]} == {"cap.echem"}
    assert {method["technique"] for method in identity["methods"]} == {"ocp", "eis", "lsv", "cv", "ca"}
    assert "hold" not in identity["commands"] and "resume" not in identity["commands"]
    assert identity["accepts_commands"] is True and identity["interlock"] is False and "warning" not in identity
    assert device.fault_target() is not None
    assert Instrument(MethodScript({"kind": "tcp", "host": "127.0.0.1", "port": 1}), CONFIG).fault_target() is None, \
        "真仪器没有模拟控制口"


def test_container_config_and_example_config_parse():
    sim = Config.load(MODULE / "simulator" / "potentiostat-sim.json")
    assert sim.device_id == "SIM-ECHEM-01" and sim.default_program == "OCP-10"
    assert sim.programs["EIS-COND"].cell.cell_constant_per_cm == 1.0 and sim.cell.area_cm2 == 2.01
    assert sim.programs["LSV-ESW"].settings["stop_A"] == pytest.approx(2.01e-3)
    example = Config.load(MODULE / "config.example.json")
    assert example.link["kind"] == "serial" and example.link["baudrate"] == 921600
    assert example.default_program in example.programs and example.model
    pico = Config.parse({**default_config(), "model": "EmStat Pico",
                         "backend": {"kind": "methodscript", "link": {"kind": "serial", "port": "COM7"}}})
    assert pico.link["baudrate"] == 230400 and pico.link["xonxoff"] is True


def test_profile_matches_the_module_and_would_import():
    """profile.json 按 ILCS 导入时的口径核对：摘要对得上（改过要重算）、能发布，契约与验收缺省和网关一致。"""
    ilcs_domain()  # api/ 进 sys.path
    from app.services.template_service import template_check, template_digest

    profile = json.loads((MODULE / "profile.json").read_text(encoding="utf-8"))
    assert profile["digest"] == template_digest(profile), "profile.json 改过之后要重算摘要"
    assert template_check(profile)["ok"] and profile["state"] == "draft"
    assert profile["supports"] == SUPPORTS and profile["driver"] == "http_json_v1"
    assert profile["acceptance"] == {"capability": CAPABILITY, "params": {}}
    assert profile["code"] == "TPL-POTENTIOSTAT" and profile["vendor"] == "PalmSens"


def test_config_problems_are_reported_together():
    with pytest.raises(ValueError) as caught:
        Config.parse({
            "backend": {"kind": "gamry", "link": {"kind": "serial"}, "abort_timeout_sec": 0},
            "cell": {"area_cm2": -1}, "limits": {"e_min_V": 5, "e_max_V": 1},
            "params": {"temp": [0, 1], "cycles": [0.5, 3]},
            "programs": {
                "A": {"technique": "lsv", "e_begin_V": 3, "e_end_V": 3.001, "scan_rate_V_s": 0.001, "e_step_V": 0.01,
                      "onset_threshold_mA_cm2": 0.01, "power": 5},
                "B": {"technique": "eis", "freq_start_Hz": 10, "freq_end_Hz": 10, "points_per_decade": 5,
                      "amplitude_Vrms": 0.01},
                "F": {"technique": "eis", "freq_start_Hz": 1000, "freq_end_Hz": 10, "points_per_decade": 1.5,
                      "amplitude_Vrms": 0.01},
                "C": {"technique": "gitt"},
                "D": {"technique": "cv", "e_begin_V": 0, "e_vertex1_V": 1, "e_vertex2_V": 1, "e_step_V": 0.01,
                      "scan_rate_V_s": 0.1, "cycles": 60},
                "E": {"technique": "ocp", "duration_s": 1, "interval_s": 1},
            },
            "default_program": "Z", "max_points": 50000, "colour": "red",
        })
    message = str(caught.value)
    for fragment in ("device_id", "backend.kind", "port", "abort_timeout_sec", "area_cm2", "e_min_V", "temp",
                     "cycles 的上下限", "不认 power", "不够一个 e_step_V", "freq_start_Hz 与 freq_end_Hz 不能相同",
                     "points_per_decade", "technique 只能是", "cycles 最多 50", "不能相同", "至少要 2 个点",
                     "default_program", "max_points", "colour"):
        assert fragment in message, fragment
