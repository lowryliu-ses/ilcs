"""测试用：起假仪表（TCP 口 + 统一控制口），按 profile 的映射配置 + 指向假仪表的连接参数建 ILCS 的 `line_command_v1` 驱动。

ILCS 侧走的就是现场那条路：接入模板的映射配置与工位填的连接参数合并（`catalog.merge_config`，套用模板时同一个函数），
驱动、作业台账、接入验收清单都是 api/ 里的原样代码，不打桩。
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

MODULE = Path(__file__).resolve().parents[1]
PROFILES = {
    "keithley-2450": "profile-keithley-2450.json",
    "keithley-2400": "profile-keithley-2400.json",
    "hioki-bt3562": "profile-hioki-bt3562.json",
}
CAPABILITY = "cap.cell_check"


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


def record(rig: Bench, station_id: str = "ST-CELL-SIM", **config_changes: Any):
    """套用模板：profile 的映射配置 + 指向假仪表的连接参数（有控制口时连同 simulator_control），支持标志照 profile。"""
    from app.adapters.acceptance import AcceptanceRecord
    from app.adapters.catalog import merge_config

    profile = load_profile(rig.name)
    connection: dict[str, Any] = {"transport": transport_for(rig.name, rig.server.port)}
    if rig.control_url:
        connection["simulator_control"] = {"url": rig.control_url}
    config = merge_config(merge_config(profile["config"], connection), config_changes)
    return AcceptanceRecord(
        station_id=station_id, kind="real", driver=profile["driver"], protocol=profile["protocol"],
        version=profile["version"], config=config,
        **{f"supports_{key}": bool(value) for key, value in profile["supports"].items()},
    )


def adapter(rec):
    from app.adapters.drivers.line_command import LineCommandAdapter

    return LineCommandAdapter(rec)


def request(command_id: str, *, capability: str = CAPABILITY, params: dict | None = None, type_: str = "dispatch",
            target: str = "", batch_id: str = "B-CELL", step_id: str = "cell-check"):
    from app.adapters.base import CommandRequest

    return CommandRequest(
        command_id=command_id, station_id="ST-CELL-SIM", capability=capability, params=params or {}, type=type_,
        batch_id=batch_id, step_index=0, step_id=step_id, target_command_id=target,
    )


def contract(rec) -> dict:
    return {
        "protocol": rec.protocol, "version": rec.version, "supports_hold": rec.supports_hold,
        "supports_abort": rec.supports_abort, "supports_query": rec.supports_query, "supports_dedup": rec.supports_dedup,
    }


def acceptance(name: str, rec, *, physical: bool = True, faults: bool = True, timeout: float = 10.0):
    """对假仪表跑 ILCS 的接入验收清单；指令照 profile 的验收缺省（能力、参数），故障项目走统一控制口。"""
    from app.adapters.acceptance import SimulatorControlInjector, run_acceptance
    from app.adapters.registry import describe

    defaults = load_profile(name)["acceptance"]
    template = request("", capability=defaults["capability"], params=defaults["params"], batch_id="ACCEPTANCE",
                       step_id="acceptance")
    injector = SimulatorControlInjector(rec.config["simulator_control"]) if faults else None
    return run_acceptance(
        rec, lambda: adapter(rec), template, contract=contract(rec), describe=lambda instance: describe(instance, rec),
        physical=physical, injector=injector, poll_timeout=timeout, poll_interval=0.1,
    )
