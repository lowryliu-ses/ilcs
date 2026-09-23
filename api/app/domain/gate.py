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
        elif age > degraded_sec:
            degraded.append(f"{adapter.station_id} 心跳间隔 {age:.0f} s 超过 {degraded_sec} s 阈值")
    return {
        "open": not reasons,
        "reasons": reasons,
        "blocked_stations": blocked,
        "degraded": degraded,
        "checked_at": now.isoformat(timespec="seconds"),
    }


def station_reasons(state: dict, station_ids) -> list[str]:
    """这些工位上挡住动作的原因：全站原因加上各工位自己的原因。"""
    blocked = state.get("blocked_stations") or {}
    return [*state["reasons"], *(blocked[s] for s in sorted(set(station_ids)) if s in blocked)]
