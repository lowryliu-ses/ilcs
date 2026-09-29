"""设备接入验收清单：对进程内的外部模拟设备跑一遍。模拟设备走真实协议，驱动不打桩。"""
from contextlib import contextmanager
import importlib.util
from pathlib import Path

import pytest

from sim_harness import balance_config, balance_sim, gateway_config, gateway_sim, record, request

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture()
def credential_root(tmp_path, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "adapter_credential_root", str(tmp_path))
    return tmp_path


class DeviceInjector:
    """测试里直接操作模拟设备对象：故障模式与每个指令号的真实动作次数；失联由模拟器进程停听实现。"""

    def __init__(self, device, runner):
        self.device, self.runner = device, runner

    def set(self, mode: str, parameter: float = 0.0) -> None:
        if mode == "offline":
            self.runner.go_offline(parameter or 3)
        else:
            self.device.set_fault(mode, parameter)

    def executions(self, command_id: str) -> int:
        return self.device.executions.get(command_id, 0)


def _contract(rec) -> dict:
    return {
        "protocol": rec.protocol, "version": rec.version, "supports_hold": rec.supports_hold,
        "supports_abort": rec.supports_abort, "supports_query": rec.supports_query,
        "supports_dedup": rec.supports_dedup,
    }


def _run(rec, factory, template=None, **options):
    from app.adapters.acceptance import run_acceptance
    from app.adapters.registry import describe

    return run_acceptance(
        rec, factory, template or request(""), contract=_contract(rec),
        describe=lambda instance: describe(instance, rec), poll_timeout=10, poll_interval=0.1, **options,
    )


def test_gateway_simulator_passes_the_full_checklist(credential_root):
    from app.adapters.drivers.http_json import HttpJsonAdapter

    with gateway_sim(credential_root, task_seconds=1.0) as (device, runner, port):
        config, token = gateway_config(port, credential_root)
        rec = record("HTTPS JSON", config, token, driver="http_json_v1", kind="real")
        report = _run(rec, lambda: HttpJsonAdapter(rec), physical=True, injector=DeviceInjector(device, runner))
    states = {check.key: check.state for check in report.checks}
    assert report.ok, report.markdown()
    assert all(states[key] == "pass" for key in (
        "identity", "health", "query_unknown", "complete", "duplicate", "restart_query", "hold", "abort",
        "lost_receipt", "busy", "interlock", "offline",
    )), states
    duplicate = next(check for check in report.checks if check.key == "duplicate")
    assert "动作 1 次" in duplicate.detail
    assert report.identity["reported_model"] and "SIM-GW-T" not in report.config_digest
    text = report.markdown()
    assert "设备接入验收报告" in text and "| 回执丢失 | 通过 |" in text


def test_read_only_run_never_moves_the_device(credential_root):
    from app.adapters.drivers.http_json import HttpJsonAdapter

    with gateway_sim(credential_root) as (device, _, port):
        config, token = gateway_config(port, credential_root)
        rec = record("HTTPS JSON", config, token, driver="http_json_v1", kind="real")
        report = _run(rec, lambda: HttpJsonAdapter(rec))
        assert not device.executions, "只读验收不提交任何动作"
    states = {check.key: check.state for check in report.checks}
    assert states["identity"] == states["query_unknown"] == "pass"
    assert states["complete"] == states["lost_receipt"] == "skip"
    assert report.ok


