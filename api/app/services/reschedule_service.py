"""动态重排：事件触发的重排建议。

触发：工位失联 / 心跳超时 / 故障（异常引擎）、指令故障（策略要求重排）、紧急插单（最高优先级批次
按现有时间线赶不上交付期）、人工请求。

建议只重排「还没开始」的部分：已下发批次保护已经开出的步骤，只对其后全部未开出的步骤重新求解；
未下发批次整体重排。不可用的工位从候选里剔除，受影响批次按依赖、优先级、交付期依次排，
彼此共享同一份时间线，不会互相撞车。建议记下重排前后的时间窗与每个批次完成时间的变化，
调度确认后才写入；写入前核对时间线自生成以来没被改过，改过就作废——不按过期的判断写入。

`ILCS_AUTO_RESCHEDULE=1` 时，只涉及未下发批次、且全部排得下的建议自动应用；在途批次的建议
一律待调度确认：谁让路是调度决定，不是算法决定。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from ..core.clock import now
from ..core.config import settings
from ..core.context import AccessContext
from ..core.errors import NotFound, PermissionDenied, StateConflict
from ..domain import tasks as task_rules
from ..domain.scheduling import WORK, Interval, SchedulingError, plan_steps
from ..domain.steps import normalize
from ..models import Allocation, Batch, ScheduleProposal, StepRun, User
from ..repositories.batches import AllocationRepository, BatchRepository
from .audit_service import AuditService

ACTIVE = {"scheduled", "running", "paused", "fault"}
TRIGGERS = {
    "station_condition": "工位不可用", "command_fault": "指令故障", "priority_insert": "紧急插单",
    "manual": "人工请求", "dependency": "依赖冲突",
}
STATE_LABEL = {"pending": "待确认", "applied": "已应用", "dismissed": "已驳回", "stale": "已作废", "failed": "应用失败"}


def _window(row: Allocation) -> dict:
    return {
        "step_index": row.step_index, "station_id": row.station_id, "kind": row.kind,
        "starts_at": row.starts_at.isoformat(timespec="seconds"), "ends_at": row.ends_at.isoformat(timespec="seconds"),
    }


class RescheduleService:
    def __init__(self, db: Session, ctx: AccessContext):
        from .schedule_service import ScheduleService

        self.db = db
        self.ctx = ctx
        self.schedule = ScheduleService(db, ctx)
        self.allocations = AllocationRepository(db)
        self.batches = BatchRepository(db, ctx)
        self.audit = AuditService(db, ctx)

    # ---------- 计算 ----------

    def affected(self, station_id: str = "", batch_ids: list[str] | None = None) -> list[Batch]:
        found: dict[str, Batch] = {}
        for batch_id in batch_ids or []:
            batch = self.batches.get(batch_id)
            if batch is not None and batch.state in ACTIVE:
                found[batch.id] = batch
        if station_id:
            # 批次行带 JSON 列，不能整行 DISTINCT：先取去重的批次号再逐个取
            ids = [
                row[0] for row in self.db.query(Batch.id).join(Allocation, Allocation.batch_id == Batch.id)
                .filter(
                    Allocation.station_id == station_id, Allocation.ends_at > now(),
                    Batch.state.in_(list(ACTIVE)), Batch.org_id == self.ctx.org_id,
                ).distinct().all()
            ]
            for batch_id in sorted(ids):
                batch = self.batches.get(batch_id)
                if batch is not None:
                    found.setdefault(batch.id, batch)
        return list(found.values())

    def _live_indices(self, batch: Batch) -> set[int]:
        return {
            row.step_index for row in self.db.query(StepRun).filter(StepRun.batch_id == batch.id).all()
            if row.state not in {"superseded", "cancelled"}
        }

    def _from_step(self, batch: Batch, steps: list[dict]) -> int | None:
        """从哪一步起重排：未下发整体重排；已下发从「其后全部未开出」的第一步起，开出过的一步都不动。"""
        if batch.state == "scheduled":
            return 0
        live = self._live_indices(batch)
        start = max(live) + 1 if live else 0
        return start if start < len(steps) else None

    def _before(self, batch: Batch) -> list[dict]:
        return [_window(row) for row in sorted(
            self.allocations.for_batch(batch.id), key=lambda row: (row.step_index, row.kind, row.starts_at),
        )]

    def plan(self, batches: list[Batch], unavailable: dict[str, str]) -> tuple[dict, dict, list[dict]]:
        """按依赖、优先级、交付期依次重排受影响批次，彼此共享一份时间线。返回 (after, impact, unplanned)。"""
        affected_ids = {batch.id for batch in batches}
        context = self.schedule.context(affected_ids)
        context.unavailable_station_ids.update(unavailable)
        by_id = {batch.id: batch for batch in batches}
        upstream = {batch.id: [ref for ref in self.schedule.upstream_batch_ids(batch) if ref in affected_ids] for batch in batches}

        def due(batch: Batch) -> datetime:
            task = self.schedule._task_of(batch)
            return task.due_at if task is not None and task.due_at else datetime.max

        ordered = sorted(batches, key=lambda row: (row.priority, due(row), row.created_at))
        order = task_rules.topo_order([row.id for row in ordered], upstream)
        after: dict[str, dict] = {}
        impact: dict[str, dict] = {}
        unplanned: list[dict] = []
        planned_end: dict[str, datetime] = {}
        begin_default = now() + timedelta(minutes=5)
        # 先把各批次要保护的时间窗（已开出的步骤）压进时间线，再逐个排尾段
        plans: dict[str, tuple[list[dict], int, list[Allocation]]] = {}
        for batch_id in order:
            batch = by_id[batch_id]
            steps = normalize(batch.recipe_snapshot.get("steps") or [])
            from_step = self._from_step(batch, steps)
            if from_step is None:
                continue
            protected = [row for row in self.allocations.for_batch(batch.id) if row.step_index < from_step]
            for row in protected:
                context.busy.setdefault(row.station_id, []).append(Interval(row.starts_at, row.ends_at))
            plans[batch_id] = (steps, from_step, protected)
        for batch_id in order:
            if batch_id not in plans:
                continue
            batch = by_id[batch_id]
            steps, from_step, protected = plans[batch_id]
            floor, missing = self.schedule.dependency_floor(batch, skip=affected_ids)
            if missing:
                unplanned.append({"batch_id": batch.id, "reason": "；".join(missing)})
                continue
            candidates = [begin_default, *([floor] if floor else []), *(planned_end[ref] for ref in upstream[batch_id] if ref in planned_end)]
            begin = max(candidates)
            try:
                if from_step == 0:
                    planned = plan_steps(steps, begin, context)
                else:
                    anchor, station = self.schedule._tail_anchor(batch, steps, from_step, protected)
                    planned = plan_steps(
                        steps, max(begin, anchor) if anchor else begin, context,
                        first_index=from_step, previous_end=anchor, previous_station=station,
                    )
            except SchedulingError as error:
                unplanned.append({"batch_id": batch.id, "reason": error.message})
                continue
            rows = [
                {"step_index": item.step_index, "station_id": item.station_id, "kind": item.kind,
                 "starts_at": item.starts_at.isoformat(timespec="seconds"),
                 "ends_at": item.ends_at.isoformat(timespec="seconds")}
                for item in planned
            ]
            after[batch.id] = {"from_step": from_step, "allocations": rows}
            old = [row for row in self.allocations.for_batch(batch.id) if row.step_index >= from_step and row.kind == WORK]
            new = [item for item in planned if item.kind == WORK]
            old_end = max((row.ends_at for row in old), default=None)
            new_end = max((item.ends_at for item in new), default=None)
            if new_end is not None:
                planned_end[batch.id] = new_end
            old_station = {row.step_index: row.station_id for row in old}
            moved = [
                f"第 {item.step_index + 1} 步 {old_station[item.step_index]} → {item.station_id}"
                for item in new if item.step_index in old_station and old_station[item.step_index] != item.station_id
            ]
            task = self.schedule._task_of(batch)
            late = (
                round((new_end - task.due_at).total_seconds() / 60)
                if task is not None and task.due_at and new_end and new_end > task.due_at else 0
            )
            impact[batch.id] = {
                "state": batch.state,
                "from_step": from_step,
                "old_end": old_end.isoformat(timespec="minutes") if old_end else None,
                "new_end": new_end.isoformat(timespec="minutes") if new_end else None,
                "delay_min": round((new_end - old_end).total_seconds() / 60) if old_end and new_end else None,
                "moved": moved,
                "late_min": late,
            }
        return after, impact, unplanned

    # ---------- 建议 ----------

    def propose(
        self, *, trigger: str, reason: str, station_id: str = "", batch_ids: list[str] | None = None,
        unavailable: dict[str, str] | None = None,
    ) -> ScheduleProposal | None:
        batches = self.affected(station_id, batch_ids)
        if not batches:
            return None
        blocked = dict(unavailable or {})
        if station_id and trigger in {"station_condition", "command_fault"}:
            blocked.setdefault(station_id, f"{station_id} 不可用：{reason}")
        after, impact, unplanned = self.plan(batches, blocked)
        if not after and not unplanned:
            return None
        for older in self.db.query(ScheduleProposal).filter(
            ScheduleProposal.org_id == self.ctx.org_id, ScheduleProposal.state == "pending",
            ScheduleProposal.trigger == trigger, ScheduleProposal.station_id == station_id,
        ).all():
            if set(older.batch_ids or []) <= {batch.id for batch in batches}:
                older.state = "stale"
                older.note = "被新的重排建议取代"
        proposal = ScheduleProposal(
            org_id=self.ctx.org_id, trigger=trigger, reason=reason[:2000], station_id=station_id,
            batch_ids=[batch.id for batch in batches], before={batch.id: self._before(batch) for batch in batches},
            after=after, impact=impact, unplanned=unplanned,
        )
        self.db.add(proposal)
        self.db.flush()
        self.audit.record(
            None, "生成重排建议", proposal.id, after=f"{len(after)} 个批次可重排",
            detail=(
                f"{TRIGGERS.get(trigger, trigger)}：{reason}"
                + (f"；{len(unplanned)} 个批次排不下" if unplanned else "")
            ),
        )
        if settings.auto_reschedule and not unplanned and all(batch.state == "scheduled" for batch in batches):
            try:
                # 保存点：自动应用失败只撤销它自己，不连带调用方（设备监控、异常引擎）的事务
                with self.db.begin_nested():
                    self.apply(proposal.id, None, auto=True)
            except StateConflict as error:
                proposal.note = f"自动应用未成功：{error}"
        return proposal

    def apply(self, proposal_id: str, user: User | None, auto: bool = False) -> dict:
        from .schedule_service import lock_schedule

        if user is not None and not self.ctx.has("batch.schedule"):
            raise PermissionDenied("当前角色不能排程")
        proposal = self._require(proposal_id)
        if proposal.state != "pending":
            raise StateConflict(f"重排建议已是「{STATE_LABEL.get(proposal.state, proposal.state)}」")
        lock_schedule(self.db)
        stale: list[str] = []
        for batch_id in proposal.after:
            batch = self.batches.get(batch_id)
            if batch is None or batch.state not in ACTIVE:
                stale.append(f"{batch_id} 已不在可重排状态")
                continue
            if self._before(batch) != (proposal.before or {}).get(batch_id):
                stale.append(f"{batch_id} 的时间线在建议生成后被改过")
                continue
            from_step = int(proposal.after[batch_id]["from_step"])
            if any(index >= from_step for index in self._live_indices(batch)):
                stale.append(f"{batch_id} 第 {from_step + 1} 步之后又有步骤开出")
        if stale:
            proposal.state = "stale"
            proposal.note = "；".join(stale)
            self.db.commit()
            raise StateConflict(
                "重排建议已过期，请重新生成", {"blocked": [{"key": "stale", "label": text} for text in stale]},
                code="proposal_stale",
            )
        from ..repositories.resources import StationRepository

        asset_of = {station.id: station.asset_id for station in StationRepository(self.db, self.ctx).list() if station.asset_id}
        for batch_id, plan in proposal.after.items():
            from_step = int(plan["from_step"])
            for row in self.allocations.for_batch(batch_id):
                if row.step_index >= from_step:
                    self.db.delete(row)
            self.db.flush()
            for row in plan["allocations"]:
                self.db.add(Allocation(
                    batch_id=batch_id, step_index=row["step_index"], station_id=row["station_id"],
                    asset_id=asset_of.get(row["station_id"], ""), kind=row["kind"],
                    starts_at=datetime.fromisoformat(row["starts_at"]), ends_at=datetime.fromisoformat(row["ends_at"]),
                ))
            self.db.flush()
            self.schedule._refuse_overlaps(batch_id)
            change = (proposal.impact or {}).get(batch_id) or {}
            self.audit.record(
                user, "按重排建议重排", batch_id, before=change.get("old_end") or "—", after=change.get("new_end") or "—",
                detail=(
                    f"建议 {proposal.id[:8]}（{TRIGGERS.get(proposal.trigger, proposal.trigger)}）；自第 {from_step + 1} 步重排"
                    + (f"；{'、'.join(change.get('moved') or [])}" if change.get("moved") else "")
                    + ("；自动应用（只涉及未下发批次）" if auto else "")
                ),
            )
        proposal.state = "applied"
        proposal.auto_applied = auto
        proposal.decided_by = user.display_name if user else "系统"
        proposal.decided_at = now()
        if not auto:
            self.db.commit()
        return self.out(proposal)

    def dismiss(self, proposal_id: str, note: str, user: User) -> dict:
        if not self.ctx.has("batch.schedule"):
            raise PermissionDenied("当前角色不能排程")
        proposal = self._require(proposal_id)
        if proposal.state != "pending":
            raise StateConflict(f"重排建议已是「{STATE_LABEL.get(proposal.state, proposal.state)}」")
        proposal.state = "dismissed"
        proposal.note = (note or "").strip() or "调度驳回"
        proposal.decided_by = user.display_name
        proposal.decided_at = now()
        self.audit.record(user, "驳回重排建议", proposal.id, before="待确认", after="已驳回", detail=proposal.note)
        self.db.commit()
        return self.out(proposal)

    def request(self, batch_ids: list[str], station_id: str, reason: str, user: User) -> dict:
        """人工请求：为选中的批次（或某台工位上的全部批次）生成重排建议。"""
        if not self.ctx.has("batch.schedule"):
            raise PermissionDenied("当前角色不能排程")
        proposal = self.propose(
            trigger="manual", reason=(reason or "").strip() or "调度请求重排", station_id=station_id,
            batch_ids=batch_ids, unavailable={station_id: f"{station_id} 人工标为不用"} if station_id else None,
        )
        if proposal is None:
            raise StateConflict("所选批次没有可以重排的未执行步骤", code="nothing_to_reschedule")
        self.audit.record(user, "请求重排建议", proposal.id, detail=reason or "")
        self.db.commit()
        return self.out(proposal)

    # ---------- 读 ----------

    def _require(self, proposal_id: str) -> ScheduleProposal:
        proposal = self.db.query(ScheduleProposal).filter(
            ScheduleProposal.id == proposal_id, ScheduleProposal.org_id == self.ctx.org_id,
        ).first()
        if proposal is None:
            raise NotFound("重排建议不存在")
        return proposal

    def list(self, state: str = "") -> list[dict]:
        query = self.db.query(ScheduleProposal).filter(ScheduleProposal.org_id == self.ctx.org_id)
        if state:
            query = query.filter(ScheduleProposal.state == state)
        return [self.out(row) for row in query.order_by(ScheduleProposal.created_at.desc()).limit(100).all()]

    @staticmethod
    def out(proposal: ScheduleProposal) -> dict:
        return {
            "id": proposal.id, "trigger": proposal.trigger, "trigger_label": TRIGGERS.get(proposal.trigger, proposal.trigger),
            "reason": proposal.reason, "station_id": proposal.station_id, "state": proposal.state,
            "state_label": STATE_LABEL.get(proposal.state, proposal.state), "batch_ids": proposal.batch_ids or [],
            "after": proposal.after or {}, "impact": proposal.impact or {}, "unplanned": proposal.unplanned or [],
            "auto_applied": proposal.auto_applied,
            "created_at": proposal.created_at.isoformat(timespec="seconds") if proposal.created_at else None,
            "decided_by": proposal.decided_by,
            "decided_at": proposal.decided_at.isoformat(timespec="seconds") if proposal.decided_at else None,
            "note": proposal.note,
        }
