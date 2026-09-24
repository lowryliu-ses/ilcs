from __future__ import annotations

from datetime import datetime

from ..core.clock import now
from ..models import BatchSignal, StepAdvance, StepRun, WorkflowEvent
from .base import Repository, ScopedRepository


class StepRunRepository(ScopedRepository[StepRun]):
    model = StepRun

    def for_batch(self, batch_id: str) -> list[StepRun]:
        return list(
            self.query()
            .filter(StepRun.batch_id == batch_id)
            .order_by(StepRun.step_index, StepRun.attempt)
            .all()
        )

    def latest(self, batch_id: str, step_id: str) -> StepRun | None:
        return (
            self.query()
            .filter(StepRun.batch_id == batch_id, StepRun.step_id == step_id)
            .order_by(StepRun.attempt.desc())
            .first()
        )

    def attempts(self, batch_id: str, step_id: str) -> int:
        return self.query().filter(StepRun.batch_id == batch_id, StepRun.step_id == step_id).count()

    def open_runs(self, batch_id: str) -> list[StepRun]:
        return [
            run for run in self.for_batch(batch_id)
            if run.state in {"pending", "ready", "running", "waiting"}
        ]

    def current(self, batch_id: str) -> StepRun | None:
        runs = self.open_runs(batch_id)
        return runs[0] if runs else None

    def due_waits(self, at: datetime | None = None) -> list[StepRun]:
        moment = at or now()
        return list(
            self.query()
            .filter(
                StepRun.kind == "wait",
                StepRun.state == "waiting",
                StepRun.due_at.isnot(None),
                StepRun.due_at <= moment,
            )
            .all()
        )

    def pending_manual(self, assignee: str = "") -> list[StepRun]:
        query = self.query().filter(StepRun.kind == "manual", StepRun.state.in_(["ready", "running"]))
        if assignee:
            query = query.filter(StepRun.assignee_user_id == assignee)
        return list(query.order_by(StepRun.due_at).all())

    def overdue_deadlines(self, at: datetime | None = None) -> list[StepRun]:
        """到了步骤级截止时刻、还开着、还没处理过超时的实例。"""
        moment = at or now()
        return list(
            self.query()
            .filter(
                StepRun.deadline_at.isnot(None),
                StepRun.deadline_at <= moment,
                StepRun.timed_out_at.is_(None),
                StepRun.state.in_(["pending", "ready", "running", "waiting"]),
            )
            .order_by(StepRun.deadline_at)
            .all()
        )

    def pending_branch_choices(self) -> list[StepRun]:
        return list(
            self.query()
            .filter(StepRun.kind == "branch", StepRun.state == "ready")
            .order_by(StepRun.created_at)
            .all()
        )

    def waiting_for_event(self, batch_id: str, name: str) -> list[StepRun]:
        rows = (
            self.query()
            .filter(StepRun.batch_id == batch_id, StepRun.kind == "wait", StepRun.state == "waiting")
            .order_by(StepRun.started_at, StepRun.created_at)
            .all()
        )
        return [
            row for row in rows
            if ((row.step_snapshot or {}).get("wait_for") or {}).get("mode") == "event"
            and ((row.step_snapshot or {}).get("wait_for") or {}).get("event") == name
        ]

    def pending_review(self) -> list[StepRun]:
        return list(
            self.query()
            .filter(StepRun.kind == "review", StepRun.state.in_(["ready", "running"]))
            .order_by(StepRun.created_at)
            .all()
        )

    def lock(self, step_run_id: str) -> StepRun | None:
        """取行锁。同一步的两个事件并发到达时，只有一个能推进。"""
        query = self.query().filter(StepRun.id == step_run_id).populate_existing()
        query = query.with_for_update()
        return query.first()


class WorkflowEventRepository(ScopedRepository[WorkflowEvent]):
    model = WorkflowEvent

    def find_key(self, event_key: str) -> WorkflowEvent | None:
        return self.query().filter(WorkflowEvent.event_key == event_key).first()

    def find_key_any_org(self, event_key: str) -> WorkflowEvent | None:
        return self.db.query(WorkflowEvent).filter(WorkflowEvent.event_key == event_key).first()

    def claim_batch(self, limit: int = 20, at: datetime | None = None) -> list[WorkflowEvent]:
        """领取待处理事件。

        后台可能有多个进程，所以领取要可恢复：先把行标成 processing 并写上领取时间，
        崩溃后超时的 processing 行会被下一轮重新领取。这里返回候选，标记由服务层在
        同一个短事务里做。
        """
        moment = at or now()
        # 按组织领取：推进器逐组织运行，步骤实例查找也按组织过滤。跨组织领到的事件
        # 在本组织上下文里找不到步骤实例，会被误判为 rejected 永久丢掉。
        query = (
            self.query()
            .filter(
                WorkflowEvent.state == "pending",
                WorkflowEvent.available_at <= moment,
            )
            .order_by(WorkflowEvent.created_at)
            .limit(limit)
        )
        query = query.with_for_update(skip_locked=True)
        return list(query.all())

    def stale_processing(self, before: datetime) -> list[WorkflowEvent]:
        return list(
            self.query()
            .filter(WorkflowEvent.state == "processing", WorkflowEvent.claimed_at < before)
            .all()
        )

    def for_batch(self, batch_id: str) -> list[WorkflowEvent]:
        return list(
            self.query()
            .filter(WorkflowEvent.batch_id == batch_id)
            .order_by(WorkflowEvent.created_at)
            .all()
        )


class BatchSignalRepository(ScopedRepository[BatchSignal]):
    model = BatchSignal

    def by_key(self, event_key: str) -> BatchSignal | None:
        return self.query().filter(BatchSignal.event_key == event_key).first()

    def unconsumed(self, batch_id: str, name: str) -> BatchSignal | None:
        return (
            self.query()
            .filter(
                BatchSignal.batch_id == batch_id, BatchSignal.name == name,
                BatchSignal.consumed_by_run_id == "",
            )
            .order_by(BatchSignal.received_at)
            .with_for_update(skip_locked=True)
            .first()
        )

    def for_batch(self, batch_id: str) -> list[BatchSignal]:
        return list(
            self.query().filter(BatchSignal.batch_id == batch_id).order_by(BatchSignal.received_at).all()
        )


class StepAdvanceRepository(Repository[StepAdvance]):
    """下一节点创建的去重表。写入靠唯一约束，不做「先查再插」。"""

    model = StepAdvance

    def claim(self, batch_id: str, from_step_id: str, from_attempt: int, to_step_id: str) -> bool:
        from sqlalchemy.exc import IntegrityError

        row = StepAdvance(
            batch_id=batch_id, from_step_id=from_step_id, from_attempt=from_attempt,
            to_step_id=to_step_id,
        )
        try:
            # 保存点：冲突只撤销这一行，不回滚同一事务里已经做完的步骤转换
            with self.db.begin_nested():
                self.db.add(row)
                self.db.flush()
        except IntegrityError:
            return False
        return True
