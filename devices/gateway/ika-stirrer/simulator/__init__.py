"""模拟接口：假加热板（`namur_server.py`，TCP 上说 NAMUR）与把它们装成一个模拟工位的工具。

`--simulate` 和测试都用它：每个位置在 127.0.0.1 上起一块假板（随机端口），真实接口（driver/namur.py +
driver/link.py）照常连它们——驱动代码一行不改，测试走的就是真实的命令与应答。故障注入（联锁、忙、回执丢失、
做到最后报故障、一直不结束、提交慢）在设备层（`ilcs_gateway.FaultState`）；线断了用假板的 `mute`。
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from ilcs_gateway import FaultState

from driver.config import Config
from driver.device import Station
from driver.link import Link
from driver.namur import Hotplate

from .namur_server import FakePlate, NamurServer


def default_config(device_id: str = "SIM-IKA-STIR", positions: int = 4) -> dict[str, Any]:
    """--simulate 没给 --config 时用的配置：4 个位置（像 EL-D-MIX 的 4 个通道），自动挑空闲位置。"""
    return {
        "device_id": device_id, "model": "RCT digital（模拟）", "vendor": "IKA（模拟）", "capability": "cap.ely.stir",
        "ambient_c": 25, "auto_position": True,
        "positions": {str(index): {"name": f"模拟板 {index}", "max_temp_c": 310, "max_rpm": 1500, "sensor": "external"}
                      for index in range(1, positions + 1)},
        "programs": {"STIR": {"name": "控温搅拌"}, "STIR-FINAL": {"name": "终混"}},
        "default_program": "STIR",
        "poll_sec": 1,
    }


class Simulation:
    """一组假板。`station()` 每次都新建客户端连同一组假板（测试里模拟网关重启）。"""

    def __init__(self, config: Config, servers: dict[str, NamurServer]):
        self.config = config
        self.servers = servers

    @property
    def plates(self) -> dict[str, FakePlate]:
        return {key: server.plate for key, server in self.servers.items()}

    def clients(self) -> dict[str, Hotplate]:
        return {key: Hotplate(Link(position.link)) for key, position in self.config.positions.items()}

    def station(self, *, state_dir: str | Path | None = None, faults: FaultState | None = None) -> Station:
        return Station(self.config, self.clients(), state_dir=state_dir, faults=faults or FaultState())

    def stop(self) -> None:
        for server in self.servers.values():
            server.stop()


def simulated_station(config: str | Path | dict[str, Any] | None = None, *, state_dir: str | Path | None = None,
                      tau_s: float = 2.0, ramp_rpm_s: float = 3000.0,
                      link_timeout: float = 1.0) -> tuple[Station, Simulation]:
    """按配置（文件、dict，或缺省的 4 个位置）每个位置起一块假板，返回 (工位, 模拟设备)。模拟设备在退出前要 stop()。

    配置里各位置的 link 不用写（写了也换成假板的地址）。"""
    if config is None:
        data = default_config()
    elif isinstance(config, dict):
        data = copy.deepcopy(config)
    else:
        data = json.loads(Path(config).read_text(encoding="utf-8"))
    ambient = float(data.get("ambient_c") or 25)
    servers: dict[str, NamurServer] = {}
    for key, item in (data.get("positions") or {}).items():
        server = NamurServer(FakePlate("RCT digital", ambient=ambient, tau_s=tau_s, ramp_rpm_s=ramp_rpm_s))
        servers[str(key)] = server
        if isinstance(item, dict):
            item["link"] = {"kind": "tcp", "host": "127.0.0.1", "port": server.port, "gap_sec": 0,
                            "timeout_sec": link_timeout}
    try:
        parsed = Config.parse(data)
    except ValueError:
        for server in servers.values():
            server.stop()
        raise
    simulation = Simulation(parsed, servers)
    return simulation.station(state_dir=state_dir), simulation
