"""设备与执行器监控：失联、心跳超时、联锁、校准临期，以及执行器自身存活。

这些异常以前只会让执行门关闭或开跑检查失败，没有人会收到报警——看的人得先想到去看。
这里把它们变成按条件去重的报警：条件持续期间只报一次，条件消除后自动复位条件
（确认与关闭仍由人做）。
"""
from __future__ import annotations

import os
import socket
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from ..core.clock import now
from ..core.config import settings
from ..core.context import system_context
from ..domain.resources import Window, calibration_blockers, governing_calibration
from ..models import Adapter, Asset, ExecutorHeartbeat, Station
from .alarm_service import AlarmService

EXECUTOR_ID = "executor"


def handshake_grace(adapter: Adapter) -> float:
    """保存连接配置（或新登记）之后等第一次握手的宽限（秒）：执行器主动探测的设备给 3 个探测周期、至少 1 min；
    设备自己推心跳的（含内置模拟，要人点「重新连接」）按心跳超时。"""
    from ..adapters.registry import probe_interval

    interval = probe_interval(adapter)
    return max(60.0, 3 * interval) if interval else float(settings.heartbeat_stale_sec)


class DeviceMonitor:
    def __init__(self, db: Session):
        self.db = db

    # ---------- 工位条件 ----------

    @staticmethod
    def awaiting_handshake(adapter: Adapter, moment: datetime | None = None) -> bool:
        """新登记、或刚保存了改变连接的配置，还在等第一次握手、没过宽限：离线不是失联（执行门照样挡着下发）。"""
        since = adapter.awaiting_handshake_since
        if since is None or adapter.connected:
            return False
        return ((moment or now()) - since).total_seconds() < handshake_grace(adapter)

    def station_conditions(self, adapter: Adapter) -> dict[str, tuple[bool, int, str]]:
        """条件名 → (是否成立, 严重度, 描述)。停用的适配器不判失联与心跳，联锁照判。"""
        moment = now()
        age = (moment - adapter.last_heartbeat).total_seconds() if adapter.last_heartbeat else None
        stale = age is None or age > settings.heartbeat_stale_sec
        lost, details = "设备适配器失联", []
        if adapter.awaiting_handshake_since is not None and not adapter.connected:
            waited = (moment - adapter.awaiting_handshake_since).total_seconds()
            details.append(f"保存连接配置 {waited / 60:.0f} min 后还没握上手：核对地址、凭据与设备身份")
        if not adapter.connected and (adapter.note or "").startswith(("探测失败：", "探测拒绝：")):
            details.append(adapter.note[:200])  # 执行器探测没通过的原因（ExecutorLoop._probe_failed）
        if details:
            lost += f"（{'；'.join(details)}）"
        return {
            "interlock": (bool(adapter.site_interlock), 1, "公共保护联锁触发"),
            "disconnected": (bool(adapter.enabled) and not adapter.connected, 2, lost),
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
        from .exception_service import ExceptionService

        exceptions = ExceptionService(self.db, system_context(station.org_id, "异常引擎"))
        pending = self.awaiting_handshake(adapter)
        for name, (active, severity, message) in self.station_conditions(adapter).items():
            key = f"station:{station_id}:{name}"
            if name == "disconnected" and active and pending:
                # 刚保存配置、在等第一次握手：既不是失联，也不是恢复——报警状态不动（改配置之前就在失联的照样挂着）
                continue
            if active:
                before = alarms.alarms.open_by_condition(key)
                alarm = alarms.raise_alarm(
                    severity=severity, source_type="station", source_id=station_id,
                    message=f"{station_id} {message}",
                    response="到现场确认设备状态与网络；条件消除后系统自动复位，确认与关闭由人处理。",
                    owner="设备负责人", origin="system", condition_key=key,
                )
                if before is None:
                    # 新出现的条件：登记异常与影响面，策略要求时把这台工位上未开始的时间窗改派出去
                    exceptions.on_station_condition(station_id, name, f"{station_id} {message}", alarm.id)
                raised += before is None
            elif alarms.resolve_condition(key, f"{station_id} {message}已消除"):
                exceptions.settle_station(station_id, f"{station_id} {message}已消除")
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


class BatchLiveness:
    """运行中批次的活性看门狗。

    返工、驳回、人工判定之后流程节点没有重新开出，批次就会一直停在「运行中」：没有指令在动、
    没有步骤在等，界面上看不出异常。这里按 `domain.liveness` 判定，状态持续超过宽限期就报警；
    批次恢复推进或不再运行时自动复位条件（确认与关闭仍由人做）。
    """

    def __init__(self, db: Session):
        self.db = db

    def check(self) -> dict:
        from ..domain.liveness import stall_reason
        from ..models import Alarm, Batch, Command, StepRun, WorkflowEvent

        raised = cleared = 0
        moment = now()
        grace = timedelta(seconds=max(0, settings.stall_alarm_sec))
        watched = {
            row.source_id for row in self.db.query(Alarm).filter(
                Alarm.condition_key.like("batch:%:stalled"), Alarm.condition_active.is_(True),
            ).all()
        }
        running = self.db.query(Batch).filter(Batch.state == "running").all()
        batches = {batch.id: batch for batch in running}
        for batch_id in watched - set(batches):
            batch = self.db.get(Batch, batch_id)
            if batch is not None:
                batches[batch_id] = batch
        for batch in batches.values():
            runs = self.db.query(StepRun).filter(StepRun.batch_id == batch.id).all()
            commands = self.db.query(Command).filter(Command.batch_id == batch.id).all()
            pending = self.db.query(WorkflowEvent).filter(
                WorkflowEvent.batch_id == batch.id, WorkflowEvent.state.in_(["pending", "processing"]),
            ).count()
            reason = stall_reason(batch.state, runs, commands, pending)
            alarms = AlarmService(self.db, system_context(batch.org_id, "流程活性检查"))
            key = f"batch:{batch.id}:stalled"
            if not reason:
                if alarms.resolve_condition(key, f"{batch.id} 已恢复推进或不再运行"):
                    cleared += 1
                continue
            moments = [
                *(value for row in runs for value in (row.created_at, row.started_at, row.ended_at) if value),
                *(value for row in commands for value in (row.created_at, row.updated_at) if value),
            ]
            last_change = max(moments, default=None)
            if last_change is not None and moment - last_change < grace:
                continue
            before = alarms.alarms.open_by_condition(key)
            alarms.raise_alarm(
                severity=2, source_type="batch", source_id=batch.id,
                message=f"{batch.id} 显示运行中，但{reason}",
                response=(
                    "在批次页核对流程节点：返工、驳回或人工判定之后可能没有重新开出节点。"
                    "请保持批次后走恢复评估，必要时联系管理员。"
                ),
                owner="调度", origin="system", condition_key=key,
            )
            raised += before is None
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
