"""驱动宿主 × 外部 PLC 模拟设备：经 SiLA 调用，三组特性、错误码、令牌、重启后查回、在途作业时不许换配置。"""
from __future__ import annotations

import json
import time

import pytest

from conftest import (TOKEN, client, free_port, freezable_proxy, modbus_points, plc_mapping, plc_sim, running_host,
                      silent_port, token, write_site)


def _plc_device(port: int, **extra) -> dict:
    points = modbus_points()
    points["sp_thickness"] = {**points["sp_thickness"], "writable": True, "min": 0, "max": 500, "unit": "µm",
                              "label": "厚度设定"}
    config = {"host": "127.0.0.1", "port": port, "unit_id": 1, "request_timeout_sec": 1, "start_timeout_sec": 5,
              **plc_mapping(points)}
    return {"plugin": "modbus_map", "port": free_port(),
            "simulator": True, "config_version": "r1",
            "supports": {"hold": True, "abort": True, "query": True, "dedup": True}, "config": config, **extra}


def _submit(c, command_id: str, params=None, capability: str = "cap.coat") -> dict:
    raw = c.TaskExecution.SubmitTask(
        CommandId=command_id, TaskType="dispatch", Capability=capability,
        ParametersJson=json.dumps(params if params is not None else {"thickness": 180, "temp": 110}),
        ContextJson=json.dumps({"batch_id": "B-1", "step_index": 0, "step_id": "s01", "station_id": "ST-T"}),
        metadata=token(c),
    ).ResultJson
    return json.loads(raw)


def _query(c, command_id: str) -> dict:
    return json.loads(c.TaskExecution.QueryTask(CommandId=command_id, metadata=token(c)).ResultJson)


def _wait(c, command_id: str, seconds: float = 6) -> dict:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        receipt = _query(c, command_id)
        if receipt["state"] in {"done", "failed"}:
            return receipt
        time.sleep(0.1)
    raise AssertionError(f"{command_id} 没有在时限内结束")


def _refused(call) -> str:
    from sila2.framework.errors.defined_execution_error import DefinedExecutionError

    with pytest.raises(DefinedExecutionError) as caught:
        call()
    return caught.value.identifier


def _point(value):
    from ilcs_host.values import to_any

    return to_any(value)


