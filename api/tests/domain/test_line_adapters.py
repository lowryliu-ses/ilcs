"""`line_command_v1`（串口 / TCP 文本命令）与 `mt_sics_v1`（MT-SICS 天平）× 外部模拟设备：真实走 TCP。

这些设备不认识 ILCS 指令号：去重、重启后按原指令号查询全靠驱动的作业台账。
"""
import json
import time

import pytest

from sim_harness import balance_config, balance_sim, line_config, line_sim, record, request


@pytest.fixture(autouse=True)
def journal_dir(tmp_path, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "adapter_state_root", str(tmp_path / "adapter-state"))
    return tmp_path / "adapter-state"


def _line(port: int, dialect: str = "oven", **config):
    from app.adapters.drivers.line_command import LineCommandAdapter

    return LineCommandAdapter(record("串口 / TCP 命令", line_config(port, dialect, **config)))


def _balance(port: int, **config):
    from app.adapters.drivers.mt_sics import MtSicsAdapter

    return MtSicsAdapter(record("MT-SICS", balance_config(port, **config)))


def _wait(adapter, command_id: str, device, seconds: float = 5, states=("done", "failed")):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        device.tick()
        result = adapter.query(command_id)
        if result is not None and result.state in states:
            return result
        time.sleep(0.05)
    raise AssertionError(f"指令 {command_id} 没有在时限内到达 {states}")


# ---------- 串口 / TCP 命令：真空干燥箱 ----------

def test_oven_runs_to_done_with_actuals_and_replays_duplicates():
    with line_sim(task_seconds=0.3, methods=[{"program": "VD-120"}]) as (device, _, port):
        adapter = _line(port, expected_device_id="SIM-OVEN-T")
        health = adapter.healthcheck()
        assert (health["device_id"], health["simulator"], health["interlock"], health["accepts_commands"]) == (
            "SIM-OVEN-T", True, False, True)

        accepted = adapter.submit(request("CMD-1", program="VD-120"))
        assert accepted.state == "accepted" and accepted.origin == "real:line_command_v1"
        assert adapter.submit(request("CMD-1", program="VD-120")).command_id == "CMD-1"
        assert sum(device.executions.values()) == 1, "同一指令号重投只回放，设备只 RUN 一次"

        done = _wait(adapter, "CMD-1", device)
        assert done.state == "done" and done.quality == "good"
        assert abs(done.delivered["temp"] - 120) < 1 and abs(done.delivered["vacuum"] - 1) < 0.1
        assert {(name, setpoint) for name, _, setpoint in done.telemetry} == {("temp", 120.0), ("vacuum", 1.0)}
        assert adapter.query("CMD-NEVER-SEEN") is None

        # 上一个作业停在 DONE：下一条先 ACK 复位再启动
        assert adapter.submit(request("CMD-2", program="VD-120")).state == "accepted"
        assert sum(device.executions.values()) == 2


def test_executor_restart_answers_from_the_journal_without_resending(journal_dir):
    with line_sim(task_seconds=0.5) as (device, _, port):
        first = _line(port)
        first.submit(request("CMD-R"))
        journal = json.loads((journal_dir / "ST-SIM.json").read_text())
        assert journal["active"] == "CMD-R", "启动前先落盘"

        restarted = _line(port)  # 执行器重启：新实例从台账读回在途作业
        assert restarted.query("CMD-R").state in {"accepted", "running"}
        assert restarted.submit(request("CMD-R")).command_id == "CMD-R"
        assert sum(device.executions.values()) == 1, "重启后重投同一指令号不再发 RUN"
        assert _wait(restarted, "CMD-R", device).state == "done"


