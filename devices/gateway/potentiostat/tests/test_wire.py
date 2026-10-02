"""真实接口（driver/palmsens.py）走 TCP 对着假 MethodSCRIPT 仪器：身份、加载 / 运行、数据流、终止、出错收尾、断线同步。

    pytest devices/gateway/potentiostat/tests/test_wire.py
"""
from __future__ import annotations

import socket
import threading
import time

import pytest

from driver.backend import BackendError, BackendRejected, Plan, Ranging
from driver.link import Link, LinkError
from driver.palmsens import MethodScript
from driver.techniques import Cell, check_settings
from simulator.cell_model import CellModel
from simulator.fake_methodscript import FakeInstrument, InstrumentServer


@pytest.fixture()
def fake():
    instrument = FakeInstrument(CellModel(), time_scale=0.0)
    server = InstrumentServer(instrument)
    try:
        yield instrument, server
    finally:
        server.stop()


def backend_for(server: InstrumentServer, timeout: float = 1.0) -> MethodScript:
    return MethodScript({"kind": "tcp", "host": "127.0.0.1", "port": server.port}, timeout=timeout)


def plan(technique: str, **settings) -> Plan:
    checked, problems = check_settings(technique, settings, Cell(area_cm2=2.01))
    assert problems == []
    return Plan(technique=technique, settings=checked, ranging=Ranging(1e-4, 1e-9, 1e-2), bandwidth_Hz=4.0)


OCP = dict(duration_s=10, interval_s=0.5)
LSV = dict(e_begin_V=3.0, e_end_V=4.0, scan_rate_V_s=0.01, e_step_V=0.005)


def measure(backend: MethodScript, measurement: Plan):
    points = []
    backend.prepare(measurement)
    backend.begin()
    finish = backend.stream(points.append)
    return finish, points


def test_identity_reads_firmware_serial_and_script_version(fake):
    instrument, server = fake
    backend = backend_for(server)
    info = backend.identity()
    assert info["device_type"] == "es4_hr" and info["model"] == "EmStat4 HR"
    assert info["serial"] == "ILCS-SIMULATOR-ES4HR-01" and info["simulator"] is True
    assert info["firmware"].startswith("es4_hr 1.4.00") and info["script_version"] == "01.08.00"
    limits = backend.limits("lsv")
    assert (limits.e_min_V, limits.e_max_V, limits.eis_max_vrms) == (-6.0, 6.0, 0.9)
    assert instrument.received[:2] == ["", "Z"], "连上先同步：补一个换行、发 Z（没有脚本在跑，回 Z!0006）"
    backend.identity()
    assert instrument.received.count("i") == 1, "序列号、版本只在连上时读一次；健康检查只问 t"


def test_load_then_run_streams_points_until_the_empty_line(fake):
    instrument, server = fake
    backend = backend_for(server)
    backend.identity()
    finish, points = measure(backend, plan("ocp", **OCP))
    assert finish.state == "done" and finish.error == ""
    assert len(points) == 20 and {p.segment for p in points} == {"ocp"}
    assert [round(p.values["t_s"], 3) for p in points[:3]] == [0.5, 1.0, 1.5]
    assert 2.9 < points[-1].values["e_V"] < 3.1
    assert "l" in instrument.received and "r" in instrument.received and instrument.received.count("e") == 0
    assert not backend.running and instrument.cell_on is False


def test_lsv_from_ocp_reports_the_rest_and_the_sweep(fake):
    instrument, server = fake
    backend = backend_for(server)
    backend.identity()
    finish, points = measure(backend, plan("lsv", e_begin_V="ocp", rest_s=5, e_end_V=4.0, scan_rate_V_s=0.01,
                                           e_step_V=0.01))
    rest = [p for p in points if p.segment == "ocp"]
    sweep = [p for p in points if p.segment == "lsv"]
    assert finish.state == "done" and len(rest) == 10 and len(sweep) > 50
    assert sweep[0].values["e_V"] == pytest.approx(rest[-1].values["e_V"], abs=1e-6), "从静置最后的开路电位起扫"
    assert sweep[-1].values["e_V"] == pytest.approx(4.0)
    techniques = instrument.runs[-1]["techniques"]
    assert [item["technique"] for item in techniques] == ["ocp", "lsv"]
    assert techniques[1]["values"][2] == pytest.approx(rest[-1].values["e_V"], abs=1e-6), "仪器拿到的起点是变量 oc"


