"""设备接入验收清单：对进程内的外部模拟设备跑一遍。模拟设备走真实协议，驱动不打桩。"""
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
    from app.adapters.http_json import HttpJsonAdapter

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
    from app.adapters.http_json import HttpJsonAdapter

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
    from app.adapters.mt_sics import MtSicsAdapter

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