def test_rejections_before_the_run_command_mean_the_oven_did_not_move():
    from app.adapters import AdapterError

    with line_sim(task_seconds=5, methods=[{"program": "VD-120"}]) as (device, _, port):
        adapter = _line(port)
        with pytest.raises(AdapterError, match="没有对应的写入点或命令"):
            adapter.submit(request("CMD-P", params={"temp": 120, "vacuum": 1, "speed": 3}))
        with pytest.raises(AdapterError, match="结构化参数"):
            adapter.submit(request("CMD-W", params={"wells": {"A1": {"temp": 1}}}))
        with pytest.raises(AdapterError, match="ERR PROG"):
            adapter.submit(request("CMD-G", program="VD-999"))
        with pytest.raises(AdapterError, match="没有在适配器配置 capabilities"):
            adapter.submit(request("CMD-C", capability="cap.coat"))
        device.set_fault("interlock")
        with pytest.raises(AdapterError, match="联锁"):
            adapter.submit(request("CMD-I", program="VD-120"))
        device.set_fault("none")
        assert not device.executions, "被拒绝的指令一条都没有让设备动作"

        adapter.submit(request("CMD-A", program="VD-120"))
        with pytest.raises(AdapterError, match="DeviceBusy"):
            adapter.submit(request("CMD-B", program="VD-120"))
        assert sum(device.executions.values()) == 1


def test_lost_reply_to_run_is_unknown_and_later_seen_running_as_uncertain():
    from app.adapters import AdapterError, AdapterUnreachable

    with line_sim(task_seconds=0.4) as (device, _, port):
        adapter = _line(port)
        device.set_fault("lost_receipt")
        with pytest.raises(AdapterUnreachable) as lost:
            adapter.submit(request("CMD-LOST"))
        assert not isinstance(lost.value, AdapterError), "RUN 没有回复：结果未知，不是明确失败"
        device.set_fault("none")
        assert sum(device.executions.values()) == 1, "回复丢了，但干燥箱已经在跑"

        seen = adapter.query("CMD-LOST")
        assert seen.state == "running" and seen.quality == "uncertain", "见到在运行才按运行处理，质量标 uncertain"
        done = _wait(adapter, "CMD-LOST", device)
        assert done.state == "done" and done.quality == "uncertain"


def test_hold_resume_and_abort_follow_the_active_job():
    with line_sim(task_seconds=30) as (device, _, port):
        adapter = _line(port)
        adapter.submit(request("CMD-H"))
        assert adapter.hold(request("CMD-HOLD", "hold", target="CMD-H")).state == "done"
        assert adapter.query("CMD-H").state == "running"
        job = next(task for task in device.tasks.values())
        assert job.state == "held"

        resumed = adapter.submit(request("CMD-RESUME", "resume"))
        assert resumed.command_id == "CMD-RESUME" and resumed.state == "running"
        assert job.state == "running", "恢复指令接着原作业跑，不重新开始"

        assert adapter.abort(request("CMD-ABORT", "abort", target="CMD-H")).state == "done"
        aborted = adapter.query("CMD-H")
        assert aborted.state == "failed" and "终止" in aborted.error
        assert adapter.abort(request("CMD-ABORT", "abort", target="CMD-H")).state == "done", "重复的终止回放"


def test_device_alarm_is_a_failure_with_the_mapped_reason():
    with line_sim(task_seconds=0.3) as (device, _, port):
        adapter = _line(port)
        device.set_fault("fail")
        adapter.submit(request("CMD-F"))
        failed = _wait(adapter, "CMD-F", device)
        assert failed.state == "failed" and "温度偏差超限" in failed.error


def test_acknowledged_start_that_never_runs_becomes_unknown():
    """启动命令回了 OK，状态却一直是空闲、也没有完成信号：不猜「做完了」，超时后转结果未知。"""
    with line_sim(task_seconds=30) as (device, _, port):
        adapter = _line(port, start_timeout_sec=0.3, status={
            "send": "DOOR?", "pattern": "^(?P<state>\\w+)$", "states": {"CLOSED": "idle", "OPEN": "failed"},
        })
        adapter.submit(request("CMD-S"))
        assert adapter.query("CMD-S").state == "accepted"
        time.sleep(0.4)
        assert adapter.query("CMD-S").state == "unknown"


