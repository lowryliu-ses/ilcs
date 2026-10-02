"""模拟接口：本进程里起假天平（MT-SICS + Quantos，`sics_server.py`）和假注射泵（Cavro DT，`cavro_server.py`），
真实接口（driver/）照常经 TCP 连它们——模拟走的是和真机同一套协议代码。

`simulated_station()` 返回 (站, 模拟设备)。故障注入经网关的统一控制口（`FaultState`）；
加粉前按指令要的料把对应的加样头「装上」（代替配粉模组换头，真机上由搬运 / 人完成，网关只核对）。
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from ilcs_gateway import FaultState

from driver.cavro import CavroPump
from driver.config import Config
from driver.device import Station
from driver.sics import Balance, Quantos

from .cavro_server import FakePump, PumpServer
from .sics_server import FakeScale, ScaleServer
from .world import Head, Reservoir, World

SALTS = ["LiPF6", "LiFSI", "LiTFSI", "LiBF4", "LiDFP"]


def default_config() -> dict[str, Any]:
    """缺省的模拟站：一台带 Quantos 的天平 + 一台 12 口分配阀注射泵（4 种溶剂），三项能力都提供。"""
    return {
        "device_id": "SIM-BAL-DOSE-01", "model": "XPE206DRQ", "vendor": "Mettler Toledo（模拟）",
        "balance": {"kind": "tcp", "host": "127.0.0.1", "port": 1},
        "capabilities": {"weigh": "cap.weigh", "dose_solid": "cap.ely.dose_solid", "dose_liquid": "cap.ely.dose_liquid"},
        "solid": {"kind": "quantos", "tolerance_pct": 2, "substances": {name: name for name in SALTS}},
        "liquid": {
            "kind": "cavro", "link": {"kind": "tcp", "host": "127.0.0.1", "port": 1},
            "syringe_ul": 5000, "steps": 3000, "output_port": 12, "tolerance_g": 0.01,
            "materials": {"EMC": {"port": 1, "density_g_ml": 1.01}, "DEC": {"port": 2, "density_g_ml": 0.975},
                          "DMC": {"port": 3, "density_g_ml": 1.07}, "EP": {"port": 4, "density_g_ml": 0.88}},
        },
        "acceptance_material": {"dose_solid": "LiPF6", "dose_liquid": "DMC"},
        "stable_timeout_sec": 10,
    }


class Simulation:
    def __init__(self, world: World, scale: FakeScale, pump: FakePump | None, servers: list):
        self.world, self.scale, self.pump, self.servers = world, scale, pump, servers

    def stop(self) -> None:
        for server in self.servers:
            server.stop()


def simulated_station(config_file: str | None = None, *, state_dir: str | Path | None = None,
                      settle_sec: float = 0.2, dose_seconds: float = 0.5, step_seconds: float = 0.0002,
                      weigh_seconds: float = 0.3, config: dict[str, Any] | None = None) -> tuple[Station, Simulation]:
    data = copy.deepcopy(config) if config is not None else (
        json.loads(Path(config_file).read_text(encoding="utf-8")) if config_file else default_config())
    world = World(settle_sec=settle_sec)
    liquid = data.get("liquid") or {}
    for name, item in (liquid.get("materials") or {}).items():
        world.reservoirs[int(item["port"])] = Reservoir(name, float(item["density_g_ml"]))
    solid = data.get("solid") or {}
    substances = list((solid.get("substances") or {}).values()) or SALTS
    world.heads = [Head(substance) for substance in dict.fromkeys(substances)]
    world.mounted = 0 if world.heads else None
    scale = FakeScale(world, quantos=bool(solid), dose_seconds=dose_seconds, weigh_seconds=weigh_seconds,
                      model=str(data.get("model") or "XPE206DRQ"))
    servers: list = [ScaleServer(scale)]
    data["balance"] = {**data.get("balance", {}), "kind": "tcp", "host": "127.0.0.1", "port": servers[0].port}
    pump = None
    if liquid:
        pump = FakePump(world, syringe_ul=float(liquid["syringe_ul"]), steps=int(liquid["steps"]),
                        output_port=int(liquid["output_port"]), step_seconds=step_seconds)
        servers.append(PumpServer(pump))
        data["liquid"] = {**liquid, "link": {"kind": "tcp", "host": "127.0.0.1", "port": servers[-1].port}}
    parsed = Config.parse(data)
    balance = Balance(parsed.balance)
    station = Station(parsed, balance, Quantos(balance, dose_timeout_sec=60) if parsed.solid else None,
                      CavroPump(parsed.liquid) if parsed.liquid else None, state_dir=state_dir,
                      faults=FaultState(), head_loader=world.mount if parsed.solid else None)
    return station, Simulation(world, scale, pump, servers)
