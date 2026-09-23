"""能力匹配。工位未定义的参数视为不能承接，这是配方校验与排程的唯一判据。"""
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class StationSpec:
    id: str
    status: str = "idle"
    clean: bool = True
    cal_due: str = ""
    positions: int = 1
    limits: dict[str, dict[str, list[float]]] = field(default_factory=dict)
    retired: bool = False
    # 一台资产可映射多个工位；容量约束按资产算，不按工位 ID 算
    asset_id: str = ""

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
    reasons = []
    for name, value in step_params(step).items():
        window = implemented.get(name)
        if not window:
            reasons.append(f"{station.id} 未定义参数 {name}")
        elif value < window[0] or value > window[1]:
            reasons.append(f"{station.id} {name}={value} 超出 [{window[0]}, {window[1]}]")
    return reasons
