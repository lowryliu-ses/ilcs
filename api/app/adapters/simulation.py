"""模拟适配器。

与真实适配器共用同一份契约：它产生的是真实的事件流程（命令 → 接受 → 回执 → 推进器
决定下一步），不跳过审核、库存或步骤状态。所有数据都带 simulation 来源标记。
"""
from __future__ import annotations

from ..core.clock import now
from ..domain import simulation as sim
from .base import AdapterContract, CommandRequest, CommandResult

ORIGIN = "simulation"


class SimulationAdapter:
    def __init__(self, station_id: str, protocol: str = "sim", capabilities: tuple[str, ...] = ()):
        self.station_id = station_id
        self.contract = AdapterContract(
            kind=ORIGIN,
            protocol=protocol or "sim",
            version="sim-1.0",
            capabilities=capabilities,
            supports_hold=True,
            supports_abort=True,
            supports_query=True,
            supports_dedup=True,
            note="模拟适配器：事件流程与真实设备一致，数据带 simulation 标记",
        )
        self._ledger: dict[str, CommandResult] = {}

    def healthcheck(self) -> dict:
        return {
            "reachable": True,
            "driver": ORIGIN,
            "protocol": self.contract.protocol,
            "detail": "模拟适配器进程内健康检查通过；不代表真实协议或真实设备已连通",
        }

    def submit(self, request: CommandRequest) -> CommandResult:
        existing = self._ledger.get(request.command_id)
        if existing is not None:
            return existing  # 设备端去重：重复投递回放终态
        telemetry = tuple(
            (metric, float(setpoint), None)
            for metric, setpoint in (request.params or {}).items()
            if isinstance(setpoint, (int, float)) and not isinstance(setpoint, bool)
        )
        result = CommandResult(
            command_id=request.command_id,
            state="done",
            device_ts=now(),
            quality="good",
            delivered=dict(request.params or {}),
            telemetry=telemetry,
            origin=ORIGIN,
        )
        self._ledger[request.command_id] = result
        return result

    def query(self, command_id: str) -> CommandResult | None:
        return self._ledger.get(command_id)

    def hold(self, request: CommandRequest) -> CommandResult:
        return CommandResult(
            command_id=request.command_id, state="done", device_ts=now(), origin=ORIGIN,
            delivered={"held": True},
        )

    def abort(self, request: CommandRequest) -> CommandResult:
        return CommandResult(
            command_id=request.command_id, state="done", device_ts=now(), origin=ORIGIN,
            delivered={"aborted": True},
        )

    def telemetry_series(self, setpoint: float, seed: str, points: int) -> list[float]:
        return sim.telemetry_series(setpoint, seed, points)
