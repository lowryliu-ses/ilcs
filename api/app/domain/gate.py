"""全局执行门：gate = stale OR site_interlock。全站只读订阅，不由用户切换。"""
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class AdapterHealth:
    station_id: str
    connected: bool
    site_interlock: bool
    last_heartbeat: datetime
    enabled: bool = True
    # 执行器按周期主动探测的设备，心跳天然隔一个探测周期才刷新一次：
    # 降级阈值取「全局阈值」与「探测周期 + 一个执行器轮询」中较大者，免得正常探测也报降级
    heartbeat_interval_sec: float = 0


def evaluate(adapters: list[AdapterHealth], now: datetime, stale_sec: int, degraded_sec: int) -> dict:
    """执行门分两层。

    - 全站（`reasons`）：公共保护联锁。它是现场公共安全事实，任何一台设备报联锁全站都停。
    - 按工位（`blocked_stations`）：失联、心跳超时。只挡用到这台设备的批次——设备一多，
      任何一台掉线都让全站停摆，等于把单台设备的故障放大成全站事故。
    """
    reasons: list[str] = []
    degraded: list[str] = []
    blocked: dict[str, str] = {}
    for adapter in adapters:
        # 公共保护联锁是现场事实，不因适配器停用或暂不接受指令而失效；
        # 停用只让它退出「失联 / 心跳超时」的判断。
        if adapter.site_interlock:
            reasons.append(f"{adapter.station_id} 公共保护联锁未解除")
        if not adapter.enabled:
            continue
        age = (now - adapter.last_heartbeat).total_seconds()
        if not adapter.connected:
            blocked[adapter.station_id] = f"{adapter.station_id} 适配器失联"
        elif age > stale_sec:
            blocked[adapter.station_id] = f"{adapter.station_id} 遥测数据超时 {age / 60:.0f} min"
        elif age > (threshold := max(degraded_sec, adapter.heartbeat_interval_sec)):
            degraded.append(f"{adapter.station_id} 心跳间隔 {age:.0f} s 超过 {threshold:.0f} s 阈值")
    return {
        "open": not reasons,
        "reasons": reasons,
        "blocked_stations": blocked,
        "degraded": degraded,
        "checked_at": now.isoformat(timespec="seconds"),
    }


def adapter_status(state: dict, station_id: str, enabled: bool, connected: bool) -> str:
    """适配器在界面上的状态，和执行门用同一份判断：停用 / 失联 / 心跳超时 / 降级 / 在线。

    「降级」按工位标识精确匹配：`degraded` 里每条原因以「<工位> 」开头，按子串找会让 ST-01 命中 ST-01-A。
    """
    if not enabled:
        return "disabled"
    if not connected:
        return "offline"
    if station_id in (state.get("blocked_stations") or {}):
        return "stale"
    if any(row.startswith(f"{station_id} ") for row in state.get("degraded") or []):
        return "degraded"
    return "online"


def station_reasons(state: dict, station_ids) -> list[str]:
    """这些工位上挡住动作的原因：全站原因加上各工位自己的原因。"""
    blocked = state.get("blocked_stations") or {}
    return [*state["reasons"], *(blocked[s] for s in sorted(set(station_ids)) if s in blocked)]
