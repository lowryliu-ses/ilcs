"""模拟接口：假冷水机（`chillers.py`，Huber / Julabo / LAUDA 三家的真协议）、假搅拌板（`namur_server.py`），
以及把它们装成一个模拟工位的工具。

`--simulate` 和测试都用它：在 127.0.0.1 上起一台假冷水机、每个位置一块假板（随机端口），真实接口（driver/）照常连它们——
驱动代码一行不改，测试走的就是真实的命令与应答。故障注入（联锁、忙、回执丢失、做到最后报故障、一直不结束、提交慢）
在设备层（`ilcs_gateway.FaultState`）；线断了用假设备的 `mute`，冷水机报警用 `set_alarm()`。
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from ilcs_gateway import FaultState

from driver.chillers import Chiller, make_chiller
from driver.config import BRANDS, Config
from driver.device import Station
from driver.link import Link
from driver.namur import Stirrer

from .chillers import FAKES, Bath, FakeChiller, LineServer
from .namur_server import FakePlate, NamurServer

# 模型参数的缺省（配置里的 simulation 段可以改）：室温、浴温时间常数、模型时间放快几倍、制冷能力下限
SIMULATION = {"ambient_c": 22.0, "tau_s": 10.0, "time_scale": 1.0, "floor_c": -40.0}


def default_config(kind: str = "huber", device_id: str = "SIM-CHILL-01", positions: int = 4) -> dict[str, Any]:
    """--simulate 没给 --config 时用的配置：一台冷水机 + 4 个搅拌位置，自动挑空闲位置。"""
    vendor = BRANDS[kind]["vendor"]
    return {
        "device_id": device_id, "model": f"{vendor} 冷水机（模拟）", "vendor": f"{vendor}（模拟）",
        "chiller": {"kind": kind, "min_c": -20, "max_c": 25, "tolerance_c": 0.5, "settle_sec": 5,
                    "reach_timeout_sec": 600, "after": "keep", "standby_c": 20},
        "capabilities": {"thermostat": "cap.thermostat", "stir": "cap.ely.stir"},
        "stirrers": {str(index): {"name": f"模拟板 {index}", "max_rpm": 1500} for index in range(1, positions + 1)},
        "auto_position": True,
        "programs": {"CHILL": {"name": "控温（到温保持）", "action": "thermostat"},
                     "STIR-CHILL": {"name": "制冷搅拌", "action": "stir"},
                     "STIR-COLD": {"name": "冷藏搅拌", "action": "stir"}},
        "default_program": "CHILL",
        "poll_sec": 1,
        "simulation": dict(SIMULATION),
    }


class Simulation:
    """一台假冷水机 + 一组假板。`station()` 每次都新建客户端连同一组假设备（测试里模拟网关重启）。"""

    def __init__(self, config: Config, bath: Bath, chiller: FakeChiller, chiller_server: LineServer,
                 plate_servers: dict[str, NamurServer]):
        self.config = config
        self.bath = bath
        self.chiller = chiller
        self.chiller_server = chiller_server
        self.plate_servers = plate_servers

    @property
    def plates(self) -> dict[str, FakePlate]:
        return {key: server.plate for key, server in self.plate_servers.items()}

    def chiller_client(self) -> Chiller:
        spec = self.config.chiller
        return make_chiller(spec.kind, Link(spec.link), uppercase=spec.uppercase)

    def clients(self) -> dict[str, Stirrer]:
        return {key: Stirrer(Link(position.link)) for key, position in self.config.positions.items()}

    def station(self, *, state_dir: str | Path | None = None, faults: FaultState | None = None) -> Station:
        return Station(self.config, self.chiller_client(), self.clients(), state_dir=state_dir,
                       faults=faults or FaultState())

    def stop(self) -> None:
        self.chiller_server.stop()
        for server in self.plate_servers.values():
            server.stop()


def simulated_station(config: str | Path | dict[str, Any] | None = None, *, kind: str | None = None,
                      state_dir: str | Path | None = None, link_timeout: float = 1.0,
                      **model: float) -> tuple[Station, Simulation]:
    """按配置（文件、dict，或缺省配置）起一台假冷水机、每个位置一块假板，返回 (工位, 模拟设备)。
    模拟设备在退出前要 stop()。`kind` 换厂家；`model` 改模型参数（ambient_c、tau_s、time_scale、floor_c）。

    配置里冷水机与各位置的 link 不用写（写了也换成假设备的地址，串口参数、行尾仍按厂家缺省）。"""
    if config is None:
        data = default_config(kind or "huber")
    elif isinstance(config, dict):
        data = copy.deepcopy(config)
    else:
        data = json.loads(Path(config).read_text(encoding="utf-8"))
    chiller = data.setdefault("chiller", {})
    if kind and kind != chiller.get("kind"):
        # 换厂家：厂家、型号跟着换，免得模拟的 Julabo 自报是 Huber 的型号
        chiller["kind"], data["vendor"] = kind, BRANDS[kind]["vendor"]
        data.pop("model", None)
    options = {**SIMULATION, **(data.get("simulation") or {}), **model}
    bath = Bath(ambient_c=options["ambient_c"], tau_s=options["tau_s"], time_scale=options["time_scale"],
                floor_c=options["floor_c"])
    fake = FAKES[str(chiller.get("kind") or "huber")](bath, min_setpoint=-40, max_setpoint=100)
    chiller_server = LineServer(lambda line: fake.handle(line))
    chiller["link"] = {"kind": "tcp", "host": "127.0.0.1", "port": chiller_server.port, "gap_sec": 0,
                       "timeout_sec": link_timeout}
    plate_servers: dict[str, NamurServer] = {}
    for key, item in (data.get("stirrers") or {}).items():
        server = NamurServer(FakePlate("RCT digital"))
        plate_servers[str(key)] = server
        if isinstance(item, dict):
            item["link"] = {"kind": "tcp", "host": "127.0.0.1", "port": server.port, "gap_sec": 0,
                            "timeout_sec": link_timeout}
    try:
        parsed = Config.parse(data)
    except ValueError:
        chiller_server.stop()
        for server in plate_servers.values():
            server.stop()
        raise
    simulation = Simulation(parsed, bath, fake, chiller_server, plate_servers)
    return simulation.station(state_dir=state_dir), simulation
