"""异常引擎：统一登记异常事件，按策略库决定处理动作，记录自动与人工处理的结果。

入口：
- `on_command_fault`：执行器把一条设备指令判为故障时（`ExecutionService.fault`）。
- `on_station_condition`：设备监控发现工位失联 / 心跳超时 / 联锁时。
- `on_step_timeout`、`on_hold`：步骤级超时、分支 / 关卡保持待人工判断。
- `settle_batch`：批次恢复、跳过、重做、终止、完成时，给它还开着的异常写最终结果。

自动动作只有三种会重新驱动设备（重试、改派、跳过），它们只在指令**从未送达设备**时执行；
其余一律登记后转人工（见 `domain/exceptions.py`）。自动动作在调用方的事务里完成：成功了批次
直接回到运行中，失败了批次保持在故障、事件留在待处理，原因写清楚。
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy.orm import Session

from ..core.clock import now
from ..core.context import AccessContext
from ..core.errors import NotFound, PermissionDenied, StateConflict, ValidationFailed
from ..domain import exceptions as rules
from ..domain.steps import kind_of, normalize, step_id_of
from ..models import Allocation, Batch, Command, ExceptionEvent, ExceptionRule, StepRun, User
from .audit_service import AuditService

_GUARD = "ilcs_exception_handling"
OPEN = {"open", "manual"}


class ExceptionService:
    def __init__(self, db: Session, ctx: AccessContext):
        self.db = db
        self.ctx = ctx
        self.audit = AuditService(db, ctx)

    # ---------- 查询 ----------

    def _query(self):
        return self.db.query(ExceptionEvent).filter(ExceptionEvent.org_id == self.ctx.org_id)

    def get(self, event_id: str) -> ExceptionEvent:
        event = self._query().filter(ExceptionEvent.id == event_id).first()
        if event is None:
            raise NotFound("异常事件不存在")
        return event

    def list(self, state: str = "", category: str = "", batch_id: str = "", limit: int = 200) -> list[dict]:
        query = self._query()
        if state == "open":
            query = query.filter(ExceptionEvent.state.in_(list(OPEN)))
        elif state:
            query = query.filter(ExceptionEvent.state == state)
        if category:
            query = query.filter(ExceptionEvent.category == category)
        if batch_id:
            query = query.filter(ExceptionEvent.batch_id == batch_id)
        return [self.out(row) for row in query.order_by(ExceptionEvent.created_at.desc()).limit(limit).all()]

    def summary(self) -> dict:
        rows = self._query().all()
        open_rows = [row for row in rows if row.state in OPEN]
        by_category: dict[str, int] = {}
        for row in open_rows:
            by_category[row.category] = by_category.get(row.category, 0) + 1
        return {
            "open": len(open_rows),
            "auto_resolved": len([row for row in rows if row.state == "auto_resolved"]),
            "total": len(rows),
            "by_category": [
                {"category": key, "label": rules.CATEGORIES.get(key, key), "count": count}
                for key, count in sorted(by_category.items(), key=lambda item: -item[1])
            ],
        }

    @staticmethod
    def out(event: ExceptionEvent) -> dict:
        return {
            "id": event.id,
            "category": event.category,
            "category_label": rules.CATEGORIES.get(event.category, event.category),
            "severity": event.severity,
            "source_type": event.source_type,
            "source_id": event.source_id,
            "batch_id": event.batch_id,
            "step_id": event.step_id,
            "step_index": event.step_index,
            "station_id": event.station_id,
            "command_id": event.command_id,
            "alarm_id": event.alarm_id,
            "message": event.message,
            "impact": event.impact or {},
            "never_sent": event.never_sent,
            "state": event.state,
            "state_label": rules.STATES.get(event.state, event.state),
            "rule_id": event.rule_id,
            "decision": event.decision,
            "auto_action": event.auto_action,
            "auto_action_label": rules.ACTIONS.get(event.auto_action, ""),
            "auto_result": event.auto_result,
            "manual_action": event.manual_action,
            "manual_note": event.manual_note,
            "manual_by": event.manual_by,
            "final_result": event.final_result,
            "created_at": event.created_at.isoformat(timespec="seconds"),
            "updated_at": event.updated_at.isoformat(timespec="seconds") if event.updated_at else None,
            "resolved_at": event.resolved_at.isoformat(timespec="seconds") if event.resolved_at else None,
        }

    # ---------- 登记 ----------

    def impact_of(self, batch: Batch | None, station_id: str = "") -> dict:
        """影响面：受影响的批次（本批次 + 同工位还有后续占用的批次）、未完成样本数、工位、任务。"""
        from ..repositories.batches import SampleRepository

        batches: list[str] = []
        tasks: list[str] = []
        samples = 0
        if batch is not None:
            batches.append(batch.id)
            if batch.task_id:
                tasks.append(batch.task_id)
            samples += SampleRepository(self.db, self.ctx).unfinished_count(batch.id)
        if station_id:
            rows = (
                self.db.query(Allocation, Batch)
                .join(Batch, Batch.id == Allocation.batch_id)
                .filter(
                    Allocation.station_id == station_id, Allocation.ends_at > now(),
                    Batch.state.notin_(["done", "aborted"]), Batch.org_id == self.ctx.org_id,
                ).all()
            )
            for _, other in rows:
                if other.id not in batches:
                    batches.append(other.id)
                    if other.task_id and other.task_id not in tasks:
                        tasks.append(other.task_id)
                    samples += SampleRepository(self.db, self.ctx).unfinished_count(other.id)
        return {"batches": batches, "samples": samples, "stations": [station_id] if station_id else [], "tasks": tasks}

    def record(
        self, *, category: str, message: str, source_type: str, source_id: str, batch: Batch | None = None,
        step_index: int = -1, station_id: str = "", command_id: str = "", alarm_id: str = "", severity: int = 2,
        never_sent: bool = False,
    ) -> ExceptionEvent:
        step_id = ""
        if batch is not None and step_index >= 0:
            steps = normalize(batch.recipe_snapshot.get("steps") or [])
            if step_index < len(steps):
                step_id = step_id_of(steps[step_index], step_index)
        event = ExceptionEvent(
            org_id=self.ctx.org_id or (batch.org_id if batch else ""), category=category or "system",
            severity=severity, source_type=source_type, source_id=source_id,
            batch_id=batch.id if batch else "", step_id=step_id, step_index=step_index,
            station_id=station_id, command_id=command_id, alarm_id=alarm_id, message=message[:2000],
            impact=self.impact_of(batch, station_id), never_sent=never_sent, state="open",
        )
        self.db.add(event)
        self.db.flush()
        return event

    def _rules(self) -> list[rules.Rule]:
        return [
            rules.Rule(
                id=row.id, name=row.name, category=row.category, action=row.action, match=row.match or {},
                params=row.params or {}, priority=row.priority, enabled=row.enabled,
            )
            for row in self.db.query(ExceptionRule).filter(ExceptionRule.org_id == self.ctx.org_id).all()
        ]

    def _attempts(self, batch_id: str, step_id: str) -> int:
        return (
            self._query()
            .filter(
                ExceptionEvent.batch_id == batch_id, ExceptionEvent.step_id == step_id,
                ExceptionEvent.auto_action.in_(["retry", "reroute"]),
            )
            .count()
        )

    # ---------- 指令故障 ----------

    def on_command_fault(self, batch: Batch, command: Command, reason: str, delivery: str) -> ExceptionEvent | None:
        """指令被判为故障。先登记，再按策略决定：能自动处理就在同一事务里处理掉。"""
        from ..repositories.execution import DISPATCHING

        if self.db.info.get(_GUARD):
            # 自动处理过程中又冒出的故障（例如改派后转运计划不成立）：只登记，不再嵌套处理
            return self.record(
                category=rules.classify(reason, command_type=command.type, delivery=delivery), message=reason,
                source_type="command", source_id=command.id, batch=batch, step_index=command.step_index,
                station_id=command.station_id, command_id=command.id,
            )
        steps = normalize(batch.recipe_snapshot.get("steps") or [])
        step = steps[command.step_index] if 0 <= command.step_index < len(steps) else {}
        never_sent = delivery == "unreachable" and command.type in DISPATCHING
        category = rules.classify(reason, command_type=command.type, delivery=delivery)
        event = self.record(
            category=category, message=reason, source_type="command", source_id=command.id, batch=batch,
            step_index=command.step_index, station_id=command.station_id, command_id=command.id,
            never_sent=never_sent,
        )
        if command.type not in DISPATCHING:
            event.decision = "保持 / 终止 / 转运指令的故障一律转人工"
            return event
        signal = rules.Signal(
            category=category, batch_id=batch.id, recipe_id=str(batch.recipe_snapshot.get("id") or ""),
            step_id=event.step_id, step_kind=kind_of(step), capability=command.capability,
            station_id=command.station_id, never_sent=never_sent, skippable=bool(step.get("skippable")),
            attempts=self._attempts(batch.id, event.step_id),
        )
        decision = rules.decide(signal, self._rules())
        event.rule_id = decision.rule.id if decision.rule else ""
        event.decision = decision.reason
        if decision.action == "hold":
            return event
        self.db.info[_GUARD] = True
        try:
            if decision.action == "retry":
                ok, result = self._retry(batch, command, float((decision.rule.params or {}).get("delay_sec", 60)))
            elif decision.action == "reroute":
                ok, result = self._reroute(batch, command)
            elif decision.action == "skip":
                ok, result = self._skip(batch, command)
            else:
                ok, result = self._request_reschedule(batch, command, reason)
        finally:
            self.db.info.pop(_GUARD, None)
        event.auto_action = decision.action
        event.auto_result = result
        event.updated_at = now()
        if ok:
            event.state = "auto_resolved"
            event.final_result = result
            event.resolved_at = now()
        self.audit.record(
            None, "异常自动处理" if ok else "异常自动处理未成功", batch.id,
            before=rules.CATEGORIES.get(category, category), after=rules.ACTIONS.get(decision.action, decision.action),
            detail=f"{decision.reason}；{result}", command_id=command.id,
        )
        return event

    def _settle_old(self, command: Command, note: str) -> None:
        """没离开系统的指令：结论就是「未执行」，不再挂在结果未知清单里。"""
        command.state = "not_executed"
        command.error = (command.error + "；" if command.error else "") + note
        command.updated_at = now()

    def _resume_batch(self, batch: Batch, command: Command, run: StepRun | None) -> None:
        from .alarm_service import AlarmService

        if run is not None and run.state in {"unknown", "pending", "failed"}:
            run.state = "ready"
            run.reason = ""
            run.ended_at = None
            run.row_version = int(run.row_version or 0) + 1
        others = [
            row for row in self.db.query(Command).filter(Command.batch_id == batch.id).all()
            if row.id != command.id and row.state in {"unknown", "manual", "partial"}
        ]
        if batch.state == "fault" and not others:
            batch.state = "running"
            batch.held_at = None
            batch.failure_reason = ""
        AlarmService(self.db, self.ctx).resolve_condition(f"command:{command.id}:fault", "异常引擎已自动处理")

    def _run_of(self, command: Command) -> StepRun | None:
        return self.db.get(StepRun, command.step_run_id) if command.step_run_id else None

    def _reissue(self, batch: Batch, command: Command, station_id: str) -> Command:
        from ..repositories.execution import CommandRepository
        from .batch_service import BatchService

        run = self._run_of(command)
        kind = command.type
        if kind == "resume" and run is not None and not CommandRepository(self.db, self.ctx).ever_delivered_for_run(run.id):
            kind = "dispatch"
        return BatchService(self.db, self.ctx).issue_command(
            batch, kind, command.step_index, step_run_id=command.step_run_id, station_id=station_id,
        )

    def _retry(self, batch: Batch, command: Command, delay_sec: float) -> tuple[bool, str]:
        run = self._run_of(command)
        self._settle_old(command, "未送达设备；异常引擎按策略延时重试")
        self._resume_batch(batch, command, run)
        fresh = self._reissue(batch, command, command.station_id)
        if fresh.state != "sent":
            return False, f"重新下发未成立：{fresh.error}"
        earliest = now() + timedelta(seconds=delay_sec)
        fresh.not_before = max(fresh.not_before or earliest, earliest)
        return True, f"{delay_sec:.0f} s 后在 {command.station_id} 重新下发（新指令 {fresh.id[:8]}）"

    def _reroute(self, batch: Batch, command: Command) -> tuple[bool, str]:
        """改派：在具备同样能力、参数范围覆盖、此刻可用的其他工位里取最早能开工的一台，重排这一步的时间窗后重新下发。"""
        from ..domain.scheduling import Interval, SchedulingError, candidate_station_ids, earliest_free
        from ..repositories.resources import StationRepository
        from .schedule_service import ScheduleService, lock_schedule

        steps = normalize(batch.recipe_snapshot.get("steps") or [])
        index = command.step_index
        step = steps[index]
        lock_schedule(self.db)
        schedule = ScheduleService(self.db, self.ctx)
        context = schedule.context({batch.id})
        mine = self.db.query(Allocation).filter(Allocation.batch_id == batch.id).all()
        for row in mine:
            if row.step_index != index:
                context.busy.setdefault(row.station_id, []).append(Interval(row.starts_at, row.ends_at))
        try:
            candidates = [sid for sid in candidate_station_ids(context, step, index) if sid != command.station_id]
        except SchedulingError as error:
            return False, f"没有可改派的工位：{error.message}"
        if not candidates:
            return False, "没有具备同样能力且此刻可用的其他工位"
        current = next((row for row in mine if row.step_index == index and row.kind == "work"), None)
        ready = max(now(), current.starts_at if current else now())
        duration = timedelta(minutes=float(step.get("dur") or 0))
        begin, station_id = min((earliest_free(context, sid, ready, duration), sid) for sid in candidates)
        for row in mine:
            if row.step_index == index:
                self.db.delete(row)
        station = StationRepository(self.db, self.ctx).get(station_id)
        self.db.add(Allocation(
            batch_id=batch.id, step_index=index, station_id=station_id,
            asset_id=station.asset_id if station is not None else "", starts_at=begin, ends_at=begin + duration,
            kind="work",
        ))
        self.db.flush()
        try:
            schedule._refuse_overlaps(batch.id)
        except StateConflict as error:
            return False, f"改派后的时间窗与其他批次重叠：{error}"
        run = self._run_of(command)
        self._settle_old(command, f"未送达设备；异常引擎改派到 {station_id}")
        self._resume_batch(batch, command, run)
        fresh = self._reissue(batch, command, station_id)
        if fresh.state != "sent":
            return False, f"改派到 {station_id} 后下发未成立：{fresh.error}"
        return True, f"第 {index + 1} 步由 {command.station_id} 改派到 {station_id}，{begin:%H:%M} 开工（新指令 {fresh.id[:8]}）"

    def _skip(self, batch: Batch, command: Command) -> tuple[bool, str]:
        from .workflow_service import WorkflowService

        run = self._run_of(command)
        if run is None:
            return False, "找不到对应的步骤实例"
        self._settle_old(command, "未送达设备；异常引擎按策略跳过该步骤")
        self._resume_batch(batch, command, None)
        run.state = "skipped"
        run.ended_at = now()
        run.reason = "异常引擎按策略跳过（指令未送达设备）"
        run.row_version = int(run.row_version or 0) + 1
        workflow = WorkflowService(self.db, self.ctx)
        workflow.release_step_windows(batch, run.step_index)
        self.db.flush()
        workflow._advance(run, batch)
        return True, f"第 {run.step_index + 1} 步已跳过，流程继续"

    def _request_reschedule(self, batch: Batch, command: Command, reason: str) -> tuple[bool, str]:
        """重排建议：批次仍保持在故障，由调度在排程页确认。建议本身由重排服务生成。"""
        from .reschedule_service import RescheduleService

        proposal = RescheduleService(self.db, self.ctx).propose(
            trigger="command_fault", reason=reason, station_id=command.station_id, batch_ids=[batch.id],
        )
        if proposal is None:
            return False, "没有可以重排的未执行步骤"
        return False, f"已生成重排建议 {proposal.id[:8]}，待调度确认；批次保持在故障"

    # ---------- 工位条件 ----------

    def on_station_condition(self, station_id: str, condition: str, message: str, alarm_id: str) -> ExceptionEvent:
        """工位失联 / 心跳超时 / 联锁：登记影响面；策略要求改派时，把这台工位上还没开始的时间窗挪到等价工位。"""
        category = rules.classify_condition(f"station:{station_id}:{condition}", message) or "device_fault"
        event = self.record(
            category=category, message=message, source_type="station", source_id=station_id,
            station_id=station_id, alarm_id=alarm_id, severity=1 if category == "safety" else 2, never_sent=True,
        )
        signal = rules.Signal(category=category, station_id=station_id, never_sent=True)
        decision = rules.decide(signal, self._rules())
        event.rule_id = decision.rule.id if decision.rule else ""
        event.decision = decision.reason
        if decision.action == "reroute":
            moved, stuck = self.reroute_future_windows(station_id)
            event.auto_action = "reroute"
            event.auto_result = (
                f"{len(moved)} 个未开始的时间窗改派到等价工位" + (f"；{len(stuck)} 个找不到可用工位" if stuck else "")
                + ("：" + "；".join(moved[:6]) if moved else "")
            )
            if moved and not stuck:
                event.state = "auto_resolved"
                event.final_result = event.auto_result
                event.resolved_at = now()
        elif decision.action == "reschedule" or (decision.rule is None and category != "safety"):
            # 没有配策略时也生成一份重排建议：建议要调度确认才写入，不会自动挤占任何预约
            from .reschedule_service import RescheduleService

            proposal = RescheduleService(self.db, self.ctx).propose(
                trigger="station_condition", reason=message, station_id=station_id,
                batch_ids=event.impact.get("batches") or [],
            )
            if proposal is not None:
                event.auto_action = "reschedule"
                event.auto_result = (
                    f"已生成重排建议 {proposal.id[:8]}"
                    + ("并自动应用（只涉及未下发批次）" if proposal.state == "applied" else "，待调度确认")
                )
        return event

    def reroute_future_windows(self, station_id: str) -> tuple[list[str], list[str]]:
        """把这台工位上还没开始、步骤也还没开出的时间窗改派到等价工位（时间不变优先，否则最早可用）。"""
        from ..domain.scheduling import Interval, SchedulingError, candidate_station_ids, earliest_free
        from ..repositories.resources import StationRepository
        from .schedule_service import ScheduleService, lock_schedule

        lock_schedule(self.db)
        schedule = ScheduleService(self.db, self.ctx)
        moved: list[str] = []
        stuck: list[str] = []
        rows = (
            self.db.query(Allocation, Batch).join(Batch, Batch.id == Allocation.batch_id)
            .filter(
                Allocation.station_id == station_id, Allocation.kind == "work", Allocation.starts_at > now(),
                Batch.state.in_(["scheduled", "running", "paused"]), Batch.org_id == self.ctx.org_id,
            ).all()
        )
        for allocation, batch in rows:
            steps = normalize(batch.recipe_snapshot.get("steps") or [])
            index = allocation.step_index
            step = steps[index] if index < len(steps) else {}
            opened = self.db.query(StepRun).filter(
                StepRun.batch_id == batch.id, StepRun.step_index == index,
                StepRun.state.notin_(["superseded", "cancelled"]),
            ).count()
            label = f"{batch.id} 第 {index + 1} 步"
            if opened:
                stuck.append(f"{label} 已开出")
                continue
            context = schedule.context({batch.id})
            for row in self.db.query(Allocation).filter(Allocation.batch_id == batch.id).all():
                if row.id != allocation.id:
                    context.busy.setdefault(row.station_id, []).append(Interval(row.starts_at, row.ends_at))
            try:
                candidates = [sid for sid in candidate_station_ids(context, step, index) if sid != station_id]
            except SchedulingError:
                candidates = []
            if not candidates:
                stuck.append(f"{label} 没有等价工位")
                continue
            duration = allocation.ends_at - allocation.starts_at
            begin, target = min((earliest_free(context, sid, allocation.starts_at, duration), sid) for sid in candidates)
            before = f"{allocation.starts_at:%H:%M}"
            allocation.station_id = target
            station = StationRepository(self.db, self.ctx).get(target)
            allocation.asset_id = station.asset_id if station is not None else ""
            allocation.starts_at, allocation.ends_at = begin, begin + duration
            self.db.flush()
            moved.append(f"{label} → {target}（{before} → {begin:%H:%M}）")
            self.audit.record(
                None, "异常改派时间窗", batch.id, before=station_id, after=target,
                detail=f"{station_id} 不可用，第 {index + 1} 步未开始的时间窗改派到 {target}",
            )
        return moved, stuck

    def settle_station(self, station_id: str, detail: str) -> int:
        count = 0
        for event in self._query().filter(
            ExceptionEvent.source_type == "station", ExceptionEvent.station_id == station_id,
            ExceptionEvent.state.in_(list(OPEN)),
        ).all():
            event.state = "resolved"
            event.final_result = detail
            event.resolved_at = now()
            event.updated_at = now()
            count += 1
        return count

    # ---------- 其他来源 ----------

    def on_step_timeout(self, batch: Batch, run: StepRun, action: str, reason: str) -> ExceptionEvent:
        event = self.record(
            category="timeout", message=reason, source_type="step", source_id=run.id, batch=batch,
            step_index=run.step_index, station_id=run.station_id, severity=2 if action != "alarm" else 3,
        )
        event.decision = f"按方法的步骤超时配置：{ {'alarm': '只报警', 'fail': '判为失败', 'skip': '自动跳过'}.get(action, action) }"
        if action == "skip":
            event.auto_action = "skip"
            event.auto_result = "步骤已按方法配置自动跳过"
            event.state = "auto_resolved"
            event.final_result = event.auto_result
            event.resolved_at = now()
        return event

    def on_hold(self, batch: Batch, run: StepRun, source: str, reason: str, alarm_id: str = "") -> ExceptionEvent:
        category = "data" if source == "branch" else "sample"
        event = self.record(
            category=category, message=reason, source_type="step", source_id=run.id, batch=batch,
            step_index=run.step_index, alarm_id=alarm_id,
        )
        event.decision = "判定需要人来做：等 QA 签名选择 / 判定"
        return event

    # ---------- 收尾 ----------

    def settle_batch(self, batch: Batch, final: str, user: User | None = None) -> int:
        """批次恢复 / 跳过 / 重做 / 终止 / 完成：它还开着的异常写上最终结果。"""
        count = 0
        for event in self._query().filter(
            ExceptionEvent.batch_id == batch.id, ExceptionEvent.state.in_(list(OPEN)),
        ).all():
            event.state = "resolved"
            event.final_result = final
            if user is not None and not event.manual_by:
                event.manual_by = user.display_name
            event.resolved_at = now()
            event.updated_at = now()
            count += 1
        return count

    def handle(self, event_id: str, action: str, note: str, user: User) -> dict:
        """人工处理：认领（开始处理）、写明结果后标为已恢复、或关闭（误报 / 不需处理）。"""
        if not self.ctx.has("exception.handle"):
            raise PermissionDenied("当前角色不能处理异常事件（exception.handle）")
        event = self.get(event_id)
        note = (note or "").strip()
        if action not in {"claim", "resolve", "close"}:
            raise ValidationFailed("处理动作只能是 claim（认领）、resolve（已恢复）或 close（关闭）")
        if action in {"resolve", "close"} and not note:
            raise ValidationFailed("标为已恢复或关闭都要写明处理结果")
        if event.state in {"resolved", "closed"}:
            raise StateConflict(f"异常事件已是「{rules.STATES.get(event.state)}」")
        before = rules.STATES.get(event.state, event.state)
        event.manual_by = user.display_name
        event.updated_at = now()
        if action == "claim":
            event.state = "manual"
            event.manual_action = "claim"
            event.manual_note = note or event.manual_note
        else:
            event.state = "resolved" if action == "resolve" else "closed"
            event.manual_action = action
            event.manual_note = note
            event.final_result = note
            event.resolved_at = now()
        self.audit.record(
            user, "处理异常事件", event.id, before=before, after=rules.STATES.get(event.state, event.state),
            detail=f"{rules.CATEGORIES.get(event.category, event.category)}：{note or '认领'}",
        )
        self.db.commit()
        return self.out(event)

    # ---------- 策略库 ----------

    def rules_out(self) -> list[dict]:
        rows = self.db.query(ExceptionRule).filter(ExceptionRule.org_id == self.ctx.org_id).order_by(
            ExceptionRule.priority, ExceptionRule.id,
        ).all()
        return [self.rule_out(row) for row in rows]

    @staticmethod
    def rule_out(row: ExceptionRule) -> dict:
        return {
            "id": row.id, "name": row.name, "category": row.category,
            "category_label": rules.CATEGORIES.get(row.category, row.category),
            "match": row.match or {}, "action": row.action, "action_label": rules.ACTIONS.get(row.action, row.action),
            "params": row.params or {}, "priority": row.priority, "enabled": row.enabled, "note": row.note,
            "updated_at": row.updated_at.isoformat(timespec="seconds") if row.updated_at else None,
            "row_version": row.row_version,
        }

    def save_rule(self, payload: dict, user: User, rule_id: str = "") -> dict:
        if not self.ctx.has("exception.rules"):
            raise PermissionDenied("当前角色不能维护异常策略（exception.rules）")
        issues = rules.rule_issues(payload)
        if issues:
            raise ValidationFailed("策略配置不成立", {"blocked": [{"key": "rule", "label": text} for text in issues]})
        if rule_id:
            row = self.db.query(ExceptionRule).filter(
                ExceptionRule.id == rule_id, ExceptionRule.org_id == self.ctx.org_id,
            ).first()
            if row is None:
                raise NotFound("策略不存在")
            expected = payload.get("row_version")
            if expected is not None and int(expected) != int(row.row_version or 1):
                raise StateConflict("策略已被别人修改，请刷新后再改", code="version_conflict")
            row.row_version = int(row.row_version or 1) + 1
        else:
            row = ExceptionRule(org_id=self.ctx.org_id, created_by=user.id)
            self.db.add(row)
        for key in ("name", "category", "action", "match", "params", "priority", "enabled", "note"):
            if key in payload and payload[key] is not None:
                setattr(row, key, payload[key])
        row.updated_at = now()
        self.db.flush()
        self.audit.record(
            user, "维护异常策略", row.id, after=f"{rules.CATEGORIES.get(row.category)} → {rules.ACTIONS.get(row.action)}",
            detail=f"{row.name}；{'启用' if row.enabled else '停用'}；优先级 {row.priority}",
        )
        self.db.commit()
        return self.rule_out(row)
