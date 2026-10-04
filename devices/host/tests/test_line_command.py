"""串口 / TCP 文本命令插件（line_command）经 SiLA 2：外部模拟设备 devices/simulators/line_device（真空干燥箱方言）走真实 TCP。

设备不认识 ILCS 指令号：去重、重启后按原指令号查询都靠插件的作业台账；拒绝带 SiLA 错误码。
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import time

import pytest

from conftest import client, free_port, running_host, token, write_site

OVEN = {
    "identity": {"send": "*IDN?", "pattern": "^(?P<vendor>[^,]*),(?P<model>[^,]*),(?P<device_id>[^,]*),(?P<firmware>.*)$"},
    "ready": {"send": "REM?", "pattern": "^(?P<value>\\w+)$", "ok": ["REMOTE"]},
    "interlock": {"send": "DOOR?", "pattern": "^(?P<value>\\w+)$", "ok": ["CLOSED"]},
    "error_pattern": "^ERR",
    "capabilities": {"cap.vacuum_dry": {"program": "VD-120", "start": [
        {"send": "PROG {program}", "expect": "^OK$"},
        {"send": "SP {temp:.1f}", "expect": "^OK$"},
        {"send": "VAC {vacuum:.2f}", "expect": "^OK$"},
        {"send": "RUN", "expect": "^OK$"},
    ]}},
    "status": {"send": "STAT?", "pattern": "^(?P<state>[A-Z]+)(,(?P<detail>.*))?$",
               "states": {"IDLE": "idle", "RUN": "running", "HOLD": "held", "DONE": "done", "ALARM": "failed"}},
    "actuals": [{"send": "PV?", "pattern": "^(?P<temp>[-\\d.]+),(?P<vacuum>[-\\d.]+)$"}],
    "abort": [{"send": "STOP", "expect": "^OK$"}],
    "acknowledge": [{"send": "ACK", "expect": "^OK$"}],
    "points": {"temp": {"send": "PV?", "pattern": "^(?P<value>[-\\d.]+),", "unit": "℃"}},
}


@contextmanager
def oven_sim(task_seconds: float = 0.3):
    from simulators.common.device import SimulatedDevice
    from simulators.line_device.server import SimulatorRunner, parse

    port = free_port()
    args = parse(["--dialect", "oven", "--device-id", "SIM-OVEN-H", "--address", "127.0.0.1", "--port", str(port)])
    runner = SimulatorRunner(args, SimulatedDevice("SIM-OVEN-H", "generic", task_seconds=task_seconds,
                                                   methods=[{"program": "VD-120"}]))
    runner.start()
    try:
        yield runner, port
    finally:
        runner.stop()


def _device(port: int) -> dict:
    return {"plugin": "line_command", "port": free_port(), "simulator": True,
            "supports": {"hold": False, "abort": True, "query": True, "dedup": True},
            "config": {"transport": {"kind": "tcp", "host": "127.0.0.1", "port": port}, "request_timeout_sec": 1,
                       "connect_timeout_sec": 1, **OVEN}}


def _submit(c, command_id: str, params: dict) -> dict:
    return json.loads(c.TaskExecution.SubmitTask(
        CommandId=command_id, TaskType="dispatch", Capability="cap.vacuum_dry", ParametersJson=json.dumps(params),
        ContextJson=json.dumps({"batch_id": "B-1", "step_index": 0, "step_id": "s01", "station_id": "ST-T"}),
        metadata=token(c),
    ).ResultJson)


def _wait(c, runner, command_id: str, seconds: float = 8) -> dict:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        runner.device.tick()
        result = json.loads(c.TaskExecution.QueryTask(CommandId=command_id, metadata=token(c)).ResultJson)
        if result["state"] in {"done", "failed"}:
            return result
        time.sleep(0.05)
    raise AssertionError(f"{command_id} 没有在时限内结束")


def _refused(call) -> str:
    from sila2.framework.errors.defined_execution_error import DefinedExecutionError

    with pytest.raises(DefinedExecutionError) as caught:
        call()
    return caught.value.identifier


def test_text_command_device_runs_through_the_host(tmp_path):
    with oven_sim() as (runner, port):
        device = _device(port)
        with running_host(write_site(tmp_path, {"OVEN-1": device})):
            c = client(device["port"])
            identity = c.DeviceInfo.Identity.get(metadata=token(c))
            assert (identity.DeviceId, identity.IdentitySource, identity.Simulator) == ("SIM-OVEN-H", "device", True)
            assert c.DeviceInfo.Driver.get(metadata=token(c)).Plugin == "line_command"
            [temp] = c.PointAccess.ReadPoints(Names=["temp"], metadata=token(c)).Values
            assert (temp.Quality, temp.Error) == ("good", "") and isinstance(temp.Value.value, float)

            assert _submit(c, "CMD-1", {"temp": 120, "vacuum": 1})["state"] == "accepted"
            assert _submit(c, "CMD-1", {"temp": 120, "vacuum": 1})["state"] in {"accepted", "running", "done"}
            done = _wait(c, runner, "CMD-1")
            assert done["state"] == "done", done
            assert abs(done["delivered"]["temp"] - 120) < 1 and abs(done["delivered"]["vacuum"] - 1) < 0.1
            assert sum(runner.device.executions.values()) == 1, "同一指令号重投只回放，设备只 RUN 一次"

            # 逐孔：每孔一套设定、各自 RUN；缺参数的逐孔指令提交时整条拒绝，一条命令都不发
            assert _refused(lambda: _submit(c, "CMD-W0", {"wells": {"A1": {"temp": 1}}})) == "InvalidParameters"
            assert runner.dialect.setpoints["temp"] == 120, "被拒的逐孔指令没有改设备上的设定"
            _submit(c, "CMD-W", {"vacuum": 2, "wells": {"B1": {"temp": 90}, "A1": {"temp": 80}}})
            wells = _wait(c, runner, "CMD-W")["delivered"]["wells"]
            assert {well: round(row["temp"]) for well, row in wells.items()} == {"A1": 80, "B1": 90}
            assert sum(runner.device.executions.values()) == 3

            # 联锁：启动之前就拒绝，带 SiLA 错误码
            runner.device.set_fault("interlock")
            assert _refused(lambda: _submit(c, "CMD-I", {"temp": 120, "vacuum": 1})) == "Interlocked"
            runner.device.set_fault("none")
            assert sum(runner.device.executions.values()) == 3
