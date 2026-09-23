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
    reasons: list[str] = []
    degraded: list[str] = []
    for adapter in adapters:
        # 公共保护联锁是现场事实，不因适配器停用或暂不接受指令而失效；
        # 停用只让它退出「失联 / 心跳超时」的判断。
        if adapter.site_interlock:
            reasons.append(f"{adapter.station_id} 公共保护联锁未解除")
        if not adapter.enabled:
            continue
        age = (now - adapter.last_heartbeat).total_seconds()
        if not adapter.connected:
            reasons.append(f"{adapter.station_id} 适配器失联")
        elif age > stale_sec:
            reasons.append(f"{adapter.station_id} 遥测数据超时 {age / 60:.0f} min")
        elif age > degraded_sec:
            degraded.append(f"{adapter.station_id} 心跳间隔 {age:.0f} s 超过 {degraded_sec} s 阈值")
    return {
        "open": not reasons,
        "reasons": reasons,
        "degraded": degraded,
        "checked_at": now.isoformat(timespec="seconds"),
    }
