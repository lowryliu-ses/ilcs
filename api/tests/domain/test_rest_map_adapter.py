"""`rest_map_v1`（设备自有 REST 接口）× MiR 风格的 AGV 车队模拟设备：真实走 HTTP。

车队接口不认识 ILCS 指令号：驱动把车队返回的任务号记进作业台账，按它查状态、暂停、取消；
启动请求没拿到应答时，按请求里带的 ILCS 指令号（message）在车队队列里找回。
"""
import json
import time
import urllib.request

import pytest

from sim_harness import fleet_config, fleet_sim, record, transfer


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "adapter_state_root", str(tmp_path / "adapter-state"))
    monkeypatch.setattr(settings, "adapter_credential_root", str(tmp_path))
    return tmp_path


def _adapter(port: int, cert_dir, robot: str = "AGV-01", credential: str | None = None, **config):
    from app.adapters.rest_map import RestMapAdapter

    settings_, reference = fleet_config(port, cert_dir, robot, **config)
    return RestMapAdapter(record("REST 接口映射", settings_, reference if credential is None else credential))


def _wait(adapter, command_id: str, seconds: float = 5, states=("done", "failed")):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = adapter.query(command_id)
        if result is not None and result.state in states:
            return result
        time.sleep(0.05)
    raise AssertionError(f"指令 {command_id} 没有在时限内到达 {states}")


def _fault(port: int, robot: str, mode: str, parameter: float = 0):
    body = json.dumps({"robot": robot, "mode": mode, "parameter": parameter}).encode()
    urllib.request.urlopen(urllib.request.Request(
        f"http://127.0.0.1:{port}/simulator/fault", data=body, method="POST",
        headers={"Content-Type": "application/json"}), timeout=5).read()


def test_transfer_runs_to_done_and_duplicates_replay(isolated):
    with fleet_sim(isolated) as (fleet, _, port):
        adapter = _adapter(port, isolated, expected_device_id="AGV-01")
        health = adapter.healthcheck()
        assert (health["device_id"], health["model"], health["simulator"]) == ("AGV-01", "MiR250", True)

        accepted = adapter.submit(transfer("CMD-T1"))
        assert accepted.state == "accepted" and accepted.delivered["remote_id"]
        adapter.submit(transfer("CMD-T1"))
        robot = fleet.robots["AGV-01"]
        assert len(robot.queue) == 1, "重投同一指令号不再往车队加任务"
        done = _wait(adapter, "CMD-T1")
        assert done.state == "done" and robot.position == "ST-05/N1"
        assert sum(robot.executions.values()) == 1


def test_positions_table_is_strict(isolated):
    from app.adapters import AdapterError

    with fleet_sim(isolated) as (fleet, _, port):
        adapter = _adapter(port, isolated, positions={"HOTEL-01/S01": "POS-HOTEL-S01", "ST-06/N1": "POS-GB"})
        with pytest.raises(AdapterError, match="ST-05/N1"):
            adapter.submit(transfer("CMD-P"))
        assert not fleet.robots["AGV-01"].queue, "位置对不上车队站点：一条任务都没下发"
        adapter.submit(transfer("CMD-OK", target="ST-06/N1"))
        parameters = fleet.robots["AGV-01"].queue[0]["parameters"]
        assert parameters == [{"id": "From", "value": "POS-HOTEL-S01"}, {"id": "To", "value": "POS-GB"}]


def test_emergency_stop_bad_mission_and_bad_credentials_are_explicit(isolated):
    from app.adapters import AdapterError

    with fleet_sim(isolated) as (fleet, _, port):
        adapter = _adapter(port, isolated)
        _fault(port, "AGV-01", "estop")
        assert adapter.healthcheck()["interlock"] is True
        with pytest.raises(AdapterError, match="急停"):
            adapter.submit(transfer("CMD-E"))
        _fault(port, "AGV-01", "none")
        broken = _adapter(port, isolated)
        broken.config["capabilities"]["cap.transfer"]["defaults"] = {"mission": "no-such-mission"}
        with pytest.raises(AdapterError, match="HTTP 400"):
            broken.submit(transfer("CMD-M"))
        wrong = isolated / "wrong.json"
        wrong.write_text(json.dumps({"headers": {"Authorization": "Basic d3Jvbmc6d3Jvbmc="}}))
        with pytest.raises(AdapterError, match="401"):
            _adapter(port, isolated, credential=f"file://{wrong}").healthcheck()
        assert not fleet.robots["AGV-01"].queue


def test_lost_response_is_unknown_then_found_by_command_id(isolated):
    from app.adapters import AdapterError, AdapterUnreachable

    with fleet_sim(isolated) as (fleet, _, port):
        adapter = _adapter(port, isolated)
        _fault(port, "AGV-01", "lost_receipt")
        with pytest.raises(AdapterUnreachable) as lost:
            adapter.submit(transfer("CMD-L"))
        assert not isinstance(lost.value, AdapterError)
        _fault(port, "AGV-01", "none")
        found = adapter.query("CMD-L")
        assert found.state in {"accepted", "running"} and found.quality == "good", "按 message 里的指令号找回"
        assert _wait(adapter, "CMD-L").state == "done"
        assert len(fleet.robots["AGV-01"].queue) == 1


def test_hold_resume_abort_and_queue_wait(isolated):
    with fleet_sim(isolated, task_seconds=30) as (fleet, _, port):
        adapter = _adapter(port, isolated, start_timeout_sec=0.2)
        adapter.submit(transfer("CMD-H"))
        time.sleep(0.2)
        assert adapter.hold(transfer("CMD-HOLD", type_="hold")).state == "done"
        assert fleet.robots["AGV-01"].queue[0]["state"] == "Paused"
        resumed = adapter.submit(transfer("CMD-RES", type_="resume"))
        assert resumed.command_id == "CMD-RES" and fleet.robots["AGV-01"].queue[0]["state"] == "Executing"
        assert adapter.abort(transfer("CMD-ABORT", type_="abort")).state == "done"
        aborted = adapter.query("CMD-H")
        assert aborted.state == "failed" and fleet.robots["AGV-01"].queue[0]["state"] == "Aborted"

        # 车队里另一台任务正在执行：我们的任务排队（Pending），超过启动时限也不判结果未知
        other = _adapter(port, isolated, robot="AGV-02", start_timeout_sec=0.2)
        other.submit(transfer("CMD-Q1"))
        other.submit(transfer("CMD-Q2"))
        time.sleep(0.4)
        assert other.query("CMD-Q2").state == "accepted"


def test_restart_answers_from_the_journal(isolated):
    with fleet_sim(isolated) as (fleet, _, port):
        _adapter(port, isolated).submit(transfer("CMD-R"))
        restarted = _adapter(port, isolated)
        assert _wait(restarted, "CMD-R").state == "done"
        assert len(fleet.robots["AGV-01"].queue) == 1
