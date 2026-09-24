"""能力匹配。工位未定义的参数视为不能承接，这是配方校验与排程的唯一判据。"""
from dataclasses import dataclass, field
from typing import Any

from .methods import station_allows


@dataclass(frozen=True)
class StationSpec:
    id: str
    status: str = "idle"
    clean: bool = True
    cal_due: str = ""
    positions: int = 1
    # 并行通道数：同一时刻能同时承接几个批次；样品位是单个批次的容量，二者不是一回事
    channels: int = 1
    limits: dict[str, dict[str, list[float]]] = field(default_factory=dict)
    retired: bool = False
    # 一台资产可映射多个工位；容量约束按资产算，不按工位 ID 算
    asset_id: str = ""
    # 型号与驱动自报的设备端程序目录：步骤引用设备方法时据此筛工位（空目录不筛）
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
    return reasons
