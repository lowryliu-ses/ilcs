"""模拟接口：本进程里起一台假 MethodSCRIPT 仪器（`fake_methodscript.py`，TCP），真实接口（driver/palmsens.py）照常
经 TCP 连它——模拟走的是和真机同一套协议代码。故障注入在设备层（`ilcs_gateway.FaultState`，统一控制口用）。

`simulated_instrument()` 返回 (网关设备, 模拟)；`Simulation.fake` 是假仪器（测试里改电池参数、开故障开关）。
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from ilcs_gateway import FaultState

from driver.config import Config
from driver.device import Instrument
from driver.palmsens import MethodScript

from .cell_model import CellModel
from .fake_methodscript import FakeInstrument, InstrumentServer

HERE = Path(__file__).resolve().parent


def default_config() -> dict[str, Any]:
    """--simulate 没给 --config 时用的配置（与 simulator/potentiostat-sim.json 相同）。"""
    return json.loads((HERE / "potentiostat-sim.json").read_text(encoding="utf-8"))


class Simulation:
    def __init__(self, fake: FakeInstrument, server: InstrumentServer):
        self.fake, self.server = fake, server

    def stop(self) -> None:
        self.server.stop()


def simulated_instrument(config_file: str | Path | None = None, *, state_dir: str | Path | None = None,
                         time_scale: float = 1.0, cell: CellModel | None = None, device_type: str = "es4_hr",
                         config: dict[str, Any] | None = None) -> tuple[Instrument, Simulation]:
    """起一台假仪器（127.0.0.1 上随便一个端口），网关配置的链路改成连它。"""
    data = copy.deepcopy(config) if config is not None else (
        json.loads(Path(config_file).read_text(encoding="utf-8")) if config_file else default_config())
    fake = FakeInstrument(cell or CellModel(), device_type=device_type, time_scale=time_scale,
                          serial=f"ILCS-SIMULATOR-{device_type.upper().replace('_', '')}-01")
    server = InstrumentServer(fake)
    backend = dict(data.get("backend") or {})
    backend["link"] = {"kind": "tcp", "host": "127.0.0.1", "port": server.port}
    data["backend"] = backend
    try:
        parsed = Config.parse(data)
    except ValueError:
        server.stop()
        raise
    device = Instrument(MethodScript(parsed.link, timeout=parsed.timeout_sec, model=parsed.model), parsed,
                        state_dir=state_dir, faults=FaultState())
    return device, Simulation(fake, server)


__all__ = ["CellModel", "FakeInstrument", "InstrumentServer", "Simulation", "default_config", "simulated_instrument"]
