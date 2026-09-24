"""人员预占。

排程时为人工步骤预占执行人（任务的执行人）的时间窗：占工位的步骤用预约时间窗，其余步骤接在前驱之后按时长推。
请假、培训、值守也登记成预占。开跑检查核对：执行人在这些时间里有没有别的批次步骤、请假或培训。
批次结束时，还没到的预占取消，已过去的标为完成。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from ..core.clock import as_utc, now
from ..core.context import AccessContext
from ..core.errors import NotFound, StateConflict, ValidationFailed
from ..domain import environment as windows
from ..domain.graph import predecessors
from ..domain.steps import MANUAL, kind_of, normalize, step_id_of
from ..models import Allocation, Batch, Person, PersonBooking, User
from .audit_service import AuditService

KIND_LABEL = {"step": "批次步骤", "leave": "请假", "training": "培训", "duty": "值守"}


class StaffingService:
    def __init__(self, db: Session, ctx: AccessContext):
        self.db = db
        self.ctx = ctx
        self.audit = AuditService(db, ctx)

    def executor_of(self, batch: Batch) -> Person | None:
        """批次执行人：任务的执行人对应的人员档案。没有分配执行人时不预占。"""
        from ..repositories.recipes import ExperimentTaskRepository

        task = ExperimentTaskRepository(self.db, self.ctx).by_batch(batch.id)
        user_id = task.assignee_user_id if task is not None else ""
        if not user_id:
            return None
        return self.db.query(Person).filter(Person.org_id == self.ctx.org_id, Person.user_id == user_id).first()

    def book_batch(self, batch: Batch, begin: datetime | None = None) -> list[PersonBooking]:
        """按当前排程重建本批次的人工步骤预占（旧的未完成预占作废）。"""
        for row in self.db.query(PersonBooking).filter(
            PersonBooking.batch_id == batch.id, PersonBooking.kind == "step", PersonBooking.state == "confirmed",
        ).all():
            self.db.delete(row)
        person = self.executor_of(batch)
        steps = normalize((batch.recipe_snapshot or {}).get("steps") or [])
        manual = [index for index, step in enumerate(steps) if kind_of(step) == MANUAL]
        if person is None or not manual:
            self.db.flush()
            return []
        allocations = self.db.query(Allocation).filter(Allocation.batch_id == batch.id, Allocation.kind == "work").all()
        starts = {row.step_index: row.starts_at for row in allocations}
        ends = {row.step_index: row.ends_at for row in allocations}
        anchor = begin or min(starts.values(), default=None) or now()
        planned = windows.step_windows(steps, starts, ends, anchor, predecessors(steps))
        rows = []
        for index in manual:
            start, end = planned[index]
            if end <= start:
                end = start + timedelta(minutes=1)
            rows.append(PersonBooking(
                org_id=self.ctx.org_id, person_id=person.id, kind="step", starts_at=start, ends_at=end,
                batch_id=batch.id, step_id=step_id_of(steps[index], index),
                reason=f"{batch.id} 第 {index + 1} 步「{steps[index].get('name', '')}」",
            ))
        self.db.add_all(rows)
        self.db.flush()
        return rows

    def conflicts(self, batch: Batch) -> tuple[list[str], list[str]] | None:
        """开跑检查：执行人在本批次人工步骤的时间里是否另有安排。没有人工步骤返回 None（不适用）。

        返回（阻断, 提醒）：请假 / 培训 / 值守期间人不在，阻断；与别的在用批次的人工步骤时间重叠只提醒——
        人工步骤的时长是估计值，重叠多少由现场安排，不替人做决定。已结束批次的预占不算。
        """
        steps = normalize((batch.recipe_snapshot or {}).get("steps") or [])
        if not any(kind_of(step) == MANUAL for step in steps):
            return None
        mine = self.db.query(PersonBooking).filter(
            PersonBooking.batch_id == batch.id, PersonBooking.kind == "step", PersonBooking.state == "confirmed",
        ).all()
        if not mine and self.executor_of(batch) is not None:
            # 升级前排的程、或排程后才分配执行人：按当前排程补一次预占
            mine = self.book_batch(batch)
        if not mine:
            return [], []
        blocking, warning = [], []
        for row in mine:
            clashes = self.db.query(PersonBooking).filter(
                PersonBooking.person_id == row.person_id, PersonBooking.state == "confirmed",
                PersonBooking.batch_id != batch.id, PersonBooking.starts_at < row.ends_at,
                PersonBooking.ends_at > row.starts_at,
            ).all()
            person = self.db.get(Person, row.person_id)
            for other in clashes:
                if other.kind == "step":
                    other_batch = self.db.get(Batch, other.batch_id)
                    if other_batch is None or other_batch.state in {"done", "aborted"}:
                        continue
                text = (
                    f"{person.name if person else row.person_id} 在 {row.starts_at:%m-%d %H:%M}–{row.ends_at:%H:%M} "
                    f"已有{KIND_LABEL.get(other.kind, other.kind)}：{other.reason or other.batch_id}"
                )
                (warning if other.kind == "step" else blocking).append(text)
        return list(dict.fromkeys(blocking)), list(dict.fromkeys(warning))

    def close_batch(self, batch: Batch) -> dict:
        moment = now()
        cancelled = done = 0
        for row in self.db.query(PersonBooking).filter(
            PersonBooking.batch_id == batch.id, PersonBooking.state == "confirmed",
        ).all():
            if row.starts_at >= moment:
                row.state = "cancelled"
                cancelled += 1
            else:
                row.state = "done"
                if row.ends_at > moment:
                    row.ends_at = moment
                done += 1
        return {"cancelled": cancelled, "done": done}

    # ---------- 请假 / 培训 / 值守 ----------

    def out(self, row: PersonBooking) -> dict:
        return {
            "id": row.id, "person_id": row.person_id, "kind": row.kind, "kind_label": KIND_LABEL.get(row.kind, row.kind),
            "starts_at": row.starts_at.isoformat(timespec="minutes"), "ends_at": row.ends_at.isoformat(timespec="minutes"),
            "batch_id": row.batch_id, "step_id": row.step_id, "state": row.state, "reason": row.reason,
        }

    def list_for(self, person_id: str, include_past: bool = False) -> list[dict]:
        query = self.db.query(PersonBooking).filter(
            PersonBooking.org_id == self.ctx.org_id, PersonBooking.person_id == person_id,
            PersonBooking.state == "confirmed",
        )
        if not include_past:
            query = query.filter(PersonBooking.ends_at >= now())
        return [self.out(row) for row in query.order_by(PersonBooking.starts_at).all()]

    def create(self, person_id: str, payload: dict, user: User) -> dict:
        person = self.db.get(Person, person_id)
        if person is None or person.org_id != self.ctx.org_id:
            raise NotFound("人员不存在")
        kind = payload.get("kind") or "leave"
        if kind not in {"leave", "training", "duty"}:
            raise ValidationFailed("只能登记请假、培训或值守；批次步骤的预占由排程生成")
        start, end = as_utc(payload["starts_at"]), as_utc(payload["ends_at"])
        if end <= start:
            raise ValidationFailed("结束时间必须晚于开始时间")
        row = PersonBooking(org_id=self.ctx.org_id, person_id=person.id, kind=kind, starts_at=start, ends_at=end,
                            reason=payload.get("reason", ""), created_by=user.id)
        self.db.add(row)
        self.db.flush()
        self.audit.record(user, f"登记人员{KIND_LABEL[kind]}", person.id,
                          detail=f"{person.name} {start:%m-%d %H:%M}–{end:%m-%d %H:%M} {row.reason}")
        self.db.commit()
        return self.out(row)

    def cancel(self, booking_id: str, user: User) -> dict:
        row = self.db.get(PersonBooking, booking_id)
        if row is None or row.org_id != self.ctx.org_id:
            raise NotFound("预占不存在")
        if row.kind == "step":
            raise StateConflict("批次步骤的预占随排程变化，不能单独取消")
        row.state = "cancelled"
        self.audit.record(user, "取消人员预占", row.person_id, before=KIND_LABEL.get(row.kind), after="已取消", detail=row.reason)
        self.db.commit()
        return self.out(row)
