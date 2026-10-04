"""`opcua_map_v1` / `modbus_map_v1`（PLC 点表映射）× 外部 PLC 模拟设备：真实走 OPC UA / Modbus TCP。

PLC 有自己的点表，不认识 ILCS 指令号：驱动按点写设定值、给启动沿、读状态点，作业台账负责去重与重启对账；
PLC 回显指令号（JobLatched）时，未确认的启动可以找回。
"""
import json
import time

import pytest

from plugin_harness import plc_config, plc_sim, record, request

PROTOCOLS = ("opcua", "modbus")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    from ilcs_host.settings import settings

    monkeypatch.setattr(settings, "state_dir", str(tmp_path / "adapter-state"))
    monkeypatch.setattr(settings, "credential_root", str(tmp_path))
    return tmp_path


def _adapter(protocol: str, port: int, cert_dir=None, **config):
    if protocol == "opcua":
        from ilcs_host.plugins.opcua_map import OpcUaMapAdapter as Adapter
    else:
        from ilcs_host.plugins.modbus_map import ModbusMapAdapter as Adapter
    settings_, credential = plc_config(protocol, port, cert_dir, **config)
    return Adapter(record("PLC 点表", settings_, credential))


def _coat(command_id: str, type_: str = "dispatch", target: str = "", params=None, program: str = ""):
    return request(command_id, type_, target, params if params is not None else {"thickness": 180, "temp": 110},
                   capability="cap.coat", program=program)


def _wait(adapter, command_id: str, seconds: float = 6, states=("done", "failed")):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = adapter.query(command_id)
        if result is not None and result.state in states:
            return result
        time.sleep(0.1)
    raise AssertionError(f"指令 {command_id} 没有在时限内到达 {states}")


def _until(label: str, condition, seconds: float = 5.0) -> None:
    """等 PLC 扫描把状态同步出来：读到为止，不按固定时长猜。PLC 每个扫描周期才把内存写回协议层，
    全量跑时机器忙，一轮扫描可能远比平时慢；固定等 0.2–0.5 s 会偶发地早读一步。"""
    deadline = time.monotonic() + seconds
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"{seconds:g} s 内没等到：{label}")
        time.sleep(0.05)


def _readiness(adapter) -> tuple[bool, bool]:
    """经驱动读（和下发前的就绪 / 联锁检查读的是同一组点）：(接受指令, 联锁)。"""
    identity = adapter.identity()
    return bool(identity.get("accepts_commands", True)), bool(identity.get("interlock"))


