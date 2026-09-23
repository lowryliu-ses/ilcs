from __future__ import annotations

from ..models import AdapterExecution, Batch, Checkpoint, Command, Telemetry
from .base import Repository, ScopedRepository

OPEN_STATES = ["sent", "accepted", "running"]
# 会驱动设备做实验动作的指令类型；保持 / 终止是安全动作，不在其中
DISPATCHING = {"dispatch", "resume", "retry"}


class CommandRepository(ScopedRepository[Command]):
    model = Command

    def for_batch(self, batch_id: str) -> list[Command]:
        return list(
            self.query().filter(Command.batch_id == batch_id).order_by(Command.created_at).all()
        )

    def recent(self, limit: int = 50) -> list[Command]:
        return list(self.query().order_by(Command.created_at.desc()).limit(limit).all())

    def pending(self, limit: int = 50) -> list[Command]:
        """执行器领取队列。跨组织一起取：执行器按持久化指令上的 org_id 确定范围。"""
        return list(
            self.db.query(Command)
            .filter(Command.state == "sent")
            .order_by(Command.created_at)
            .limit(limit)
            .all()
        )

    def in_flight(self) -> list[Command]:
        return list(self.db.query(Command).filter(Command.state.in_(["accepted", "running"])).all())

    def maybe_sent(self) -> list[Command]:
        """可能已发出但结果未确认的指令。重启先按原 command_id 查设备侧状态。"""
        return list(
            self.db.query(Command)
            .filter(Command.delivery_state == "maybe_sent", Command.state.in_(OPEN_STATES))
            .all()
        )

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