def test_offline_device_is_unreachable_and_journal_survives():
    from app.adapters import AdapterUnreachable

    with line_sim(task_seconds=5) as (device, runner, port):
        adapter = _line(port)
        adapter.submit(request("CMD-O"))
        runner.server.go_offline(1)
        time.sleep(0.4)
        with pytest.raises(AdapterUnreachable):
            adapter.query("CMD-O")
        deadline = time.monotonic() + 5
        found = None
        while found is None and time.monotonic() < deadline:
            try:
                found = adapter.query("CMD-O")
            except AdapterUnreachable:
                time.sleep(0.2)
        assert found is not None and found.state == "running"


def test_corrupt_journal_refuses_to_guess(journal_dir):
    from app.adapters import AdapterError

    journal_dir.mkdir(parents=True, exist_ok=True)
    (journal_dir / "ST-SIM.json").write_text("{not json")
    with pytest.raises(AdapterError, match="作业台账"):
        _line(1)


def test_serial_transport_rejects_unlisted_paths_and_hosts():
    from app.adapters import AdapterError
    from app.adapters.drivers.line_command import LineCommandAdapter

    for port in ("/etc/passwd", "rfc2217://evil.example:4001", "telnet://127.0.0.1:23"):
        with pytest.raises(AdapterError):
            LineCommandAdapter(record("串口", {**line_config(1), "transport": {"kind": "serial", "port": port}}))
    ok = LineCommandAdapter(record("串口", {**line_config(1), "transport": {"kind": "serial", "port": "rfc2217://127.0.0.1:4001"}}))
    assert ok.transport.label == "rfc2217://127.0.0.1:4001"


# ---------- UR 仪表盘服务 ----------

def test_ur_dashboard_program_runs_and_safety_stop_blocks_start():
    from app.adapters import AdapterError

    with line_sim("ur", "SIM-ARM-T", task_seconds=0.3, methods=[{"program": "load_glovebox"}]) as (device, _, port):
        adapter = _line(port, "ur")
        assert adapter.healthcheck()["simulator"] is True
        started = adapter.submit(request("CMD-ARM", capability="cap.robot_load", params={}, program="load_glovebox"))
        assert started.state == "accepted"
        assert _wait(adapter, "CMD-ARM", device).state == "done"

        with pytest.raises(AdapterError, match="File not found"):
            adapter.submit(request("CMD-X", capability="cap.robot_load", params={}, program="missing"))
        device.set_fault("interlock")
        with pytest.raises(AdapterError, match="联锁"):
            adapter.submit(request("CMD-Y", capability="cap.robot_load", params={}, program="load_glovebox"))


# ---------- MT-SICS 天平 ----------

def test_balance_weighs_once_per_command_and_reports_identity():
    with balance_sim() as (balance, _, port):
        adapter = _balance(port, expected_device_id="SIM-BAL-T")
        health = adapter.healthcheck()
        assert (health["model"], health["simulator"]) == ("XPR226", True)

        done = adapter.submit(request("CMD-W1", capability="cap.weigh", params={"mass": 0.0152}))
        assert done.state == "done" and done.origin == "real:mt_sics_v1"
        assert abs(done.delivered["mass"] - 0.0152) < 0.0001 and done.delivered["stable"] is True
        assert done.telemetry[0][0] == "mass" and done.telemetry[0][2] == 0.0152
        again = adapter.submit(request("CMD-W1", capability="cap.weigh", params={"mass": 0.0152}))
        assert again.delivered["mass"] == done.delivered["mass"] and balance.weighings == 1, "重投回放原读数"
        assert adapter.query("CMD-W1").state == "done"
        assert adapter.query("CMD-NEVER-SEEN") is None