def test_device_service_end_to_end(tmp_path, plc):
    program, plc_port = plc
    device = _plc_device(plc_port)
    with running_host(write_site(tmp_path, {"PLC-1": device})) as site:
        c = client(device["port"])
        features = set(c.SiLAService.ImplementedFeatures.get())
        assert {"ai.ses/ilcs/DeviceInfo/v1", "ai.ses/ilcs/PointAccess/v1", "ai.ses/ilcs/TaskExecution/v1",
                "org.silastandard/core/AuthorizationService/v1"} <= features

        identity = c.DeviceInfo.Identity.get(metadata=token(c))
        assert (identity.DeviceId, identity.IdentitySource, identity.Simulator) == ("SIM-PLC-T", "device", True)
        status = c.DeviceInfo.Status.get(metadata=token(c))
        assert (status.State, status.Interlock, status.AcceptsCommands, status.ActiveCommandId) == ("idle", False, True, "")
        driver = c.DeviceInfo.Driver.get(metadata=token(c))
        assert (driver.Plugin, driver.ConfigVersion, driver.ConfigDigest) == ("modbus_map", "r1", site.devices[0].digest)
        assert driver.OfflineAfterSeconds == 5.0, "PLC 心跳超时就是发现失联要的时间"

        points = {row.Name: row for row in c.PointAccess.Points.get(metadata=token(c))}
        assert (points["sp_thickness"].Writable, points["sp_thickness"].Minimum, points["sp_thickness"].Unit) == (
            True, [0.0], "µm")
        assert points["cmd_start"].Control and not points["cmd_start"].Writable
        assert (points["state"].ValueType, points["serial"].ValueType, points["remote"].ValueType) == (
            "Integer", "String", "Boolean")
        [state] = c.PointAccess.ReadPoints(Names=["state"], metadata=token(c)).Values
        assert (state.Value.value, state.Quality, state.Error) == (0, "good", "")

        outcome = c.PointAccess.WritePoint(RequestId="pw-1", Name="sp_thickness", Value=_point(150.0),
                                           metadata=token(c)).Outcome
        assert (outcome.After.value, outcome.Matches) == (150.0, True)
        replay = c.PointAccess.WritePoint(RequestId="pw-1", Name="sp_thickness", Value=_point(150.0),
                                          metadata=token(c)).Outcome
        assert replay.ObservedAt == outcome.ObservedAt, "同一请求号回放原结论，不再写"
        write = c.PointAccess.WritePoint
        assert _refused(lambda: write(RequestId="pw-1", Name="sp_thickness", Value=_point(160.0),
                                      metadata=token(c))) == "RequestConflict"
        assert _refused(lambda: write(RequestId="pw-2", Name="sp_thickness", Value=_point(9999.0),
                                      metadata=token(c))) == "OutOfRange"
        assert _refused(lambda: write(RequestId="pw-3", Name="cmd_start", Value=_point(True),
                                      metadata=token(c))) == "NotWritable"
        assert _refused(lambda: write(RequestId="pw-4", Name="nope", Value=_point(1.0),
                                      metadata=token(c))) == "UnknownPoint"
        assert _refused(lambda: write(RequestId="pw-5", Name="sp_thickness", Value=_point("厚"),
                                      metadata=token(c))) == "InvalidValue"

        support = c.TaskExecution.TaskSupport.get(metadata=token(c))
        [capability] = support.Capabilities
        schema = json.loads(capability.ParametersSchema)
        assert capability.Capability == "cap.coat" and set(schema["properties"]) == {"thickness", "temp"}
        assert schema["properties"]["thickness"] == {"type": "number", "unit": "µm", "minimum": 0, "maximum": 500}
        assert support.Handoff == "async" and support.SupportsHold

        assert _submit(c, "CMD-1")["state"] == "accepted"
        assert _submit(c, "CMD-1")["command_id"] == "CMD-1"
        assert sum(program.device.executions.values()) == 1, "同一指令号只给一次启动沿"
        done = _wait(c, "CMD-1")
        assert done["state"] == "done" and abs(done["delivered"]["thickness"] - 180) < 2
        assert done["device_ts"].endswith("+00:00"), "设备时间带时区"
        assert _query(c, "CMD-NEVER")["state"] == "not_found"
        assert _refused(lambda: _submit(c, "CMD-X", capability="cap.other")) == "NotSupported"


def test_wells_run_one_after_another_and_report_each_well(tmp_path, plc):
    """逐孔参数（ILCS 矩阵条件）：按孔位顺序一个一个跑——写设定、启动、等完成、取实测、复位，再下一孔；回执按孔位回报。
    每孔一个启动沿、一个运行号（<指令号>/<序号>）；固定参数是每孔的缺省值。"""
    program, plc_port = plc
    device = _plc_device(plc_port)
    with running_host(write_site(tmp_path, {"PLC-1": device})):
        c = client(device["port"])
        params = {"thickness": 180, "wells": {"A2": {"temp": 120}, "A10": {"temp": 140}, "A1": {"temp": 110}}}
        assert _submit(c, "CMD-W", params)["state"] == "accepted"
        done = _wait(c, "CMD-W", seconds=15)
        assert done["state"] == "done", done
        wells = done["delivered"]["wells"]
        assert set(wells) == {"A1", "A2", "A10"}
        assert all(abs(wells[well]["temp"] - target) < 2.5 for well, target in (("A1", 110), ("A2", 120), ("A10", 140)))
        assert all(abs(row["thickness"] - 180) < 2.5 for row in wells.values()), "固定参数是每孔的缺省值"
        assert sum(program.device.executions.values()) == 3, "每孔一个启动沿"
        assert program.memory["JobLatched"] == "CMD-W/3", "每孔写自己的运行号：最后锁存的是第 3 孔（A10）的"
        assert _submit(c, "CMD-W", params)["state"] == "done", "重复投递回放原作业，不再动作"
        assert sum(program.device.executions.values()) == 3

        bad = {"thickness": 180, "wells": {"A1": {"temp": 110}, "A2": {"pressure": 3}}}
        assert _refused(lambda: _submit(c, "CMD-BAD", bad)) == "InvalidParameters"
        assert sum(program.device.executions.values()) == 3, "有一孔参数不对：整条拒绝，设备一次都没动"