def test_a_late_abort_reply_is_not_taken_for_the_run_reply(fake):
    """终止撞上上一次测量结束时，仪器回 Z!0006（没有脚本在跑）。它晚到、夹在 r 的应答前面时不能当成「没开始」。"""
    instrument, server = fake
    backend = backend_for(server)
    backend.identity()
    backend.prepare(plan("ocp", **OCP))
    backend.link.write("Z\n")  # 空闲时的 Z：仪器回 Z!0006，排在 r 的应答前面
    backend.begin()
    finish = backend.stream(lambda point: None)
    assert finish.state == "done" and instrument.runs[-1]["aborted"] is False


def test_cv_scans_are_numbered(fake):
    _, server = fake
    backend = backend_for(server)
    backend.identity()
    finish, points = measure(backend, plan("cv", e_begin_V=3.0, e_vertex1_V=3.6, e_vertex2_V=3.0, e_step_V=0.01,
                                           scan_rate_V_s=0.05, cycles=3))
    assert finish.state == "done" and sorted({p.scan for p in points}) == [0, 1, 2]
    assert all(sum(1 for p in points if p.scan == scan) == 120 for scan in range(3))


def test_instrument_rejects_a_bad_script_at_load_time(fake, monkeypatch):
    """加载时出错（`l!XXXX: Line L, Col C`）：仪器没动，带着出错的那一行报回来。"""
    instrument, server = fake
    backend = backend_for(server)
    backend.identity()
    import driver.palmsens as palmsens

    original = palmsens.build_script
    monkeypatch.setattr(palmsens, "build_script", lambda *a, **k: [*original(*a, **k)[:3], "wrong_command 1",
                                                                    *original(*a, **k)[3:]])
    with pytest.raises(BackendRejected) as caught:
        backend.prepare(plan("ocp", **OCP))
    assert caught.value.kind == "invalid" and "!4001" in caught.value.message and "wrong_command 1" in caught.value.message
    assert instrument.runs == [], "加载失败：仪器什么都没跑"
    monkeypatch.setattr(palmsens, "build_script", original)
    assert measure(backend, plan("ocp", **OCP))[0].state == "done", "出错之后链路照常可用"


def test_runtime_error_sends_a_cell_off_script(fake):
    """运行时出错（!0032 电池严重过载）不执行 on_finished：网关另发一个只有 cell_off 的脚本，确认电池断开。"""
    instrument, server = fake
    backend = backend_for(server)
    backend.identity()
    instrument.fail_after = 5
    finish, points = measure(backend, plan("lsv", **LSV))
    assert finish.state == "error" and "!0032" in finish.error and "电池严重过载" in finish.error
    assert finish.safe is True and instrument.cell_on is False and len(points) == 5
    assert instrument.received[-3:] == ["e", "cell_off", ""], "补发的 cell_off 脚本"
    instrument.fail_after = None
    assert measure(backend, plan("ocp", **OCP))[0].state == "done"


def test_runtime_limit_violation_is_an_error_not_a_hang(fake):
    """仪器自己查出电位超范围（!000F）：报错结束，电池断开。网关平时在发之前就按型号核对过（见 test_module）。"""
    instrument, server = fake
    backend = backend_for(server)
    backend.identity()
    finish, _ = measure(backend, plan("lsv", e_begin_V=3.0, e_end_V=7.0, scan_rate_V_s=0.1, e_step_V=0.01))
    assert finish.state == "error" and "!000F" in finish.error and instrument.cell_on is False


