"""模拟适配器。

与真实适配器共用同一份契约：它产生的是真实的事件流程（命令 → 接受 → 回执 → 推进器
决定下一步），不跳过审核、库存或步骤状态。所有数据都带 simulation 来源标记。

回执在「原样回显下发参数」之外：
- 步骤声明了投料物料（`request.material`）时，按用量参数回报消耗（逐孔位时取各孔之和，单位是该参数登记的单位），
  和真实设备一样经消耗入账——模拟阶段也能看到预留、消耗与对账是否对得上。
- 工位配置 `simulate_outputs: true` 时，给方法输出规则里没回显的检测项生成确定性的示意值，
  否则每一步都是「缺必报项」。缺省关闭：不打开就和以前一样只回显参数。
"""
from __future__ import annotations

from ...core.clock import now
from ...core.rng import hash_str
from ...domain import simulation as sim
from ...domain.dosing import commanded_quantity
from ..base import AdapterContract, CommandRequest, CommandResult

ORIGIN = "simulation"


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# 旧名保留给已有调用方；「这一步下发了多少」的规则在领域层 dosing，消耗对账用的是同一个
delivered_quantity = commanded_quantity


def _within(value: float, lo, hi) -> float:
    """保留 4 位小数；舍入会越出 [lo, hi]（很窄、量级很小的区间）时保留全精度，示意值绝不落到规则外。"""
    rounded = round(float(value), 4)
    if (_number(lo) and rounded < lo) or (_number(hi) and rounded > hi):
        return float(value)
    return rounded


def sample_output(rule: dict, seed: str) -> float:
    """示意检测值：有上下限取区间内 35%–65% 处（按指令 + 检测项 + 孔位确定性散列）；只有一边就从边界往里
    挪 |界| × 5%（正的边界与乘 1.05 / 0.95 相同；负的边界那样乘会挪到界外）；下限正好是 0 给 1.0，上限正好
    是 0 就取 0（边界本身不算超限）；都没有给 1.0。
    只为让流程与数据链路跑通，不是物理模型，所以生成的值必须落在规则自己的范围内，否则每一步都被判超限。"""
    lo, hi = rule.get("lo"), rule.get("hi")
    if _number(lo) and _number(hi):
        ratio = 0.35 + 0.30 * (hash_str(seed) % 10000) / 9999
        value = lo + (hi - lo) * ratio
    elif _number(lo):
        value = lo + abs(lo) * 0.05 if lo else 1.0
    elif _number(hi):
        value = hi - abs(hi) * 0.05
    else:
        value = 1.0
    return _within(value, lo, hi)


class SimulationAdapter:
    def __init__(
        self, station_id: str, protocol: str = "sim", capabilities: tuple[str, ...] = (), config: dict | None = None,
    ):
        self.station_id = station_id
        self.config = dict(config or {})
        self.simulate_outputs = self.config.get("simulate_outputs") is True
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

    def identity(self) -> dict:
        # 内置模拟适配器接受任何设备端程序
        return {
            "vendor": "ILCS 内置模拟", "model": "", "firmware": "", "simulator": True,
            "methods": [{"program": "*", "name": "任意设备端程序（内置模拟）"}],
            "commands": ["dispatch", "resume", "retry", "hold", "abort", "query"],
        }

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
            delivered=self._delivered(request),
            telemetry=telemetry,
            origin=ORIGIN,
        )
        self._ledger[request.command_id] = result
        return result

    def _delivered(self, request: CommandRequest) -> dict:
        delivered = dict(request.params or {})
        if request.type == "transfer":
            return delivered
        material = request.material or {}
        if material.get("name") and material.get("param"):
            quantity = commanded_quantity(request.params or {}, material["param"])
            # 0 用量不回报：消耗入账会拒掉 0 并报警，而「这一步没投」本来就不是异常
            if quantity > 0:
                delivered["materials"] = [
                    {"material": material["name"], "unit": material.get("unit") or "", "quantity": float(quantity)}
                ]
        if self.simulate_outputs and request.outputs:
            self._fill_outputs(request, delivered)
        return delivered

    def _fill_outputs(self, request: CommandRequest, delivered: dict) -> None:
        """方法输出规则里没回显的检测项补示意值：有孔位时逐孔一份、顶层写均值。绝不覆盖回显的参数键。"""
        wells = delivered.get("wells") if isinstance(delivered.get("wells"), dict) else None
        if wells:
            wells = {well: dict(values or {}) for well, values in wells.items()}
        for rule in request.outputs:
            key = str((rule or {}).get("key") or "")
            if not key or key in delivered or key == "materials":
                continue
            if wells:
                values = []
                for well, row in wells.items():
                    if key not in row:
                        row[key] = sample_output(rule, f"{request.command_id}:{key}:{well}")
                    if _number(row[key]):
                        values.append(row[key])
                delivered[key] = (
                    _within(sum(values) / len(values), rule.get("lo"), rule.get("hi")) if values
                    else sample_output(rule, f"{request.command_id}:{key}")
                )
            else:
                delivered[key] = sample_output(rule, f"{request.command_id}:{key}")
        if wells:
            delivered["wells"] = wells

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