def _heartbeat_stopped(adapter) -> bool:
    """心跳隔两个扫描周期不再变：PLC 程序真的停了（最后一轮扫描可能还在把心跳写回协议层）。"""
    before = adapter.read_point_value("heartbeat")
    time.sleep(0.15)
    return adapter.read_point_value("heartbeat") == before


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_point_map_job_runs_to_done_and_replays_duplicates(protocol, isolated):
    cert_dir = isolated if protocol == "opcua" else None
    with plc_sim(protocol, cert_dir, task_seconds=0.4) as (program, _, port):
        adapter = _adapter(protocol, port, cert_dir, expected_device_id="SIM-PLC-T")
        health = adapter.healthcheck()
        assert (health["device_id"], health["simulator"], health["interlock"], health["accepts_commands"]) == (
            "SIM-PLC-T", True, False, True)

        accepted = adapter.submit(_coat("CMD-1"))
        assert accepted.state == "accepted" and accepted.origin == f"real:{protocol}_map_v1"
        adapter.submit(_coat("CMD-1"))
        assert sum(program.device.executions.values()) == 1, "重投同一指令号不再给启动沿"
        assert program.memory["JobLatched"] == "CMD-1", "指令号写进 PLC 并被锁存"

        done = _wait(adapter, "CMD-1")
        assert done.state == "done"
        assert abs(done.delivered["thickness"] - 180) < 2 and abs(done.delivered["temp"] - 110) < 2
        assert {point.metric for point in done.telemetry} == {"thickness", "temp"}

        # 上一个作业停在「完成」：下一条先复位（CmdAck）再启动
        adapter.submit(_coat("CMD-2"))
        assert sum(program.device.executions.values()) == 2
        assert _wait(adapter, "CMD-2").state == "done"


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_restart_and_unconfirmed_start_are_recovered_by_the_job_id_echo(protocol, isolated):
    with plc_sim(protocol, task_seconds=5) as (program, _, port):
        adapter = _adapter(protocol, port)
        adapter.submit(_coat("CMD-E"))
        journal_path = isolated / "adapter-state" / "ST-SIM.json"
        journal = json.loads(journal_path.read_text())
        # 模拟「启动沿已发出但没拿到确认」：台账里只有未确认的记录
        journal["jobs"]["CMD-E"].update({"state": "unconfirmed", "unconfirmed": True})
        journal_path.write_text(json.dumps(journal))

        restarted = _adapter(protocol, port)
        found = restarted.query("CMD-E")
        assert found.state == "running" and found.quality == "good", "PLC 回显了指令号：确认是这条指令在跑"
        assert sum(program.device.executions.values()) == 1


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_writes_before_the_start_edge_and_readiness_are_explicit(protocol):
    from ilcs_host.plugins.base import AdapterError

    recipes = {"COAT-180": 12, "COAT-OLD": 99}
    with plc_sim(protocol, task_seconds=5, recipes="12") as (program, _, port):
        adapter = _adapter(protocol, port, recipes=recipes)
        with pytest.raises(AdapterError, match="recipe.map"):
            adapter.submit(_coat("CMD-M", program="COAT-999"))
        with pytest.raises(AdapterError, match="没有对应的写入点"):
            adapter.submit(_coat("CMD-X", params={"thickness": 180, "temp": 110, "speed": 3}, program="COAT-180"))
        program.device.set_fault("interlock")
        _until("PLC 报联锁", lambda: _readiness(adapter)[1])
        with pytest.raises(AdapterError, match="联锁"):
            adapter.submit(_coat("CMD-I", program="COAT-180"))
        program.device.set_fault("busy")
        _until("PLC 退出远程模式、联锁解除", lambda: _readiness(adapter) == (False, False))
        with pytest.raises(AdapterError, match="未就绪"):
            adapter.submit(_coat("CMD-R", program="COAT-180"))
        program.device.set_fault("none")
        _until("PLC 回到远程模式", lambda: _readiness(adapter) == (True, False))
        assert not program.device.executions, "没有一条被拒的指令让 PLC 动作"

        # PLC 自己不认这个程序号：作业报故障，带故障代码对应的说明
        adapter.submit(_coat("CMD-OLD", program="COAT-OLD"))
        failed = _wait(adapter, "CMD-OLD")
        assert failed.state == "failed" and "程序号不存在" in failed.error

        adapter.submit(_coat("CMD-A", program="COAT-180"))
        with pytest.raises(AdapterError, match="DeviceBusy"):
            adapter.submit(_coat("CMD-B", program="COAT-180"))


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_hold_resume_abort_and_failure_codes(protocol):
    with plc_sim(protocol, task_seconds=30) as (program, _, port):
        adapter = _adapter(protocol, port)
        adapter.submit(_coat("CMD-H"))
        _until("PLC 开始运行", lambda: program.memory["State"] == 1)  # 还没运行时给的保持信号 PLC 不理
        assert adapter.hold(_coat("CMD-HOLD", "hold", target="CMD-H")).state == "done"
        _until("PLC 进入保持", lambda: program.memory["State"] == 2)
        resumed = adapter.submit(_coat("CMD-RES", "resume"))
        assert resumed.command_id == "CMD-RES"
        _until("PLC 恢复运行", lambda: program.memory["State"] == 1)
        assert adapter.abort(_coat("CMD-ABORT", "abort", target="CMD-H")).state == "done"
        aborted = adapter.query("CMD-H")
        assert aborted.state == "failed" and "终止" in aborted.error

    with plc_sim(protocol, task_seconds=0.3) as (program, _, port):
        adapter = _adapter(protocol, port)
        program.device.set_fault("fail")
        adapter.submit(_coat("CMD-F"))
        failed = _wait(adapter, "CMD-F")
        assert failed.state == "failed" and "过程报警" in failed.error


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_plc_that_refuses_the_start_is_a_clear_failure(protocol):
    """PLC 不接这次启动（报 start_refused 里登记的故障码、停在空闲）：设备明确没动，判失败，不等启动超时转人工。

    这台不映射就绪信号（`ready`）：模拟 PLC 进入 busy 后，下一轮扫描会把远程模式写回协议层，映射了的话下发前的就绪检查
    会先拦下来——拦不拦得住看扫描赶没赶在读之前，测的就不是「PLC 自己拒绝启动」这条路了，用例也会时过时不过。
    """
    with plc_sim(protocol, task_seconds=0.3) as (program, _, port):
        adapter = _adapter(protocol, port, ready={})
        program.device.set_fault("busy")
        assert adapter.submit(_coat("CMD-R")).state == "accepted", "启动沿写下去就是交接，拒不拒要看 PLC 的反应"
        refused = _wait(adapter, "CMD-R", seconds=4)
        assert refused.state == "failed" and "拒绝启动" in refused.error and "91" in refused.error, refused
        assert sum(program.device.executions.values()) == 0
        # 故障点上还留着 91：PLC 接了下一次启动就进入运行，不会被旧代码误判成拒绝
        program.device.set_fault("none")
        adapter.submit(_coat("CMD-OK"))
        assert _wait(adapter, "CMD-OK").state == "done"


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_stalled_plc_heartbeat_reads_as_lost(protocol):
    from ilcs_host.plugins.base import AdapterUnreachable

    with plc_sim(protocol) as (_, runner, port):
        adapter = _adapter(protocol, port)
        adapter.healthcheck()
        runner.closed.set()  # PLC 程序停了：变量还能读，心跳不再变
        _until("PLC 心跳不再变", lambda: _heartbeat_stopped(adapter))
        watcher = _adapter(protocol, port, heartbeat={"point": "heartbeat", "stale_sec": 0.3})
        watcher.healthcheck()
        time.sleep(0.4)
        with pytest.raises(AdapterUnreachable, match="心跳"):
            watcher.healthcheck()


def test_modbus_map_refuses_read_only_points():
    from ilcs_host.plugins.base import AdapterError

    with plc_sim("modbus") as (_, _, port):
        adapter = _adapter("modbus", port)
        with pytest.raises(AdapterError, match="被设备拒绝"):
            adapter.write_point("state", 3)
        with pytest.raises(AdapterError, match="只读"):
            adapter.write_point("remote", True)


def test_opcua_map_can_start_by_method_call():
    with plc_sim("opcua", task_seconds=0.3) as (program, _, port):
        prefix = "nsu=urn:ilcs:sim:plc;s=Coater"
        adapter = _adapter("opcua", port)
        spec = adapter.config["capabilities"]["cap.coat"]
        spec["start"] = {"method": {"object": prefix, "method": f"{prefix}.StartJob", "args": ["{recipe}"]}}
        spec["defaults"] = {"recipe": 12}
        adapter.submit(_coat("CMD-METHOD"))
        assert _wait(adapter, "CMD-METHOD").state == "done"
        assert program.memory["RecipeNo"] == 12