def test_restart_mid_sequence_continues_without_rerunning_started_wells(tmp_path):
    """逐孔跑到一半驱动宿主重启：按台账接着查当前这一孔，做完再启动后面的孔；已经启动过的孔不重发。"""
    with plc_sim(task_seconds=0.8) as (program, plc_port):
        device = _plc_device(plc_port)
        site_dir = write_site(tmp_path, {"PLC-1": device})
        params = {"thickness": 180, "wells": {"A1": {"temp": 110}, "A2": {"temp": 120}, "A3": {"temp": 130}}}
        with running_host(site_dir):
            c = client(device["port"])
            _submit(c, "CMD-R", params)
            deadline = time.monotonic() + 10
            while sum(program.device.executions.values()) < 2 and time.monotonic() < deadline:
                _query(c, "CMD-R")
                time.sleep(0.1)
            assert sum(program.device.executions.values()) == 2, "停在第二孔运行中"
        with running_host(site_dir):
            c = client(device["port"])
            done = _wait(c, "CMD-R", seconds=15)
        assert done["state"] == "done" and set(done["delivered"]["wells"]) == {"A1", "A2", "A3"}, done
        assert sum(program.device.executions.values()) == 3 and program.memory["JobLatched"] == "CMD-R/3", "重启后没有重发"


def test_calls_without_a_valid_token_are_refused(tmp_path, plc):
    from sila2.framework.abc.sila_error import SilaError

    _, plc_port = plc
    device = _plc_device(plc_port)
    with running_host(write_site(tmp_path, {"PLC-1": device})):
        c = client(device["port"])
        assert c.SiLAService.ServerName.get() == "PLC-1", "SiLAService 不受令牌约束"
        with pytest.raises(SilaError):
            c.DeviceInfo.Identity.get()
        with pytest.raises(SilaError) as wrong:
            c.DeviceInfo.Identity.get(metadata=token(c, "w" * 40))
        assert "InvalidAccessToken" in repr(wrong.value) or "令牌无效" in str(wrong.value)
        assert c.DeviceInfo.Identity.get(metadata=token(c, TOKEN)).DeviceId == "SIM-PLC-T"


def test_busy_device_refuses_jobs_and_manual_writes(tmp_path):
    with plc_sim(task_seconds=30) as (_, plc_port):
        device = _plc_device(plc_port)
        with running_host(write_site(tmp_path, {"PLC-1": device})):
            c = client(device["port"])
            assert _submit(c, "CMD-LONG")["state"] == "accepted"
            deadline = time.monotonic() + 3
            while c.DeviceInfo.Status.get(metadata=token(c)).State != "running" and time.monotonic() < deadline:
                time.sleep(0.1)
            assert c.DeviceInfo.Status.get(metadata=token(c)).ActiveCommandId == "CMD-LONG"
            assert _refused(lambda: _submit(c, "CMD-NEXT")) == "DeviceBusy"
            assert _refused(lambda: c.PointAccess.WritePoint(
                RequestId="pw-busy", Name="sp_thickness", Value=_point(100.0), metadata=token(c))) == "DeviceBusy"


def test_unreachable_device_is_refused_before_acting(tmp_path):
    with plc_sim() as (_, plc_port):
        pass  # 模拟 PLC 已经停了：端口上没人
    device = _plc_device(plc_port)
    with running_host(write_site(tmp_path, {"PLC-1": device})):
        c = client(device["port"])
        assert _refused(lambda: _submit(c, "CMD-OFF")) == "DeviceUnreachable", "台账里还没有这条作业：设备没动"
        assert _refused(lambda: c.DeviceInfo.Status.get(metadata=token(c))) == "DeviceUnreachable"
        assert _query(c, "CMD-OFF")["state"] == "not_found", "台账里没有就是没见过，不用问设备"


