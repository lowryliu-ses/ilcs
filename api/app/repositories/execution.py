from __future__ import annotations

from ..models import AdapterExecution, Batch, Checkpoint, Command, Telemetry
from .base import Repository, ScopedRepository

OPEN_STATES = ["sent", "accepted", "running"]
# 会驱动设备做实验动作、完成后推进步骤的指令类型；保持 / 终止是安全动作，不在其中
DISPATCHING = {"dispatch", "resume", "retry"}
# 会让设备物理动作的指令：实验动作 + 转运。执行门、联锁、保持撤回、工位忙闲都按它判；
# 转运不在 DISPATCHING 里——它完成只是把板送到位，不代表步骤完成
MOTION = DISPATCHING | {"transfer"}


class CommandRepository(ScopedRepository[Command]):
    model = Command

    def for_batch(self, batch_id: str) -> list[Command]:
        return list(
            self.query().filter(Command.batch_id == batch_id).order_by(Command.created_at).all()
        )

    def recent(self, limit: int = 50) -> list[Command]:
        return list(self.query().order_by(Command.created_at.desc()).limit(limit).all())

    def pending(
        self, limit: int = 50, *, at=None, dispatch_open: bool = True, station_id: str | None = None,
    ) -> list[Command]:
        """执行器领取队列。跨组织一起取：执行器按持久化指令上的 org_id 确定范围。

        只取「现在就能投」的指令：未到最早投递时刻的、执行门关闭时的动作指令都留在队列里
        但不进候选。否则它们占满 LIMIT，排在后面已经到点的指令永远轮不到（队首阻塞），
        且每条都要白白取一次批次行锁。保持 / 终止排在动作指令前面：现场要停的时候先停。
        """
        from sqlalchemy import case, or_

        from ..core.clock import now

        moment = at or now()
        query = self.db.query(Command).filter(
            Command.state == "sent",
            Command.delivery_state == "queued",
            or_(Command.not_before.is_(None), Command.not_before <= moment),
        )
        if not dispatch_open:
            query = query.filter(Command.type.notin_(sorted(MOTION)))
        if station_id is not None:
            query = query.filter(Command.station_id == station_id)
        # 前置指令（转运）没确认完成之前，设备动作不进候选
        from sqlalchemy.orm import aliased

        before = aliased(Command)
        query = query.filter(
            or_(
                Command.after_command_id == "",
                self.db.query(before.id)
                .filter(before.id == Command.after_command_id, before.state == "done")
                .exists(),
            )
        )
        safety_first = case((Command.type.in_(sorted(MOTION)), 1), else_=0)
        return list(query.order_by(safety_first, Command.created_at).limit(limit).all())

    def in_flight(self, station_id: str | None = None) -> list[Command]:
        query = self.db.query(Command).filter(Command.state.in_(["accepted", "running"]))
        if station_id is not None:
            query = query.filter(Command.station_id == station_id)
        return list(query.order_by(Command.created_at).all())

    def stations_with_open_work(self, at=None) -> set[str]:
        """有事要做的工位：在途、可能已发出、或已到点待投递的指令所在工位。并发执行器按它派活。"""
        from sqlalchemy import or_

        from ..core.clock import now

        from sqlalchemy.orm import aliased

        moment = at or now()
        before = aliased(Command)
        ready = or_(
            Command.after_command_id == "",
            self.db.query(before.id)
            .filter(before.id == Command.after_command_id, before.state == "done")
            .exists(),
        )
        rows = (
            self.db.query(Command.station_id)
            .filter(
                or_(
                    Command.state.in_(["accepted", "running"]),
                    Command.delivery_state == "maybe_sent",
                    (Command.state == "sent") & (Command.delivery_state == "queued")
                    & or_(Command.not_before.is_(None), Command.not_before <= moment)
                    & ready,
                )
            )
            .distinct()
            .all()
        )
        return {row[0] for row in rows if row[0]}

    def maybe_sent(self, station_id: str | None = None) -> list[Command]:
        """可能已发出但结果未确认的指令。重启先按原 command_id 查设备侧状态。"""
        query = self.db.query(Command).filter(
            Command.delivery_state == "maybe_sent", Command.state.in_(OPEN_STATES)
        )
        if station_id is not None:
            query = query.filter(Command.station_id == station_id)
        return list(query.order_by(Command.created_at).all())

    def open_for_step(self, batch_id: str, step_index: int) -> Command | None:
        return (
            self.query()
            .filter(
                Command.batch_id == batch_id,
                Command.step_index == step_index,
                Command.state.in_(OPEN_STATES),
            )
            .order_by(Command.created_at.desc())
            .first()
        )

    def dependents_of(self, command_id: str) -> list[Command]:
        """以这条指令为前置、还在队列里的指令。"""
        return list(
            self.db.query(Command)
            .filter(
                Command.after_command_id == command_id,
                Command.state == "sent",
                Command.delivery_state == "queued",
            )
            .all()
        )

    def queued_for_batch(self, batch_id: str) -> list[Command]:
        """还没交给适配器的指令。保持 / 终止时撤回它们，设备侧从未见过。"""
        return list(
            self.db.query(Command)
            .filter(
                Command.batch_id == batch_id,
                Command.state == "sent",
                Command.delivery_state == "queued",
            )
            .all()
        )

    def in_flight_for_batch(self, batch_id: str, types: set[str]) -> list[Command]:
        return list(
            self.db.query(Command)
            .filter(
                Command.batch_id == batch_id,
                Command.state.in_(["accepted", "running"]),
                Command.type.in_(sorted(types)),
            )
            .all()
        )

    def possibly_acting(self, batch_id: str, types: set[str]) -> list[Command]:
        """设备侧可能仍在动作的指令：在途，或结果未知且可能已送达。"""
        return [
            command
            for command in self.db.query(Command)
            .filter(Command.batch_id == batch_id, Command.type.in_(sorted(types)))
            .all()
            if command.state in {"accepted", "running"}
            or (command.state == "unknown" and command.delivery_state == "maybe_sent")
        ]

    def ever_delivered_for_run(self, step_run_id: str) -> bool:
        if not step_run_id:
            return False
        return (
            self.db.query(Command)
            .filter(
                Command.step_run_id == step_run_id,
                Command.type.in_(sorted(DISPATCHING)),
                Command.delivery_state.in_(["maybe_sent", "delivered"]),
            )
            .count()
            > 0
        )

    def for_step_run(self, step_run_id: str) -> list[Command]:
        return list(self.query().filter(Command.step_run_id == step_run_id).all())

    def unknown_count(self) -> int:
        return self.query().filter(Command.state == "unknown").count()


