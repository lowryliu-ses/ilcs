"""测试用：起假仪表（TCP 口 + 统一控制口），按 profile 的映射配置 + 指向假仪表的连接参数建驱动宿主的 `line_command` 插件。

- 插件直接测（`record` / `adapter`）：驱动宿主里的插件、作业台账原样代码，不打桩；
- 接入验收（`acceptance`）走现场那条路：假仪表挂到本进程起的驱动宿主上（设备文件 = profile 的映射 + 连接参数），
  ILCS 用 sila2_v1 接它，跑 ILCS 的接入验收清单；故障项目经假仪表的统一控制口注入。
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
from pathlib import Path
import socket
from types import SimpleNamespace
from typing import Any

MODULE = Path(__file__).resolve().parents[1]
PROFILES = {
    "keithley-2450": "profile-keithley-2450.json",
    "keithley-2400": "profile-keithley-2400.json",
    "hioki-bt3562": "profile-hioki-bt3562.json",
}
CAPABILITY = "cap.cell_check"
HOST_TOKEN = "m" * 40


def load_profile(name: str) -> dict:
    return json.loads((MODULE / PROFILES[name]).read_text(encoding="utf-8"))


def transport_for(name: str, port: int) -> dict:
    """2450 与 Hioki 走 TCP；2400 是串口仪表，模拟时按串口服务器的 TCP 原始模式（socket://）连，和现场经串口服务器一样走串口通道。"""
    if name == "keithley-2400":
        return {"kind": "serial", "port": f"socket://127.0.0.1:{port}"}
    return {"kind": "tcp", "host": "127.0.0.1", "port": port}


@dataclass
class Bench:
    name: str
    meter: Any
    server: Any
    control_url: str = ""


@contextmanager
def bench(name: str, *, control: bool = True, **options: Any):
    """起一台假仪表（和它的统一控制口），用完关掉。`options` 给仪表模型：ocv_V、ir_mohm、serial、language、model……"""
    from simulators.common.control import ControlServer
    from simulator.server import MeterControl, MeterServer, build_meter

    meter = build_meter(name, **options)
    server = MeterServer(meter).start()
    panel = ControlServer(MeterControl(meter, server), "127.0.0.1", 0).start() if control else None
    try:
        yield Bench(name, meter, server, f"http://127.0.0.1:{panel.port}" if panel else "")
    finally:
        if panel is not None:
            panel.stop()
        server.stop()


def device_config(rig: Bench, **config_changes: Any) -> dict:
    """驱动宿主设备文件里的 config：profile 的映射 + 指向假仪表的连接参数（现场照 profile 的 connection 填）。"""
    profile = load_profile(rig.name)
    return {**profile["config"], "transport": transport_for(rig.name, rig.server.port), **config_changes}


def record(rig: Bench, station_id: str = "ST-CELL-SIM", **config_changes: Any):
    """插件的设备登记（驱动宿主按设备文件建的那一份），支持标志照 profile。"""
    profile = load_profile(rig.name)
    return SimpleNamespace(
        station_id=station_id, config=device_config(rig, **config_changes), credential_ref="", protocol=profile["protocol"],
        version=profile["version"], note="", **{f"supports_{key}": bool(value) for key, value in profile["supports"].items()},
    )


def adapter(rec):
    from ilcs_host.plugins.line_command import LineCommandAdapter

    return LineCommandAdapter(rec)


def request(command_id: str, *, capability: str = CAPABILITY, params: dict | None = None, type_: str = "dispatch",
            target: str = "", batch_id: str = "B-CELL", step_id: str = "cell-check"):
    from ilcs_host.plugins.base import CommandRequest

    return CommandRequest(
        command_id=command_id, station_id="ST-CELL-SIM", capability=capability, params=params or {}, type=type_,
        batch_id=batch_id, step_index=0, step_id=step_id, target_command_id=target,
    )


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@contextmanager
def through_host(rig: Bench, root: Path):
    """假仪表挂到本进程起的驱动宿主上（一台设备一个 SiLA 服务），返回 ILCS 那边 sila2_v1 的设备登记。"""
    from app.adapters.acceptance import AcceptanceRecord
    from ilcs_host.plugins import PLUGINS
    from ilcs_host.server import prepare, start, stop
    from ilcs_host.site import load_site

    profile = load_profile(rig.name)
    site = root / "site"
    (site / "devices").mkdir(parents=True, exist_ok=True)
    (site / "tokens.txt").write_text(HOST_TOKEN + "\n", encoding="utf-8")
    (site / "host.json").write_text(json.dumps({
        "environment": "development", "address": "127.0.0.1", "allowed_hosts": "127.0.0.1,localhost",
        "state_dir": "state", "tokens_file": "tokens.txt",
    }), encoding="utf-8")
    port = _free_port()
    (site / "devices" / "METER.json").write_text(json.dumps({
        "plugin": profile["plugin"], "port": port, "simulator": True, "supports": profile["supports"],
        "config": device_config(rig),
    }, ensure_ascii=False), encoding="utf-8")
    token = root / "secrets" / "host.token"
    token.write_text(HOST_TOKEN, encoding="utf-8")
    loaded = load_site(site, set(PLUGINS))
    servers = start(loaded, prepare(loaded))
    try:
        config = {"host": "127.0.0.1", "port": port, "insecure": True, "request_timeout_sec": 5, "connect_timeout_sec": 2}
        if rig.control_url:
            config["simulator_control"] = {"url": rig.control_url}
        yield AcceptanceRecord(
            station_id="ST-CELL-SIM", kind="real", driver="sila2_v1", protocol="SiLA 2（驱动宿主）",
            version=profile["version"], config=config, credential_ref=f"file://{token}",
            **{f"supports_{key}": bool(value) for key, value in profile["supports"].items()},
        )
    finally:
        stop(servers)


def contract(rec) -> dict:
    return {
        "protocol": rec.protocol, "version": rec.version, "supports_hold": rec.supports_hold,
        "supports_abort": rec.supports_abort, "supports_query": rec.supports_query, "supports_dedup": rec.supports_dedup,
    }


def acceptance(name: str, rec, *, physical: bool = True, faults: bool = True, timeout: float = 10.0):
    """经驱动宿主对假仪表跑 ILCS 的接入验收清单；指令照 profile 的验收缺省（能力、参数），故障项目走统一控制口。"""
    from app.adapters.acceptance import SimulatorControlInjector, run_acceptance
    from app.adapters.base import CommandRequest
    from app.adapters.drivers.sila2 import Sila2Adapter
    from app.adapters.registry import describe

    defaults = load_profile(name)["acceptance"]
    template = CommandRequest(
        command_id="", station_id=rec.station_id, capability=defaults["capability"], params=defaults["params"],
        type="dispatch", batch_id="ACCEPTANCE", step_index=0, step_id="acceptance",
    )
    injector = SimulatorControlInjector(rec.config["simulator_control"]) if faults else None
    return run_acceptance(
        rec, lambda: Sila2Adapter(rec), template, contract=contract(rec), describe=lambda instance: describe(instance, rec),
        physical=physical, injector=injector, poll_timeout=timeout, poll_interval=0.1,
    )