def test_silent_device_is_answered_within_one_timeout(tmp_path):
    """设备卡死不回话（连接建得上、请求没回音）：读点一个超时就答复、后面的点不再读；写点读不到当前值就明确没写。
    宿主要赶在 ILCS 的截止时间之前答复，否则 ILCS 只能按「结果未知」处理一次其实没发出去的写入。"""
    points = modbus_points()
    points["sp_thickness"] = {**points["sp_thickness"], "writable": True, "min": 0, "max": 500}
    with silent_port() as plc_port:
        device = {"plugin": "modbus_map", "port": free_port(), "simulator": True,
                  "config": {"host": "127.0.0.1", "port": plc_port, "unit_id": 1, "request_timeout_sec": 0.5,
                             "points": points}}
        with running_host(write_site(tmp_path, {"PLC-1": device})):
            c = client(device["port"])
            started = time.monotonic()
            values = c.PointAccess.ReadPoints(Names=[], metadata=token(c)).Values
            took = time.monotonic() - started
            assert len(values) == len(points) and all(row.Quality == "bad" and row.Error for row in values)
            assert took < 3, f"读 {len(values)} 个点等了 {took:.1f} s：每个点都等满了超时"
            started = time.monotonic()
            assert _refused(lambda: c.PointAccess.WritePoint(
                RequestId="pw-silent", Name="sp_thickness", Value=_point(100.0), metadata=token(c))) == "DeviceUnreachable"
            assert time.monotonic() - started < 3


def test_link_that_goes_silent_mid_session_is_answered_within_one_timeout(tmp_path):
    """OPC UA 会话建好之后断网（连接还挂着、没有回音）：读点一个超时就答复，关旧会话不再多等一个超时。"""
    prefix = "nsu=urn:ilcs:sim:plc;s=Coater."
    with plc_sim("opcua") as (_, plc_port), freezable_proxy(plc_port) as proxy:
        device = {"plugin": "opcua_map", "port": free_port(), "simulator": True, "device_id": "PF-OPCUA-CFG",
                  "config": {"endpoint": f"opc.tcp://127.0.0.1:{proxy.port}/plc/", "security_policy": "None",
                             "request_timeout_sec": 1.5, "connect_timeout_sec": 1,
                             "points": {"pressure": prefix + "PV_temp", "setpoint": prefix + "SP_temp"}}}
        with running_host(write_site(tmp_path, {"PF-OPCUA": device})):
            c = client(device["port"])
            assert all(row.Quality == "good" for row in c.PointAccess.ReadPoints(Names=[], metadata=token(c)).Values)
            proxy.freeze()
            started = time.monotonic()
            values = c.PointAccess.ReadPoints(Names=[], metadata=token(c)).Values
            took = time.monotonic() - started
            assert all(row.Quality == "bad" for row in values)
            assert took < 2.5, f"等了 {took:.1f} s：关旧会话又等了一个超时"


def test_mapped_identity_that_reads_blank_is_missing_not_the_configured_id(tmp_path, plc):
    """点表映射了设备编号、设备却报空（接错了设备、PLC 没配编号）：报缺失，不拿现场配置里的编号顶替。"""
    _, plc_port = plc
    device = _plc_device(plc_port, device_id="SIM-PLC-T")
    device["config"]["identity"] = {"device_id": "job_latched", "model": "model"}  # 还没下发过作业：读出来是空的
    with running_host(write_site(tmp_path, {"PLC-1": device})):
        c = client(device["port"])
        identity = c.DeviceInfo.Identity.get(metadata=token(c))
        assert (identity.DeviceId, identity.IdentitySource) == ("", "device")


def test_restarted_host_answers_from_the_journal_without_resending(tmp_path):
    with plc_sim(task_seconds=1.5) as (program, plc_port):
        device = _plc_device(plc_port)
        site_dir = write_site(tmp_path, {"PLC-1": device})
        with running_host(site_dir):
            assert _submit(client(device["port"]), "CMD-R")["state"] == "accepted"
        with running_host(site_dir):  # 宿主重启：新进程、新插件实例，台账在 state_dir 里
            c = client(device["port"])
            assert _wait(c, "CMD-R")["state"] == "done"
            assert _submit(c, "CMD-R")["state"] == "done", "重启后重投同一指令号回放结论"
        assert sum(program.device.executions.values()) == 1