class CheckpointRepository(Repository[Checkpoint]):
    model = Checkpoint

    def for_batch(self, batch_id: str) -> list[Checkpoint]:
        return list(
            self.db.query(Checkpoint).filter(Checkpoint.batch_id == batch_id).order_by(Checkpoint.created_at).all()
        )

    def latest_for_step(self, batch_id: str, step_index: int) -> Checkpoint | None:
        return (
            self.db.query(Checkpoint)
            .filter(Checkpoint.batch_id == batch_id, Checkpoint.step_index == step_index)
            .order_by(Checkpoint.created_at.desc())
            .first()
        )


class AdapterExecutionRepository(Repository[AdapterExecution]):
    model = AdapterExecution


class TelemetryRepository(Repository[Telemetry]):
    model = Telemetry

    def for_batch(self, batch_id: str, limit: int = 120) -> list[Telemetry]:
        return list(
            self.db.query(Telemetry)
            .filter(Telemetry.batch_id == batch_id)
            .order_by(Telemetry.device_ts.desc())
            .limit(limit)
            .all()
        )

    def series_for_batch(self, batch_id: str, limit: int = 4000) -> list[Telemetry]:
        return list(
            self.db.query(Telemetry)
            .filter(Telemetry.batch_id == batch_id)
            .order_by(Telemetry.device_ts)
            .limit(limit)
            .all()
        )

    def latest_per_metric(self, batch_id: str) -> list[Telemetry]:
        latest: dict[tuple[str, str], Telemetry] = {}
        for point in self.series_for_batch(batch_id):
            latest[(point.station_id, point.metric)] = point
        return sorted(latest.values(), key=lambda p: (p.station_id, p.metric))