def test_abort_stops_the_script_and_runs_on_finished():
    instrument = FakeInstrument(CellModel(), time_scale=0.01)  # 一点 5 ms
    server = InstrumentServer(instrument)
    try:
        backend = backend_for(server)
        backend.identity()
        backend.prepare(plan("ocp", duration_s=100, interval_s=0.5))
        backend.begin()
        points: list = []
        threading.Timer(0.1, backend.abort).start()
        finish = backend.stream(points.append)
        assert finish.state == "aborted" and 0 < len(points) < 200
        assert instrument.runs[-1]["aborted"] is True and instrument.cell_on is False
        backend.abort()  # 没有在跑：什么都不发
        assert instrument.received.count("Z") == 2  # 连上时同步一次 + 终止一次
    finally:
        server.stop()


def test_link_lost_mid_run_then_recover_stops_the_instrument():
    """测量途中断线：读线程报 BackendError；重连时同步（发 Z），仪器上还在跑的脚本停下、电池断开。"""
    instrument = FakeInstrument(CellModel(), time_scale=0.01)
    server = InstrumentServer(instrument)
    try:
        backend = backend_for(server)
        backend.identity()
        instrument.drop_after = 3
        backend.prepare(plan("ocp", duration_s=200, interval_s=0.5))
        backend.begin()
        points: list = []
        with pytest.raises(BackendError):
            backend.stream(points.append)
        assert len(points) <= 3 and instrument.running, "仪器照跑，输出没人收"
        with pytest.raises(BackendError):
            backend.identity()  # 断线重连之前：健康检查如实报失联，不回旧的身份
        assert not backend.ready
        backend.recover()
        assert backend.ready and not instrument.running and instrument.runs[-1]["aborted"] is True
        assert instrument.cell_on is False
        assert backend.identity()["serial"].startswith("ILCS-SIMULATOR")
    finally:
        server.stop()


def test_a_script_left_running_by_a_previous_gateway_is_stopped_on_connect():
    instrument = FakeInstrument(CellModel(), time_scale=0.01)
    server = InstrumentServer(instrument)
    try:
        old = backend_for(server)
        old.identity()
        old.prepare(plan("ocp", duration_s=200, interval_s=0.5))
        old.begin()  # 网关在这里「崩了」：没人读输出
        assert instrument.running
        new = backend_for(server)  # 重启后的网关
        assert not new.ready
        new.identity()
        assert new.ready and not instrument.running and instrument.runs[-1]["aborted"] is True
    finally:
        server.stop()


def test_mute_instrument_is_unreachable_not_rejected(fake):
    instrument, server = fake
    instrument.mute = True
    backend = backend_for(server, timeout=0.3)
    with pytest.raises(BackendError):
        backend.identity()


def test_nothing_listening_is_a_backend_error():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    backend = MethodScript({"kind": "tcp", "host": "127.0.0.1", "port": port}, timeout=0.3)
    with pytest.raises(BackendError):
        backend.identity()


def test_pico_xon_is_ignored():
    instrument = FakeInstrument(CellModel(), device_type="espico", serial="ILCS-SIMULATOR-PICO", time_scale=0.0)
    server = InstrumentServer(instrument)
    try:
        backend = backend_for(server)
        info = backend.identity()
        assert info["device_type"] == "espico" and info["model"] == "EmStat Pico"
        assert backend.limits("eis").window_V == pytest.approx(1.214)
    finally:
        server.stop()


def test_link_keeps_spaces_and_empty_lines():
    """数据包里值为 0 时前缀是空格、脚本结束是空行：链路只去掉 \\r，不裁空格、不跳空行。"""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def serve():
        connection, _ = listener.accept()
        connection.sendall(b"Pda8000000 \r\n\nM0000\n")
        time.sleep(0.2)
        connection.close()

    threading.Thread(target=serve, daemon=True).start()
    link = Link({"kind": "tcp", "host": "127.0.0.1", "port": port}, timeout=1.0)
    link.write("x\n")
    assert link.read_line() == "Pda8000000 " and link.read_line() == "" and link.read_line() == "M0000"
    assert link.try_read_line(0.05) is None
    with pytest.raises(LinkError):
        link.read_line(1.0)
    listener.close()
