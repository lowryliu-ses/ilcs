"""按 ILCS 任务契约编程的设备经驱动宿主（插件 `modbus_task` / `opcua_task`）：ILCS 只经 SiLA 2 下发，指令号、去重、查询都在
设备侧，宿主不记台账；明确拒绝带 SiLA 错误码。外部模拟设备走真实 Modbus TCP / OPC UA。"""
from __future__ import annotations

import json
import time

import pytest

from conftest import client, free_port, running_host, token, write_site
from plugin_harness import MODBUS_MAP, modbus_sim, opcua_sim


def _submit(c, command_id: str, params=None, capability: str = "cap.vacuum_dry") -> dict:
    return json.loads(c.TaskExecution.SubmitTask(
        CommandId=command_id, TaskType="dispatch", Capability=capability,
        ParametersJson=json.dumps(params if params is not None else {"temp": 120, "vacuum": 1}),
        ContextJson=json.dumps({"batch_id": "B-1", "step_index": 0, "step_id": "s01"}), metadata=token(c),
    ).ResultJson)


def _wait(c, device, command_id: str, seconds: float = 8) -> dict:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        device.tick()
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


def test_modbus_task_registers_device_behind_the_host(tmp_path):
    with modbus_sim() as (device, _, port):
        entry = {"plugin": "modbus_task", "port": free_port(), "simulator": True,
                 "supports": {"hold": True, "abort": True, "query": True, "dedup": True},
                 "config": {"host": "127.0.0.1", "port": port, "request_timeout_sec": 1, "connect_timeout_sec": 1,
                            "expected_device_id": "SIM-MB-T", **MODBUS_MAP}}
        with running_host(write_site(tmp_path, {"MB-1": entry})):
            c = client(entry["port"])
            identity = c.DeviceInfo.Identity.get(metadata=token(c))
            assert (identity.DeviceId, identity.IdentitySource, identity.Simulator) == ("SIM-MB-T", "device", True)
            status = c.DeviceInfo.Status.get(metadata=token(c))
            assert (status.State, status.AcceptsCommands, status.Interlock) == ("unknown", True, False), \
                "契约里没有运行中这一项：状态报 unknown，接不接指令看设备标志"
            assert c.DeviceInfo.Driver.get(metadata=token(c)).OfflineAfterSeconds == 30.0, "心跳计数多久不变算停了"
            assert "PointAccess" not in " ".join(c.SiLAService.ImplementedFeatures.get()), "契约设备没有点表"

            support = c.TaskExecution.TaskSupport.get(metadata=token(c))
            capabilities = {row.Capability: json.loads(row.ParametersSchema) for row in support.Capabilities}
            assert set(capabilities) == set(MODBUS_MAP["capabilities"])
            assert set(capabilities["cap.vacuum_dry"]["properties"]) == set(MODBUS_MAP["params"])
            assert support.Handoff == "sync"

            assert _submit(c, "CMD-1")["state"] == "accepted"
            assert _submit(c, "CMD-1")["command_id"] == "CMD-1"
            assert device.executions["CMD-1"] == 1, "去重在设备侧：同一指令号只动作一次"
            done = _wait(c, device, "CMD-1")
            assert done["state"] == "done" and abs(done["delivered"]["temp"] - 120) < 1
            not_found = json.loads(c.TaskExecution.QueryTask(CommandId="CMD-NEVER", metadata=token(c)).ResultJson)
            assert not_found["state"] == "not_found"

            device.set_fault("interlock")
            assert _refused(lambda: _submit(c, "CMD-I")) == "Interlocked"
            device.set_fault("busy")
            assert _refused(lambda: _submit(c, "CMD-B")) == "DeviceBusy"
            device.set_fault("none")
            assert _refused(lambda: _submit(c, "CMD-X", capability="cap.unknown")) == "NotSupported"
            assert "CMD-I" not in device.executions and "CMD-B" not in device.executions


def test_unreachable_task_contract_device_is_result_unknown_not_a_refusal(tmp_path):
    """提交时连不上：分不清触发写没写下，回结果未知（之后按指令号问设备），不说「设备没动」。"""
    entry = {"plugin": "modbus_task", "port": free_port(), "simulator": True,
             "supports": {"hold": False, "abort": False, "query": True, "dedup": True},
             "config": {"host": "127.0.0.1", "port": free_port(), "request_timeout_sec": 0.5,
                        "connect_timeout_sec": 0.5, **MODBUS_MAP}}
    with running_host(write_site(tmp_path, {"MB-2": entry})):
        c = client(entry["port"])
        with pytest.raises(Exception) as caught:
            _submit(c, "CMD-U")
        assert "结果未知" in str(caught.value)


def test_opcua_task_execution_device_behind_the_host(tmp_path):
    with opcua_sim() as (device, _, port):
        entry = {"plugin": "opcua_task", "port": free_port(), "simulator": True,
                 "supports": {"hold": True, "abort": True, "query": True, "dedup": True},
                 "config": {"endpoint": f"opc.tcp://127.0.0.1:{port}/ilcs/", "security_policy": "None",
                            "request_timeout_sec": 2, "connect_timeout_sec": 1}}
        with running_host(write_site(tmp_path, {"UA-1": entry})):
            c = client(entry["port"])
            assert c.DeviceInfo.Identity.get(metadata=token(c)).DeviceId == "SIM-UA-T"
            assert _submit(c, "CMD-U1")["state"] == "accepted"
            assert _submit(c, "CMD-U1")["command_id"] == "CMD-U1"
            assert device.executions["CMD-U1"] == 1
            assert _wait(c, device, "CMD-U1")["state"] == "done"
            device.set_fault("busy")
            assert _refused(lambda: _submit(c, "CMD-U2")) == "DeviceBusy"
            device.set_fault("none")
