"""SiLA 2 契约（`devices/contracts/sila2`）：按 SiLA 官方 XSD 解析，关键约定不走样，能用固定版本的 sila2 库往返收发。

往返用例同时守着 sila2 0.14.0 的两处限制：结构体里不能放带约束的列表（客户端解不开）；`Any` 值的类型 XML
必须带 SiLA 命名空间（`AllowedTypes` 按字符串比对）。
"""
from __future__ import annotations

import socket
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from xml.etree import ElementTree

import pytest

CONTRACTS = Path(__file__).resolve().parents[3] / "devices" / "contracts" / "sila2"
NS = {"sila": "http://www.sila-standard.org"}
SILA_TYPE = '<DataType xmlns="http://www.sila-standard.org"><Basic>{}</Basic></DataType>'


def _feature(name: str):
    from sila2.framework import Feature

    return Feature((CONTRACTS / f"{name}.sila.xml").read_text(encoding="utf-8"))


def _declared_errors(name: str, command: str) -> set[str]:
    root = ElementTree.parse(CONTRACTS / f"{name}.sila.xml").getroot()
    for node in root.findall("sila:Command", NS):
        if node.findtext("sila:Identifier", namespaces=NS) == command:
            return {item.text for item in node.findall("sila:DefinedExecutionErrors/sila:Identifier", NS)}
    raise AssertionError(f"{name} 没有命令 {command}")


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_contracts_parse_against_the_sila_schema():
    identifiers = {
        name: str(_feature(name).fully_qualified_identifier)
        for name in ("DeviceInfo", "PointAccess", "TaskExecution", "SimulatorControl")
    }
    assert identifiers == {
        "DeviceInfo": "ai.ses/ilcs/DeviceInfo/v1", "PointAccess": "ai.ses/ilcs/PointAccess/v1",
        "TaskExecution": "ai.ses/ilcs/TaskExecution/v1", "SimulatorControl": "ai.ses/ilcs/SimulatorControl/v1",
    }


def test_every_refusal_is_declared_on_the_command_that_raises_it():
    """sila2 把命令没声明的定义错误改成未定义错误：「设备没动」就成了「结果未知」，只能转人工核查。"""
    assert _declared_errors("TaskExecution", "SubmitTask") == {
        "InvalidParameters", "Interlocked", "DeviceBusy", "NotSupported", "DeviceUnreachable",
    }
    assert "DeviceUnreachable" in _declared_errors("TaskExecution", "QueryTask")
    assert _declared_errors("PointAccess", "WritePoint") == {
        "UnknownPoint", "NotWritable", "ControlPoint", "OutOfRange", "InvalidValue", "DeviceBusy",
        "WriteRejected", "DeviceUnreachable", "RequestConflict", "WriteUnconfirmed",
    }