def test_config_cannot_change_under_an_unfinished_job(tmp_path):
    from ilcs_host.plugins import PLUGINS
    from ilcs_host.server import prepare
    from ilcs_host.site import SiteError, load_site

    with plc_sim(task_seconds=30) as (_, plc_port):
        device = _plc_device(plc_port)
        site_dir = write_site(tmp_path, {"PLC-1": device})
        with running_host(site_dir):
            assert _submit(client(device["port"]), "CMD-HOLD-CFG")["state"] == "accepted"
        changed = json.loads((site_dir / "devices" / "PLC-1.json").read_text(encoding="utf-8"))
        changed["config"]["status"]["states"]["3"] = "idle"  # 改了判结论的映射
        (site_dir / "devices" / "PLC-1.json").write_text(json.dumps(changed), encoding="utf-8")
        with pytest.raises(SiteError, match="没结束的作业 CMD-HOLD-CFG"):
            prepare(load_site(site_dir, set(PLUGINS)))
        # 只改超时不算改配置：摘要不变，照常启动
        changed["config"]["status"]["states"]["3"] = "done"
        changed["config"]["request_timeout_sec"] = 2
        (site_dir / "devices" / "PLC-1.json").write_text(json.dumps(changed), encoding="utf-8")
        assert prepare(load_site(site_dir, set(PLUGINS)))


def test_checking_a_new_config_does_not_record_it(tmp_path, plc):
    """`--check` 只检查：检查过新配置不算换过配置，摘要等设备服务真正按它起来了才记。"""
    from ilcs_host.plugins import PLUGINS
    from ilcs_host.server import prepare
    from ilcs_host.site import load_site

    _, plc_port = plc
    device = _plc_device(plc_port)
    with running_host(write_site(tmp_path, {"PLC-1": device})) as site:
        started_with = site.devices[0].digest
    marker = tmp_path / "state" / "PLC-1.digest"
    assert marker.read_text(encoding="utf-8").strip() == started_with
    device["config"]["points"]["sp_thickness"]["max"] = 400
    checked = load_site(write_site(tmp_path, {"PLC-1": device}), set(PLUGINS))
    prepare(checked)  # `python -m ilcs_host --check` 走的就是这一步
    assert checked.devices[0].digest != started_with
    assert marker.read_text(encoding="utf-8").strip() == started_with, "只检查不改状态"


def test_point_only_device_over_opcua_has_no_task_execution(tmp_path):
    with plc_sim("opcua") as (_, plc_port):
        prefix = "nsu=urn:ilcs:sim:plc;s=Coater."
        device = {
            "plugin": "opcua_map", "port": free_port(), "simulator": True,
            "device_id": "PF-OPCUA-CFG",
            "config": {"endpoint": f"opc.tcp://127.0.0.1:{plc_port}/plc/", "security_policy": "None",
                       "request_timeout_sec": 2, "connect_timeout_sec": 1,
                       "points": {"pressure": {"node": prefix + "PV_temp", "unit": "bar", "label": "压力"},
                                  "setpoint": {"node": prefix + "SP_temp", "unit": "bar", "writable": True,
                                               "min": 0, "max": 10}}},
        }
        with running_host(write_site(tmp_path, {"PF-OPCUA": device})):
            c = client(device["port"])
            features = set(c.SiLAService.ImplementedFeatures.get())
            assert "ai.ses/ilcs/PointAccess/v1" in features and "ai.ses/ilcs/TaskExecution/v1" not in features
            identity = c.DeviceInfo.Identity.get(metadata=token(c))
            assert (identity.DeviceId, identity.IdentitySource) == ("PF-OPCUA-CFG", "config"), "设备报不出身份：按配置登记"
            assert c.DeviceInfo.Status.get(metadata=token(c)).State == "idle"
            outcome = c.PointAccess.WritePoint(RequestId="pw-op", Name="setpoint", Value=_point(6.0),
                                               metadata=token(c)).Outcome
            assert (outcome.After.value, outcome.Matches) == (6.0, True)
