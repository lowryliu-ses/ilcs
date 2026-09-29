"""能力匹配。工位未定义的参数视为不能承接，这是流程校验与排程的唯一判据。

取自上游结果的参数（前馈）在排程时还没有值：按流程声明的预期范围匹配，工位极限必须覆盖整个范围，
下发时再按实际值核一次。
"""
from dataclasses import dataclass, field
from typing import Any

from .bindings import bindings_of, expect_of, window_holds
from .methods import station_allows


@dataclass(frozen=True)
class StationSpec:
    id: str
    status: str = "idle"
    clean: bool = True
    # 并行通道数。按批计（缺省）：同一时刻能同时承接几个批次的设备步骤，一个批次的一个设备步骤占 1 个，
    # 与批次里有几个样本无关。按样本计（per_sample，一颗电芯占一个物理通道的充放电柜）：批次里每个样本
    # 各占 1 个，8 通道的柜子同时只能跑 8 颗——一批 8 颗，或一批 5 颗加一批 3 颗
    channels: int = 1
    per_sample: bool = False
    limits: dict[str, dict[str, list[float]]] = field(default_factory=dict)
    retired: bool = False
    # 一台资产可映射多个工位；容量约束按资产算，不按工位 ID 算
    asset_id: str = ""
    # 型号（关联了资产取资产登记的型号）与驱动自报的设备端程序目录：步骤引用设备方法时据此筛工位（空目录不筛）
    model: str = ""
    programs: tuple[str, ...] = ()

    @property
    def healthy(self) -> bool:
        # 离线工位与故障工位一样不能承接新排程：排进去只会到开跑检查才暴露
        return self.status not in {"fault", "offline"}


def step_params(step: dict[str, Any]) -> dict[str, float]:
    return step.get("params") or {}


def station_fits(station: StationSpec, step: dict[str, Any]) -> bool:
    if station.retired:
        return False  # 已停用的工位不再参与匹配，但历史分配仍指向它
    implemented = station.limits.get(step.get("cap", ""))
    if implemented is None:
        return False
    if station_allows(station.model, station.programs, step):
        return False
    for name, value in step_params(step).items():
        window = implemented.get(name)
        if not window or value < window[0] or value > window[1]:
            return False
    for name, binding in bindings_of(step).items():
        if not window_holds(implemented.get(name), expect_of(binding)):
            return False
    return True


def stations_for_step(stations: list[StationSpec], step: dict[str, Any]) -> list[StationSpec]:
    return [s for s in stations if station_fits(s, step)]


def out_of_range(station: StationSpec, step: dict[str, Any]) -> list[str]:
    """返回越界原因，用于界面解释为什么该工位不能承接。"""
    if station.retired:
        return [f"{station.id} 已停用"]
    implemented = station.limits.get(step.get("cap", ""))
    if implemented is None:
        return [f"{station.id} 未实现能力 {step.get('cap')}"]
    reasons = [f"{station.id} {reason}" for reason in station_allows(station.model, station.programs, step)]
    for name, value in step_params(step).items():
        window = implemented.get(name)
        if not window:
            reasons.append(f"{station.id} 未定义参数 {name}")
        elif value < window[0] or value > window[1]:
            reasons.append(f"{station.id} {name}={value} 超出 [{window[0]}, {window[1]}]")
    for name, binding in bindings_of(step).items():
        window, expect = implemented.get(name), expect_of(binding)
        if not window:
            reasons.append(f"{station.id} 未定义参数 {name}")
        elif expect is None:
            reasons.append(f"{station.id} {name} 取自上游结果但没有预期范围")
        elif not window_holds(window, expect):
            reasons.append(
                f"{station.id} {name} 预期范围 [{expect[0]:g}, {expect[1]:g}] 超出 [{window[0]}, {window[1]}]"
            )
    return reasons
