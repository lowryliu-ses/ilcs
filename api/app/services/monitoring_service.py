"""设备与执行器监控：失联、心跳超时、联锁、校准临期，以及执行器自身存活。

这些异常以前只会让执行门关闭或开跑检查失败，没有人会收到报警——看的人得先想到去看。
这里把它们变成按条件去重的报警：条件持续期间只报一次，条件消除后自动复位条件
（确认与关闭仍由人做）。
"""
from __future__ import annotations

import os
import socket
from datetime import timedelta

from sqlalchemy.orm import Session

from ..core.clock import now
from ..core.config import settings
from ..core.context import system_context
from ..domain.resources import Window, calibration_blockers, governing_calibration
from ..models import Adapter, Asset, ExecutorHeartbeat, Station
from .alarm_service import AlarmService

EXECUTOR_ID = "executor"


class DeviceMonitor:
    def __init__(self, db: Session):
        self.db = db

    # ---------- 工位条件 ----------

    def station_conditions(self, adapter: Adapter) -> dict[str, tuple[bool, int, str]]:
        """条件名 → (是否成立, 严重度, 描述)。停用的适配器不判失联与心跳，联锁照判。"""
        age = (now() - adapter.last_heartbeat).total_seconds() if adapter.last_heartbeat else None
        stale = age is None or age > settings.heartbeat_stale_sec
        return {
            "interlock": (bool(adapter.site_interlock), 1, "公共保护联锁触发"),
            "disconnected": (bool(adapter.enabled) and not adapter.connected, 2, "设备适配器失联"),
            "heartbeat_stale": (
                bool(adapter.enabled) and bool(adapter.connected) and stale, 2,
                f"设备心跳超时（{(age or 0) / 60:.0f} min 未上报）",
            ),
        }

    def evaluate_station(self, station_id: str) -> tuple[int, int]:
        adapter = self.db.get(Adapter, station_id)
        station = self.db.get(Station, station_id)
        if adapter is None or station is None or station.retired or not station.org_id:
            return 0, 0
        alarms = AlarmService(self.db, system_context(station.org_id, "设备监控"))
        raised = cleared = 0
        for name, (active, severity, message) in self.station_conditions(adapter).items():
            key = f"station:{station_id}:{name}"
            if active:
                before = alarms.alarms.open_by_condition(key)
                alarms.raise_alarm(
                    severity=severity, source_type="station", source_id=station_id,
                    message=f"{station_id} {message}",
                    response="到现场确认设备状态与网络；条件消除后系统自动复位，确认与关闭由人处理。",
                    owner="设备负责人", origin="system", condition_key=key,
                )
                raised += before is None
            elif alarms.resolve_condition(key, f"{station_id} {message}已消除"):
                cleared += 1
        return raised, cleared

    def stations(self) -> dict:
        raised = cleared = 0
        for adapter in self.db.query(Adapter).all():
            r, c = self.evaluate_station(adapter.station_id)
            raised += r
            cleared += c
        return {"raised": raised, "cleared": cleared}

    # ---------- 校准临期 ----------

    def calibrations(self) -> dict:
        raised = cleared = 0
        moment = now()
        window = Window(moment, moment + timedelta(minutes=1))
        from .asset_service import AssetService

        for asset in self.db.query(Asset).filter(Asset.state != "retired").all():
            if not asset.org_id or not asset.calibration_applicable:
                continue
            ctx = system_context(asset.org_id, "校准监控")
            spec = AssetService(self.db, ctx).spec_for(asset)
            alarms = AlarmService(self.db, ctx)
            invalid_key = f"asset:{asset.id}:calibration_invalid"
            due_key = f"asset:{asset.id}:calibration_due"
            blockers = [
                reason for reason in calibration_blockers(spec, "", window)
                if "维护状态" not in reason
            ]
            governing = governing_calibration(spec, "", moment)
            due_soon = (
                not blockers and governing is not None and governing.expires_at is not None
                and governing.expires_at - moment <= timedelta(days=settings.calibration_warn_days)
            )
            if blockers:
                before = alarms.alarms.open_by_condition(invalid_key)
                alarms.raise_alarm(
                    severity=2, source_type="asset", source_id=asset.id, message=blockers[0],
                    response="登记有效校准并附证书；在此之前该资产上的设备步骤无法开跑。",
                    owner="设备负责人", origin="system", condition_key=invalid_key,
                )
                raised += before is None
            elif alarms.resolve_condition(invalid_key, f"{asset.asset_no} 已有有效校准"):
                cleared += 1
            if due_soon:
                before = alarms.alarms.open_by_condition(due_key)
                alarms.raise_alarm(
                    severity=3, source_type="asset", source_id=asset.id,
                    message=(
                        f"{asset.asset_no} {asset.name} 的校准将于 "
                        f"{governing.expires_at.isoformat(timespec='minutes')} 到期"
                    ),
                    response="安排复校；到期后该资产上的设备步骤无法开跑。",
                    owner="设备负责人", origin="system", condition_key=due_key,
                )
                raised += before is None
            elif alarms.resolve_condition(due_key, f"{asset.asset_no} 校准不再临期"):
                cleared += 1
        return {"raised": raised, "cleared": cleared}


class ExecutorLiveness:
    """执行器的存活记录。执行器停了，界面上仍能下发，却没有人投递、没有人推进。"""

    def __init__(self, db: Session):
        self.db = db

    def beat(self) -> None:
        row = self.db.get(ExecutorHeartbeat, EXECUTOR_ID)
        moment = now()
        pid = os.getpid()
        host = socket.gethostname()
        if row is None:
            self.db.add(ExecutorHeartbeat(
                id=EXECUTOR_ID, host=host, pid=pid, started_at=moment, last_seen=moment,
            ))
        else:
            if row.pid != pid or row.host != host:
                row.started_at = moment
            row.host, row.pid, row.last_seen = host, pid, moment

    def record_cycle(self, *, cycle_ms: int, busy: list[str], stuck: list[str], workers: int) -> None:
        row = self.db.get(ExecutorHeartbeat, EXECUTOR_ID)
        if row is None:
            return
        row.detail = {
            "cycle_ms": cycle_ms, "busy_stations": busy, "stuck_stations": stuck, "workers": workers,
            "mode": "concurrent",
        }

    def reasons(self) -> list[str]:
        if settings.executor_stale_sec <= 0:
            return []
        row = self.db.get(ExecutorHeartbeat, EXECUTOR_ID)
        if row is None:
            return ["执行器从未上报存活：指令不会被投递，等待节点不会被唤醒"]
        age = (now() - row.last_seen).total_seconds()
        if age > settings.executor_stale_sec:
            return [f"执行器 {age:.0f} s 未上报存活（{row.host}）：指令不会被投递"]
        return []

    def status(self) -> dict:
        row = self.db.get(ExecutorHeartbeat, EXECUTOR_ID)
        if row is None:
            return {"alive": False, "last_seen": None, "host": "", "pid": 0}
        return {
            "alive": not self.reasons(),
            "last_seen": row.last_seen.isoformat(timespec="seconds"),
            "started_at": row.started_at.isoformat(timespec="seconds"),
            "host": row.host,
            "pid": row.pid,
            "detail": row.detail or {},
        }