def test_balance_errors_are_explicit_and_lost_reply_is_unknown():
    from app.adapters import AdapterError, AdapterUnreachable

    with balance_sim() as (balance, _, port):
        adapter = _balance(port)
        balance.fault = "busy"
        with pytest.raises(AdapterError, match="不能执行"):
            adapter.submit(request("CMD-B", capability="cap.weigh", params={"mass": 0.01}))
        balance.fault = "fail"
        with pytest.raises(AdapterError, match="超载"):
            adapter.submit(request("CMD-O", capability="cap.weigh", params={"mass": 0.01}))
        balance.fault = "lost_receipt"
        with pytest.raises(AdapterUnreachable) as lost:
            adapter.submit(request("CMD-L", capability="cap.weigh", params={"mass": 0.01}))
        assert not isinstance(lost.value, AdapterError)
        balance.fault = "none"
        assert adapter.query("CMD-L").state == "unknown", "称了但没回读数：台账记未确认，不补一个读数"
        assert adapter.abort(request("CMD-RESET", "abort")).state == "done", "@ 复位"
        with pytest.raises(AdapterError, match="没有对应的写入点"):
            adapter.submit(request("CMD-P", capability="cap.weigh", params={"mass": 0.01, "speed": 1}))


def test_balance_unit_conversion_and_tare():
    with balance_sim(sample_mass=0.5) as (_, _, port):
        adapter = _balance(port, capabilities={
            "cap.weigh": {"action": "weigh", "result": "mass", "unit": "mg", "stable_timeout_sec": 3},
            "cap.tare": {"action": "tare"},
        })
        assert abs(adapter.submit(request("CMD-MG", capability="cap.weigh", params={})).delivered["mass"] - 500) < 0.1
        tared = adapter.submit(request("CMD-T", capability="cap.tare", params={}))
        assert tared.state == "done" and abs(tared.delivered["tare"] - 0.5) < 0.001


def test_barcode_reader_returns_the_code_as_an_immediate_result():
    """固定式读码器（LON → 条码 / ERROR）：动作命令的回复就是结果；读不到是明确失败。"""
    import socket
    import threading

    from app.adapters import AdapterError
    from app.adapters.drivers.line_command import LineCommandAdapter

    codes = iter(["TRAY-C02", "ERROR"])
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    port = server.getsockname()[1]

    def serve():
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            with conn:
                buffer = b""
                while b"\r" not in buffer:
                    chunk = conn.recv(64)
                    if not chunk:
                        break
                    buffer += chunk
                command = buffer.strip().decode()
                reply = "SR-1000,SR-1000,RD-01,1.0" if command == "*IDN?" else next(codes) if command == "LON" else "IDLE"
                conn.sendall(reply.encode() + b"\r")

    threading.Thread(target=serve, daemon=True).start()
    config = {
        "transport": {"kind": "tcp", "host": "127.0.0.1", "port": port}, "request_timeout_sec": 1,
        "write_terminator": "\r", "read_terminator": "\r", "error_pattern": "^ERROR",
        "identity": {"send": "*IDN?", "pattern": "^(?P<vendor>[^,]*),(?P<model>[^,]*),(?P<device_id>[^,]*),(?P<firmware>.*)$"},
        "capabilities": {"cap.scan": {"start": [{"send": "LON"}], "result": {"pattern": "^(?P<barcode>[\\w-]+)$"}}},
        "status": {"send": "STAT?", "pattern": "^(?P<state>\\w+)$", "states": {"IDLE": "idle"}},
    }
    reader = LineCommandAdapter(record("读码器", config))
    read = reader.submit(request("CMD-SCAN", capability="cap.scan", params={}))
    assert read.state == "done" and read.delivered["barcode"] == "TRAY-C02" and not read.telemetry
    with pytest.raises(AdapterError, match="ERROR"):
        reader.submit(request("CMD-NOREAD", capability="cap.scan", params={}))
    server.close()