def test_script_gateway_injector_uses_the_simulator_control_api(credential_root):
    spec = importlib.util.spec_from_file_location("device_acceptance", ROOT / "scripts" / "device-acceptance.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    with gateway_sim(credential_root) as (device, _, port):
        config, token = gateway_config(port, credential_root)
        rec = record("HTTPS JSON", config, token, driver="http_json_v1", kind="real")
        injector = module.GatewaySimulatorInjector(rec)
        injector.set("busy")
        assert device.fault == "busy"
        injector.set("none")
        assert injector.executions("never") == 0


def test_job_ledger_driver_without_device_counts(credential_root):
    """天平不认 ILCS 指令号：去重靠作业台账。读不到设备侧动作次数时「重复提交」只能判跳过，不硬判通过。"""
    from app.adapters.drivers.mt_sics import MtSicsAdapter

    with balance_sim() as (_, _, port):
        rec = record("MT-SICS", balance_config(port), driver="mt_sics_v1", kind="real", supports_hold=False,
                     supports_abort=False)
        weigh = request("", capability="cap.weigh", params={"mass": 0.0152})
        report = _run(rec, lambda: MtSicsAdapter(rec), weigh, physical=True)
    states = {check.key: check.state for check in report.checks}
    assert states["complete"] == "pass" and states["restart_query"] == "pass", report.markdown()
    assert states["duplicate"] == "skip" and states["hold"] == states["abort"] == "skip"


def test_script_runs_read_only_acceptance_against_a_registered_station(monkeypatch, capsys, reset_runtime):
    """命令行入口：从库里读工位与适配器登记，跑只读验收；演示库的工位是模拟适配器。"""
    import sys

    spec = importlib.util.spec_from_file_location("device_acceptance_cli", ROOT / "scripts" / "device-acceptance.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(sys, "argv", ["device-acceptance.py", "ST-05"])
    assert module.main() == 0
    output = capsys.readouterr().out
    assert "设备接入验收报告：ST-05" in output and "| 查询不存在的指令号 | 通过 |" in output

    monkeypatch.setattr(sys, "argv", ["device-acceptance.py", "ST-99"])
    with pytest.raises(SystemExit, match="不存在"):
        module.main()


def test_control_port_counts_motions_for_devices_without_command_ids(credential_root):
    """串口命令干燥箱不认 ILCS 指令号：统一控制口报设备的总动作次数，「重复提交只动作一次」照样能判。"""
    from app.adapters.acceptance import SimulatorControlInjector
    from app.adapters.drivers.line_command import LineCommandAdapter
    from sim_harness import control_port, line_config, line_sim

    with line_sim(task_seconds=0.3) as (_, runner, port), control_port(runner.control_target()) as control:
        rec = record("串口 / TCP 命令", line_config(port), driver="line_command_v1", kind="real")
        injector = SimulatorControlInjector({"url": f"http://127.0.0.1:{control}"})
        assert injector.executions("ACC-x") is None and injector.motions() == 0
        report = _run(rec, lambda: LineCommandAdapter(rec), physical=True, injector=injector)
    states = {check.key: check.state for check in report.checks}
    assert report.ok, report.markdown()
    duplicate = next(check for check in report.checks if check.key == "duplicate")
    assert duplicate.state == "pass" and "总动作次数" in duplicate.detail, duplicate
    assert all(states[key] == "pass" for key in ("lost_receipt", "busy", "interlock", "offline")), states
    assert report.simulator


def test_control_port_for_the_balance_counts_weighings(credential_root):
    from app.adapters.acceptance import SimulatorControlInjector
    from app.adapters.drivers.mt_sics import MtSicsAdapter
    from sim_harness import control_port

    with balance_sim() as (balance, runner, port), control_port(runner.control_target()) as control:
        rec = record("MT-SICS", balance_config(port), driver="mt_sics_v1", kind="real", supports_hold=False,
                     supports_abort=False)
        injector = SimulatorControlInjector({"url": f"http://127.0.0.1:{control}"})
        weigh = request("", capability="cap.weigh", params={"mass": 0.0152})
        report = _run(rec, lambda: MtSicsAdapter(rec), weigh, physical=True, injector=injector)
        assert balance.weighings >= 2
    states = {check.key: check.state for check in report.checks}
    duplicate = next(check for check in report.checks if check.key == "duplicate")
    assert duplicate.state == "pass" and "总动作次数" in duplicate.detail, report.markdown()
    assert states["busy"] == states["interlock"] == states["offline"] == "pass", report.markdown()


def test_control_port_requires_its_token(credential_root):
    from app.adapters.acceptance import SimulatorControlInjector
    from app.adapters.base import AdapterError
    from sim_harness import control_port, line_sim

    token = credential_root / "simctl.token"
    token.write_text("s3cret-token")
    with line_sim() as (device, runner, _), control_port(runner.control_target(), token="s3cret-token") as control:
        with pytest.raises(AdapterError, match="401"):
            SimulatorControlInjector({"url": f"http://127.0.0.1:{control}"}).set("busy")
        SimulatorControlInjector({"url": f"http://127.0.0.1:{control}", "token_ref": f"file://{token}"}).set("busy")
        assert device.fault == "busy"


def test_script_runs_without_the_database_from_an_adapter_file(tmp_path, credential_root, monkeypatch, capsys):
    """设备开发者在自己电脑上：不连 ILCS 库，给一份适配器登记 JSON 就能跑完整清单（含故障项目）。"""
    import json
    import sys

    from sim_harness import control_port, line_config, line_sim

    spec = importlib.util.spec_from_file_location("device_acceptance_standalone", ROOT / "scripts" / "device-acceptance.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    token = credential_root / "oven-control.token"
    token.write_text("oven-token")
    with line_sim(task_seconds=0.3) as (_, runner, port), control_port(runner.control_target(), token="oven-token") as control:
        registration = {
            "kind": "real", "driver": "line_command_v1", "protocol": "串口 / TCP 命令", "version": "vendor-1.2",
            "config": {**line_config(port),
                       "simulator_control": {"url": f"http://127.0.0.1:{control}", "token_ref": f"file://{token}"}},
        }
        path = tmp_path / "oven.json"
        path.write_text(json.dumps(registration, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setattr(sys, "argv", [
            "device-acceptance.py", "--adapter", str(path), "--params", '{"temp": 120, "vacuum": 1}',
            "--physical", "--faults", "--timeout", "10", "--json", str(tmp_path / "report.json"),
        ])
        assert module.main() == 0
    output = capsys.readouterr().out
    assert "设备接入验收报告：STANDALONE" in output and "| 同一指令号重复提交 | 通过 |" in output
    assert "| 回执丢失 | 通过 |" in output and "| 失联 | 通过 |" in output
    saved = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert saved["ok"] and saved["simulator"] and saved["driver"] == "line_command_v1"


class StubbornDevice:
    """内存里的假设备：作业提交后一直在跑；`stops` 决定终止能不能让它停下。"""

    def __init__(self, *, stops: bool):
        self.stops = stops
        self.jobs: dict[str, str] = {}
        self.aborts: dict[str, str] = {}

    def submit(self, command):
        from app.adapters.base import CommandResult

        self.jobs.setdefault(command.command_id, "running")
        return CommandResult(command.command_id, self.jobs[command.command_id])

    def query(self, command_id):
        from app.adapters.base import CommandResult

        state = self.jobs.get(command_id)
        return CommandResult(command_id, state) if state else None

    def abort(self, command):
        from app.adapters.base import AdapterError, CommandResult

        if command.command_id in self.aborts:  # 同一终止指令号：按重投回放，不去停别的目标
            return CommandResult(command.command_id, "done")
        self.aborts[command.command_id] = command.target_command_id
        if not self.stops:
            raise AdapterError("设备拒绝终止")
        self.jobs[command.target_command_id] = "failed"
        return CommandResult(command.command_id, "done")


def _cleanup(device, submitted, **contract):
    from dataclasses import replace

    from app.adapters.acceptance import Report, _clean_up

    report = Report(station_id="ST-SIM", driver="stub", protocol="stub", contract={}, identity={}, config_digest="",
                    physical=True, faults=False, started_at="")
    template = request("")
    _clean_up(report, device, submitted, {"supports_query": True, "supports_abort": True, **contract},
              lambda tag, **changes: replace(template, command_id=f"ACC-T-{tag}", **changes),
              pause=lambda _: None, settle=0.2)
    return report


def test_cleanup_stops_what_acceptance_left_running_or_reports_it():
    """验收结束时还在动的 ACC- 指令先逐条终止；停不下来的记成残留，报告不通过（调用方据此把工位改回欠动作级）。"""
    targets = ["ACC-T-hold-target", "ACC-T-abort-target"]
    device = StubbornDevice(stops=True)
    for command_id in targets:
        device.submit(request(command_id))
    report = _cleanup(device, targets)
    assert report.ok and report.leftovers == []
    assert sorted(device.aborts.values()) == sorted(targets), "每个目标各发一条终止：撞号会被设备当重投回放"

    stuck = StubbornDevice(stops=False)
    stuck.submit(request("ACC-T-run"))
    report = _cleanup(stuck, ["ACC-T-run"])
    assert report.leftovers == ["ACC-T-run"] and not report.ok
    assert [check.key for check in report.checks] == ["cleanup"]

    # 不支持状态查询：终止回执确认了就算停住（契约：终止确认 = 设备已在安全状态）
    blind = StubbornDevice(stops=True)
    blind.submit(request("ACC-T-run"))
    assert _cleanup(blind, ["ACC-T-run"], supports_query=False).leftovers == []
    # 连驱动实例都建不起来：发过的全算残留
    assert _cleanup(None, ["ACC-T-lost"]).leftovers == ["ACC-T-lost"]


PILOT_SIMULATORS = ("plc_opcua", "plc_modbus", "opcua_task", "fleet", "sql_table")


@contextmanager
def _pilot_simulator(kind: str, root: Path):
    """一种试点模拟设备 + 它的统一控制口：返回 (适配器登记, 驱动工厂, 验收指令模板, 注入器)。"""
    from app.adapters.acceptance import SimulatorControlInjector, default_template
    from sim_harness import (
        control_port, fleet_config, fleet_sim, opcua_config, opcua_sim, plc_config, plc_sim, transfer,
    )

    if kind.startswith("plc_"):
        from app.adapters.drivers.modbus_map import ModbusMapAdapter
        from app.adapters.drivers.opcua_map import OpcUaMapAdapter

        protocol = kind.split("_", 1)[1]
        with plc_sim(protocol, task_seconds=0.4) as (_, runner, port), control_port(runner.control_target()) as control:
            config, credential = plc_config(protocol, port)
            rec = record("PLC 点表", config, credential, driver=f"{protocol}_map_v1", kind="real")
            implementation = OpcUaMapAdapter if protocol == "opcua" else ModbusMapAdapter
            yield rec, (lambda: implementation(rec)), request("", params={"thickness": 180, "temp": 110},
                                                              capability="cap.coat"), \
                SimulatorControlInjector({"url": f"http://127.0.0.1:{control}"})
    elif kind == "opcua_task":
        from app.adapters.drivers.opcua import OpcUaAdapter

        with opcua_sim(task_seconds=0.4) as (_, runner, port), control_port(runner.control_target()) as control:
            config, credential = opcua_config(port)
            rec = record("OPC UA TaskExecution", config, credential, driver="opcua_v1", kind="real")
            yield rec, (lambda: OpcUaAdapter(rec)), request(""), \
                SimulatorControlInjector({"url": f"http://127.0.0.1:{control}"})
    elif kind == "fleet":
        from app.adapters.drivers.rest_map import RestMapAdapter

        with fleet_sim(root, task_seconds=0.4) as (_, runner, port), control_port(runner.control_target()) as control:
            config, credential = fleet_config(port, root)
            rec = record("REST 接口映射", config, credential, driver="rest_map_v1", kind="real")
            # 和执行器一样按缺省模板造验收指令：转运能力发 type = transfer，参数取验收缺省里的起止位置
            template = default_template("ST-SIM", {"cap.transfer": {}}, "cap.transfer", dict(transfer("").params))
            assert template.type == "transfer"
            yield rec, (lambda: RestMapAdapter(rec)), template, \
                SimulatorControlInjector({"url": f"http://127.0.0.1:{control}", "unit": "AGV-01"})
    else:
        from app.adapters.drivers.sql_table import SqlTableAdapter
        from simulators.sql_device.worker import SqlDeviceWorker
        from sim_harness import _device

        url = f"sqlite:///{root / 'exchange.db'}"
        worker = SqlDeviceWorker(url, _device("SIM-SQL-T", task_seconds=0.4), poll_seconds=0.05)
        worker.start()
        try:
            with control_port(worker.control_target()) as control:
                rec = record("数据库中间表", {"url": url, "device_id": "SIM-SQL-T", "request_timeout_sec": 3,
                                            "heartbeat_stale_sec": 1.5}, driver="sql_table_v1", kind="real")
                yield rec, (lambda: SqlTableAdapter(rec)), request(""), \
                    SimulatorControlInjector({"url": f"http://127.0.0.1:{control}"})
        finally:
            worker.stop()


@pytest.mark.parametrize("kind", PILOT_SIMULATORS)
def test_every_pilot_simulator_passes_the_full_checklist(kind, credential_root, tmp_path, monkeypatch):
    """试点的每一类模拟设备都跑完整清单（动作 + 故障）：没有误判的不通过，也不留下没结束的验收指令。

    模拟设备注入不了的故障（点表设备没有回执可丢、中间表插入作业行就是交接）由控制口说明，清单判跳过；
    中间表的拒绝是设备侧轮询到作业行后异步回写的，失联要等心跳超时才看得出来。
    """
    from app.core.config import settings

    monkeypatch.setattr(settings, "adapter_state_root", str(tmp_path / "adapter-state"))
    count = {"live": 0, "peak": 0}

    def counted(factory):
        """数同时开着的驱动实例：每个实例占中间库一条连接、OPC UA 一个会话，轮询时不关会把设备占满。"""
        def build():
            instance = factory()
            original = getattr(instance, "close", None)
            count["live"] += 1
            count["peak"] = max(count["peak"], count["live"])

            def close():
                if not getattr(instance, "_closed_once", False):
                    instance._closed_once = True
                    count["live"] -= 1
                if original is not None:
                    original()

            instance.close = close
            return instance

        return build

    with _pilot_simulator(kind, credential_root) as (rec, factory, template, injector):
        report = _run(rec, counted(factory), template, physical=True, injector=injector)
    states = {check.key: check.state for check in report.checks}
    assert report.ok and not report.leftovers, report.markdown()
    assert count["live"] == 0, "验收建的驱动实例结束时都要关掉"
    assert count["peak"] <= 12, f"同时开着 {count['peak']} 个驱动实例：轮询时用完就关"
    offline = next(check.detail for check in report.checks if check.key == "offline")
    assert "OperationalError" not in offline and "too many" not in offline, offline
    assert states["complete"] == "pass" and states["abort"] == "pass", states
    if kind in {"plc_opcua", "plc_modbus", "sql_table"}:
        assert states["lost_receipt"] == "skip", states
    else:
        assert states["lost_receipt"] == "pass", states
    assert states["busy"] == states["interlock"] == states["offline"] == "pass", report.markdown()
    for key in ("busy", "interlock"):
        detail = next(check.detail for check in report.checks if check.key == key)
        assert "ACC-" not in detail, f"{key} 要测到注入的故障，不能被前一条探针挡住：{detail}"


def test_polling_rides_out_a_brief_loss_of_contact():
    """查询一时连不上（共用接口的另一台设备在做失联项目）接着查，超时前恢复就照常出结论，不中断整次验收。"""
    from app.adapters.acceptance import _wait
    from app.adapters.base import AdapterUnreachable, CommandResult

    class Flaky:
        calls = 0

        def query(self, command_id):
            self.calls += 1
            if self.calls <= 2:
                raise AdapterUnreachable("设备接口不可达：URLError")
            return CommandResult(command_id, "done")

    result, seen = _wait(Flaky(), "ACC-T-run", 5, 0.01, lambda _: None)
    assert result is not None and result.state == "done" and seen == ["unreachable", "done"]

    class Gone:
        def query(self, command_id):
            raise AdapterUnreachable("设备接口不可达")

    result, seen = _wait(Gone(), "ACC-T-run", 0.1, 0.01, lambda _: None)
    assert result is None and seen == ["unreachable"], "一直连不上：超时后照实报，不编结论"