def test_round_trip_with_the_pinned_sila2_library():
    from sila2.client import SilaClient
    from sila2.framework.data_types.any import SilaAnyType
    from sila2.framework.errors.defined_execution_error import DefinedExecutionError
    from sila2.server import FeatureImplementationBase, SilaServer

    info, points, tasks = _feature("DeviceInfo"), _feature("PointAccess"), _feature("TaskExecution")
    moment = datetime.now(timezone.utc)

    def value(raw):
        kind = {bool: "Boolean", int: "Integer", float: "Real", str: "String"}[type(raw)]
        return SilaAnyType(SILA_TYPE.format(kind), raw)

    class Info(FeatureImplementationBase):
        def get_Identity(self, *, metadata):
            return {"DeviceId": "DEV-1", "IdentitySource": "device", "Vendor": "v", "Model": "m",
                    "SerialNumber": "DEV-1", "Firmware": "1", "Simulator": True}

        def get_Status(self, *, metadata):
            raise DefinedExecutionError(info.defined_execution_errors["DeviceUnreachable"], "设备没有应答")

        def get_Driver(self, *, metadata):
            return {"Plugin": "modbus_map", "PluginVersion": "1", "HostVersion": "1", "ConfigVersion": "r1",
                    "ConfigDigest": "sha256:" + "a" * 64, "OfflineAfterSeconds": 30.0}

    class Points(FeatureImplementationBase):
        def get_Points(self, *, metadata):
            return [{"Name": "sp", "Label": "设定温度", "Unit": "℃", "ValueType": "Real", "Writable": True,
                     "Minimum": [0.0], "Maximum": [], "Control": False}]

        def ReadPoints(self, Names, *, metadata):
            return [{"Name": "sp", "Value": value(25.5), "Quality": "good", "ObservedAt": moment, "Error": ""}]

        def WritePoint(self, RequestId, Name, Value, *, metadata):
            if Name != "sp":
                raise DefinedExecutionError(points.defined_execution_errors["UnknownPoint"], Name)
            return {"Before": value(25.5), "After": value(Value.value), "Matches": True, "ObservedAt": moment}

    class Tasks(FeatureImplementationBase):
        def SubmitTask(self, CommandId, TaskType, Capability, ParametersJson, ContextJson, *, metadata):
            raise DefinedExecutionError(tasks.defined_execution_errors["NotSupported"], f"不支持 {Capability}")

        def QueryTask(self, CommandId, *, metadata):
            raise DefinedExecutionError(tasks.defined_execution_errors["DeviceUnreachable"], "设备没有应答")

        def HoldTask(self, CommandId, TargetCommandId, *, metadata):
            raise NotImplementedError

        def AbortTask(self, CommandId, TargetCommandId, *, metadata):
            raise NotImplementedError

        def get_DeviceIdentity(self, *, metadata):
            return "{}"

        def get_TaskSupport(self, *, metadata):
            return {"Capabilities": [{"Capability": "cap.x", "ParametersSchema": '{"type": "object"}', "Programs": []}],
                    "SupportsHold": False, "SupportsAbort": False, "SupportsQuery": True, "SupportsDedup": True,
                    "Handoff": "async"}

    port = _free_port()
    server = SilaServer(server_name="ContractTest", server_type="ILCSContractTest", server_description="契约往返",
                        server_version="1.0", server_vendor_url="https://ses.ai", server_uuid=uuid4())
    for feature, implementation in ((info, Info), (points, Points), (tasks, Tasks)):
        server.set_feature_implementation(feature, implementation(server))
    server.start_insecure("127.0.0.1", port, enable_discovery=False)
    try:
        client = SilaClient("127.0.0.1", port, insecure=True)
        assert client.DeviceInfo.Identity.get().DeviceId == "DEV-1"
        assert client.DeviceInfo.Driver.get().OfflineAfterSeconds == 30.0
        with pytest.raises(DefinedExecutionError) as offline:
            client.DeviceInfo.Status.get()
        assert offline.value.identifier == "DeviceUnreachable"

        [point] = client.PointAccess.Points.get()
        assert (point.Minimum, point.Maximum) == ([0.0], [])
        [reading] = client.PointAccess.ReadPoints(Names=[]).Values
        assert (reading.Value.value, reading.Quality) == (25.5, "good")
        outcome = client.PointAccess.WritePoint(RequestId="pw-1", Name="sp", Value=value(30.0)).Outcome
        assert (outcome.Before.value, outcome.After.value, outcome.Matches) == (25.5, 30.0, True)
        with pytest.raises(DefinedExecutionError) as unknown:
            client.PointAccess.WritePoint(RequestId="pw-2", Name="nope", Value=value(1))
        assert unknown.value.identifier == "UnknownPoint"

        with pytest.raises(DefinedExecutionError) as refused:
            client.TaskExecution.SubmitTask(
                CommandId="c-1", TaskType="dispatch", Capability="cap.y", ParametersJson="{}", ContextJson="{}",
            )
        assert refused.value.identifier == "NotSupported"
        with pytest.raises(DefinedExecutionError) as unreachable:
            client.TaskExecution.QueryTask(CommandId="c-1")
        assert unreachable.value.identifier == "DeviceUnreachable"
        assert client.TaskExecution.TaskSupport.get().Handoff == "async"
    finally:
        server.stop(grace_period=0)
