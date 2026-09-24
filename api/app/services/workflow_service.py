"""流程推进。

设备回执只完成对应 StepRun；下一节点由推进器决定。状态转换、事件处理标记、
下一个待办或命令在同一个短事务里提交——否则崩在中间就会出现「步骤完成了但没有下一步」
或者「下一步建了两遍」。

并发由三层挡住：事件唯一键（同一事件只处理一次）、步骤行版本 + 行锁（同一步只转换一次）、
下一节点创建唯一约束（不同事件也不会把同一步推进两次）。
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..core.clock import now
from ..core.config import settings
from ..core.context import AccessContext, system_context
from ..core.errors import (
    DomainError, NotFound, PermissionDenied, StateConflict, ValidationFailed,
)
from ..models.base import uid as uid_hex
from ..domain import graph, workflow
from ..domain.access import same_person
from ..domain.steps import (
    BRANCH, DEVICE, GATE, MANUAL, REVIEW, SPLIT, WAIT, KIND_NAMES, TIMEOUT_ACTIONS, branch_cases, branch_config,
    case_label, kind_of, match_case, missing_form_values, normalize, step_id_of,
)
from ..domain.permissions import ADMIN, ROLE_NAMES
from ..models import Batch, BatchSignal, Sample, StepRun, User, WorkflowEvent, roles_of
from ..repositories.batches import BatchRepository, SampleRepository
from ..repositories.execution import CommandRepository
from ..repositories.governance import UserRepository
from ..repositories.materials import ReservationRepository
from ..repositories.resources import CapabilityRepository
from ..repositories.workflow import (
    BatchSignalRepository, StepAdvanceRepository, StepRunRepository, WorkflowEventRepository,
)
from .audit_service import AuditService
from .identity_service import IdentityService, admin_self_approval


class WorkflowService:
    def __init__(self, db: Session, ctx: AccessContext):
        self.db = db
        self.ctx = ctx
        self.runs = StepRunRepository(db, ctx)
        self.events = WorkflowEventRepository(db, ctx)
        self.advances = StepAdvanceRepository(db)
        self.signals = BatchSignalRepository(db, ctx)
        self.batches = BatchRepository(db, ctx)
        self.samples = SampleRepository(db, ctx)
        self.reservations = ReservationRepository(db, ctx)
        self.commands = CommandRepository(db, ctx)
        self.capabilities = CapabilityRepository(db)
        self.users = UserRepository(db)
        self.audit = AuditService(db, ctx)
        self.identity = IdentityService(db, ctx)

    # ---------- 步骤实例 ----------

    def steps_of(self, batch: Batch) -> list[dict]:
        return normalize(batch.recipe_snapshot.get("steps") or [])

    def start(self, batch: Batch, assignee_user_id: str = "") -> list[dict]:
        """下发时开出起点并进入它们。

        顺序流程只有第一步；依赖图里所有没有前驱的步骤都是起点，一起开出——以前只开第一行，
        其余起点要等第一步做完才开，平白串行。设备起点照常下指令（同一块板上的并行设备步骤
        仍按载具互斥一个一个来）。
        """
        steps = self.steps_of(batch)
        if not steps:
            raise StateConflict("方法没有步骤，无法开跑")
        indices = graph.frontier(steps, {}, {})[0] if graph.graph_mode(steps) else [0]
        entered: list[dict] = []
        for index in indices:
            step_id = step_id_of(steps[index], index)
            if graph.graph_mode(steps) and not self.advances.claim(batch.id, f"join:{step_id}", 1, step_id):
                continue
            run = self.open_step(batch, index, assignee_user_id)
            entered.append({**self.enter(batch, run, index), "run": run})
        self._sync_current_step(batch)
        return entered

    def open_step(self, batch: Batch, index: int, assignee_user_id: str = "") -> StepRun:
        """建立一个步骤实例。同一步的重试用 attempt 区分，旧记录保留。"""
        steps = self.steps_of(batch)
        step = steps[index]
        step_id = step_id_of(step, index)
        attempt = self.runs.attempts(batch.id, step_id) + 1
        run = StepRun(
            org_id=batch.org_id or self.ctx.org_id, batch_id=batch.id, step_id=step_id,
            step_index=index, kind=kind_of(step), attempt=attempt,
            state=workflow.initial_state(step), step_snapshot=step,
            assignee_user_id=assignee_user_id, started_at=now(),
        )
        if run.kind == WAIT:
            wait_for = step.get("wait_for") or {}
            if (wait_for.get("mode") or "duration") == "duration":
                run.due_at = now() + timedelta(minutes=float(step.get("dur") or 0))
        if run.kind == MANUAL and step.get("dur"):
            run.due_at = now() + timedelta(minutes=float(step["dur"]))
        timeout = step.get("timeout") or {}
        if isinstance(timeout, dict) and isinstance(timeout.get("minutes"), (int, float)) and timeout["minutes"] > 0:
            run.deadline_at = now() + timedelta(minutes=float(timeout["minutes"]))
        self.runs.add(run)
        batch.current_step = index
        if run.kind == WAIT and ((step.get("wait_for") or {}).get("mode") == "event"):
            self._consume_early_signal(batch, run, step)
        return run

    def _consume_early_signal(self, batch: Batch, run: StepRun, step: dict) -> None:
        """事件等待开出时，先看有没有早到的同名信号：有就直接消费，不让它空等。"""
        name = (step.get("wait_for") or {}).get("event") or ""
        self.db.flush()
        signal = self.signals.unconsumed(batch.id, name)
        if signal is None:
            return
        self._bind_signal(batch, run, signal)

    # ---------- 事件 ----------

    def emit(
        self, batch_id: str, step_run_id: str, event_type: str, event_key: str,
        payload: dict | None = None, available_at=None, org_id: str = "",
    ) -> WorkflowEvent:
        """持久化一个推进事件。同 key 重复直接返回已有行，不重复处理。"""
        existing = self.events.find_key_any_org(event_key)
        if existing is not None:
            return existing
        event = WorkflowEvent(
            org_id=org_id or self.ctx.org_id, batch_id=batch_id, step_run_id=step_run_id,
            event_type=event_type, event_key=event_key, payload=payload or {},
            available_at=available_at or now(),
        )
        try:
            # 保存点：唯一键冲突只撤销这一行，不连带回滚调用方同一事务里的人工记录与签名
            with self.db.begin_nested():
                self.db.add(event)
                self.db.flush()
        except IntegrityError:
            found = self.events.find_key_any_org(event_key)
            if found is None:
                raise
            return found
        return event

    # ---------- 人工步骤 ----------

    def submit_manual(self, step_run_id: str, payload: dict, user: User) -> dict:
        run = self.runs.lock(step_run_id)
        if run is None:
            raise NotFound("步骤实例不存在")
        if run.kind != MANUAL:
            raise StateConflict(f"该步骤是{KIND_NAMES.get(run.kind, run.kind)}步骤，不接受人工提交")
        if run.state in workflow.TERMINAL_STATES:
            raise StateConflict(f"步骤已是终态 {run.state}，不能再提交")
        self.runs.check_version(run, payload.get("row_version"), "步骤实例")
        batch = self.batches.get(run.batch_id)
        if batch is None:
            raise NotFound("批次不存在")
        if workflow.hold_blocks_device_action(batch.state) and batch.state != "paused":
            raise StateConflict(f"批次状态为 {batch.state}，人工提交已停止")

        step = run.step_snapshot or {}
        values = payload.get("form_data") or {}
        problems = missing_form_values(step, values)
        # 样本与物料核对：声明了就必须勾，不能只在界面上打个对勾
        checks = payload.get("checks") or {}
        if step.get("requires_sample_check", True) and not checks.get("samples"):
            problems.append("未确认样本核对")
        needs_material = bool((batch.recipe_snapshot.get("bom") or []))
        if needs_material and not checks.get("materials"):
            problems.append("未确认物料核对")
        if problems:
            raise StateConflict(
                "人工记录不完整，步骤未推进",
                {"blocked": [{"key": "form", "label": p} for p in problems]},
                code="manual_record_incomplete",
            )
        signature = None
        if step.get("requires_signature"):
            signature = self.identity.consume_signature(
                payload.get("signature_id"), user, f"人工步骤提交：{step.get('name')}",
                object_ref=run.id, object_version=run.row_version,
            )

        run.form_data = {"values": values, "checks": checks, "note": payload.get("note", "")}
        run.submitted_by = user.id
        self.runs.bump(run)
        event = self.emit(
            batch.id, run.id, "manual_submit",
            f"manual:{run.id}:{run.attempt}",
            {"submitted_by": user.id, "signature_id": signature.id if signature else ""},
        )
        self.audit.record(
            user, "提交人工步骤记录", batch.id, sign=bool(signature),
            meaning=signature.meaning if signature else "",
            signature_id=signature.id if signature else "",
            before=run.state, after="待推进", object_version=run.row_version,
            detail=f"{step.get('name')}（第 {run.step_index + 1} 步，第 {run.attempt} 次）；{len(values)} 个字段",
        )
        self.db.commit()
        # 立刻推进一次，不用等下一个轮询周期
        result = self.process_event(event.id)
        return {"step_run": self.run_out(run), "advance": result}

    # ---------- 审核步骤 ----------

    def decide_review(self, step_run_id: str, payload: dict, user: User) -> dict:
        run = self.runs.lock(step_run_id)
        if run is None:
            raise NotFound("步骤实例不存在")
        if run.kind != REVIEW:
            raise StateConflict("该步骤不是审核节点")
        if run.state in workflow.TERMINAL_STATES:
            raise StateConflict(f"审核步骤已是终态 {run.state}")
        self.runs.check_version(run, payload.get("row_version"), "步骤实例")
        conclusion = payload.get("conclusion")
        if conclusion not in {"approved", "rejected"}:
            raise ValidationFailed("审核结论只能是 approved 或 rejected；不接受任意目标状态")
        reason = (payload.get("reason") or "").strip()
        if conclusion == "rejected" and not reason:
            raise ValidationFailed("退回必须写明理由")
        step = run.step_snapshot or {}
        required_role = step.get("review_role") or "qa"
        if required_role not in roles_of(user) and ADMIN not in roles_of(user):
            raise PermissionDenied(f"该审核节点要求 {ROLE_NAMES.get(required_role, required_role)} 角色")
        # 审核本人录入或编写的内容必须被拒
        previous = [
            row for row in self.runs.for_batch(run.batch_id)
            if row.step_index < run.step_index and row.submitted_by
        ]
        if any(same_person(row.submitted_by, user.id) for row in previous) and not admin_self_approval(
            self.db, self.ctx, user, run.id, "审核本人提交的上游人工记录",
        ):
            raise PermissionDenied(
                "不能审核本人提交的上游人工记录（职责分离）", code="self_review_denied"
            )
        signature = self.identity.consume_signature(
            payload.get("signature_id"), user, f"流程审核：{conclusion}",
            object_ref=run.id, object_version=run.row_version,
        )
        run.conclusion = conclusion
        run.reason = reason
        run.reviewed_by = user.id
        self.runs.bump(run)
        event = self.emit(
            run.batch_id, run.id, "review_decision", f"review:{run.id}:{run.attempt}",
            {"conclusion": conclusion, "reviewer": user.id, "reason": reason},
        )
        self.audit.record(
            user, "流程审核决定", run.batch_id, sign=True, meaning=signature.meaning,
            signature_id=signature.id, before="待审核",
            after="通过" if conclusion == "approved" else "退回",
            object_version=run.row_version, detail=f"{step.get('name')}；{reason or '无附加说明'}",
        )
        self.db.commit()
        result = self.process_event(event.id)
        return {"step_run": self.run_out(run), "advance": result}

    # ---------- 设备回执 ----------

    def device_ack(
        self, batch_id: str, step_run_id: str, command_id: str, outcome: str, payload: dict,
        org_id: str = "",
    ) -> WorkflowEvent:
        """设备回执入事件表。必须绑定原 command_id。"""
        if not command_id:
            raise ValidationFailed("设备回执必须绑定原 command_id", code="command_id_required")
        return self.emit(
            batch_id, step_run_id, "device_ack", f"device:{command_id}:{outcome}",
            {"command_id": command_id, "outcome": outcome, **payload}, org_id=org_id,
        )

    # ---------- 推进器 ----------

    def process_event(self, event_id: str) -> dict:
        """处理一个事件。整体成功或整体失败，不留半条完成任务。

        业务性拒绝（状态机不允许、重复事件）是确定结论，直接判为 rejected；其他错误
        （数据库抖动、连接中断）按指数退避重排，超过次数才判失败并对批次报警——
        一次瞬时错误不能把等待节点永久卡住。
        """
        event = self.db.get(WorkflowEvent, event_id)
        if event is None:
            return {"processed": False, "reason": "事件不存在"}
        if event.state == "processed":
            return {"processed": False, "reason": "事件已处理", "replayed": True}
        run = self.runs.lock(event.step_run_id) if event.step_run_id else None
        batch = self.db.get(Batch, event.batch_id) if event.batch_id else None
        if run is None or batch is None:
            event.state = "rejected"
            event.error = "事件缺少对应的步骤实例或批次"
            event.processed_at = now()
            self.db.commit()
            return {"processed": False, "reason": event.error}
        # 取到步骤行锁后再看一次：同步调用与后台推进器可能同时拿着同一个事件
        self.db.refresh(event)
        if event.state == "processed":
            return {"processed": False, "reason": "事件已处理", "replayed": True}

        try:
            # 保存点：失败只撤销推进本身，调用方同一事务里的人工记录、签名消费、审计保留
            with self.db.begin_nested():
                outcome = self._apply(event, run, batch)
        except DomainError as exc:
            return self._reject_event(event, run, batch, str(exc))
        except Exception as exc:
            return self._retry_event(event, run, batch, exc)
        # 状态转换、事件标记与下一节点在同一事务里提交
        event.state = "processed"
        event.processed_at = now()
        event.error = ""
        self.db.commit()
        return {"processed": True, **outcome}

    def _reject_event(self, event: WorkflowEvent, run: StepRun, batch: Batch, reason: str) -> dict:
        event.state = "rejected"
        event.error = reason[:500]
        event.processed_at = now()
        if run.state in workflow.OPEN_STATES and batch.state not in {"done", "aborted"}:
            # 步骤还开着却推不动：批次会停在这里，必须让人知道
            self._stuck_alarm(batch, run, f"推进事件被拒：{reason}")
        self.db.commit()
        return {"processed": False, "reason": reason}

    def _retry_event(self, event: WorkflowEvent, run: StepRun, batch: Batch, exc: Exception) -> dict:
        reason = f"{exc.__class__.__name__}: {exc}"[:500]
        attempts = int(event.attempts or 0) + 1
        event.attempts = attempts
        event.error = reason
        event.claimed_at = None
        event.claimed_by = ""
        if attempts >= settings.advance_max_attempts:
            return self._reject_event(event, run, batch, f"重试 {attempts} 次仍失败：{reason}")
        delay = min(
            settings.advance_retry_max_sec, settings.advance_retry_base_sec * (2 ** (attempts - 1))
        )
        event.state = "pending"
        event.available_at = now() + timedelta(seconds=delay)
        self.db.commit()
        return {"processed": False, "reason": reason, "retry_in_sec": delay, "attempts": attempts}

    def _stuck_alarm(self, batch: Batch, run: StepRun, message: str) -> None:
        from .alarm_service import AlarmService

        AlarmService(self.db, self.ctx).raise_alarm(
            severity=2, source_type="batch", source_id=batch.id,
            message=f"第 {run.step_index + 1} 步{KIND_NAMES.get(run.kind, run.kind)}节点无法推进：{message}"[:500],
            response="核对批次事件与步骤状态；排除原因后由操作员走恢复评估。", owner="操作员",
            origin="system", condition_key=f"step:{run.id}:stuck",
        )

    def _apply(self, event: WorkflowEvent, run: StepRun, batch: Batch) -> dict:
        timeout_target = {"fail": workflow.FAILED, "skip": workflow.SKIPPED}
        target = {
            "manual_submit": workflow.COMPLETED,
            "wait_due": workflow.COMPLETED,
            "signal": workflow.COMPLETED,
            "timeout": timeout_target.get(event.payload.get("action", "")),
            "device_ack": (
                workflow.COMPLETED if event.payload.get("outcome") == "done" else workflow.FAILED
            ),
            "review_decision": (
                workflow.COMPLETED if event.payload.get("conclusion") == "approved"
                else workflow.FAILED
            ),
            "cancel": workflow.CANCELLED,
        }.get(event.event_type)
        if target is None:
            raise StateConflict(f"未知事件类型 {event.event_type}")
        if not workflow.can_transition(run.kind, run.state, target):
            # 同一步的第二个事件到这里就停：状态机不允许重复转换
            raise StateConflict(
                f"{KIND_NAMES.get(run.kind, run.kind)}步骤从 {run.state} 不能转到 {target}"
            )
        run.state = target
        run.ended_at = now()
        run.row_version = int(run.row_version or 0) + 1
        if event.event_type == "timeout":
            run.reason = event.payload.get("reason") or "步骤超时"
        if run.timed_out_at is not None and event.event_type != "timeout":
            from .alarm_service import AlarmService

            AlarmService(self.db, self.ctx).resolve_condition(
                f"step:{run.id}:timeout", f"第 {run.step_index + 1} 步已给出结论 {target}",
            )

        if target == workflow.FAILED and run.kind == REVIEW:
            return self._handle_review_rejection(run, batch)
        if target in {workflow.FAILED, workflow.CANCELLED}:
            batch.state = "fault" if target == workflow.FAILED else batch.state
            batch.failure_reason = event.payload.get("reason") or "步骤失败"
            if target == workflow.FAILED:
                # 并行分支时恢复评估针对出问题的这一步
                batch.current_step = run.step_index
                batch.held_at = batch.held_at or now()
            return {"batch_state": batch.state, "next": None}
        if target == workflow.SKIPPED:
            self.release_step_windows(batch, run.step_index)
        return self._advance(run, batch)

    def _advance(self, run: StepRun, batch: Batch) -> dict:
        if batch.state in {"aborting", "aborted", "done"}:
            # 终止中或已结束的批次不再开下一节点；迟到的完成事件只记录步骤本身
            return {"next": None, "batch_state": batch.state, "stopped": True}
        steps = self.steps_of(batch)
        if graph.graph_mode(steps):
            return self._advance_graph(run, batch, steps)
        index, next_step_id = self._next_open_step(steps, batch, run.step_index)
        if index is None:
            if batch.state != "running":
                # 保持 / 故障中走完了最后一步：批次仍在操作员控制下，不能自己变成「已完成」，
                # 恢复评估确认后才结束
                batch.current_step = run.step_index
                self.audit.record(
                    None, "流程节点已全部完成", batch.id,
                    before=batch.state, after=batch.state,
                    detail="批次处于保持或故障，恢复评估确认后才结束",
                )
                return {"next": None, "batch_state": batch.state, "awaiting_recovery": True}
            # 领取「结束」也要去重，否则两个事件会各写一次批次完成
            if not self.advances.claim(batch.id, run.step_id, run.attempt, "__end__"):
                return {"next": None, "duplicate": True}
            batch.current_step = run.step_index
            self.finish_batch(batch)
            return {"next": None, "batch_state": "done"}
        if not self.advances.claim(batch.id, run.step_id, run.attempt, next_step_id or ""):
            return {"next": None, "duplicate": True}
        next_run = self.open_step(batch, index)
        return self.enter(batch, next_run, index)

    def _advance_graph(self, run: StepRun, batch: Batch, steps: list[dict]) -> dict:
        """依赖图模式的推进：按「死路剪除」判定每一步（见 `domain/graph.py`）。

        - 分叉：一步完成可以同时开出多个后继（设备动作与人工记录、等待并行）。
        - 汇合：后继要等它的全部入边都有结论；并行分支上的汇合等全部前驱，条件分支后的汇合
          只等真正走到的那条路。
        - 没走到的分支上的步骤记为「未走此分支」，它们预约的工位时间窗随即归还。
        - 每个后继的开出（或剪除）各自经 `StepAdvance` 去重：两个前驱几乎同时完成时只处理一次。
        - 全部步骤都有结论（完成 / 跳过 / 未走）才结束批次。
        """
        # 两个分支在不同事务里几乎同时完成时，各自都可能看不到对方的完成而谁也不开汇合步骤。
        # 先取批次行锁再重读步骤实例：后到的事务等前一个提交，读到的是它提交后的状态。
        self.db.flush()
        self.batches.lock(batch.id)
        rows = self._fresh_rows(batch.id)
        self._release_waiting_devices(batch, rows, finished=run)
        status, chosen = self.flow_state(rows)
        if graph.all_resolved(steps, status):
            return self._finish_graph(run, batch)
        to_open, to_prune = graph.frontier(steps, status, chosen)
        pruned = self._prune(batch, steps, rows, to_prune)
        opened: list[dict] = []
        for index in to_open:
            step_id = step_id_of(steps[index], index)
            # 去重键用「汇合步骤本身」而不是触发它的前驱：两个前驱同时完成也只开一次
            if not self.advances.claim(batch.id, f"join:{step_id}", self._join_epoch(rows, step_id), step_id):
                continue
            next_run = self.open_step(batch, index)
            opened.append(self.enter(batch, next_run, index))
        self._sync_current_step(batch)
        if pruned and not opened:
            # 剪掉的是最后一段路：剩下的步骤都有了结论，批次就此结束
            rows = self._fresh_rows(batch.id)
            status, _ = self.flow_state(rows)
            if graph.all_resolved(steps, status):
                return self._finish_graph(run, batch)
        if not opened:
            waiting = [
                step_id_of(steps[index], index) for index in range(len(steps))
                if status.get(step_id_of(steps[index], index)) not in graph.RESOLVED
                and index not in to_prune
            ]
            return {"next": None, "batch_state": batch.state, "waiting_for": waiting, "pruned": pruned}
        first = opened[0]
        return {**first, "opened": [row.get("next") for row in opened], "pruned": pruned}

    def _fresh_rows(self, batch_id: str) -> list[StepRun]:
        return list(
            self.db.query(StepRun).filter(StepRun.batch_id == batch_id)
            .order_by(StepRun.step_index, StepRun.attempt).populate_existing().all()
        )

    @staticmethod
    def flow_state(rows: list[StepRun]) -> tuple[dict[str, str], dict[str, str]]:
        """每一步最新一次有效实例的状态，以及已完成分支选中的出口。作废与取消的记录不算。"""
        latest: dict[str, StepRun] = {}
        for row in sorted(rows, key=lambda r: (r.step_index, r.attempt)):
            if row.state in workflow.VOID_STATES:
                continue
            latest[row.step_id] = row
        status = {step_id: row.state for step_id, row in latest.items()}
        chosen = {
            step_id: row.conclusion for step_id, row in latest.items()
            if row.kind == BRANCH and row.state == workflow.COMPLETED and row.conclusion
        }
        return status, chosen

    def _finish_graph(self, run: StepRun, batch: Batch) -> dict:
        if batch.state != "running":
            self.audit.record(
                None, "流程节点已全部完成", batch.id, before=batch.state, after=batch.state,
                detail="批次处于保持或故障，恢复评估确认后才结束",
            )
            return {"next": None, "batch_state": batch.state, "awaiting_recovery": True}
        if not self.advances.claim(batch.id, run.step_id, run.attempt, "__end__"):
            return {"next": None, "duplicate": True}
        self.finish_batch(batch)
        return {"next": None, "batch_state": "done"}

    def _prune(self, batch: Batch, steps: list[dict], rows: list[StepRun], indices: list[int]) -> list[str]:
        """把没走到的分支上的步骤记为「未走此分支」，归还它们的工位时间窗。"""
        pruned: list[str] = []
        for index in indices:
            step = steps[index]
            step_id = step_id_of(step, index)
            if not self.advances.claim(batch.id, f"join:{step_id}", self._join_epoch(rows, step_id), f"prune:{step_id}"):
                continue
            self.runs.add(StepRun(
                org_id=batch.org_id or self.ctx.org_id, batch_id=batch.id, step_id=step_id,
                step_index=index, kind=kind_of(step), attempt=self.runs.attempts(batch.id, step_id) + 1,
                state=workflow.NOT_TAKEN, step_snapshot=step, started_at=now(), ended_at=now(),
                reason="条件分支没有走到这条路径",
            ))
            self.release_step_windows(batch, index)
            pruned.append(step_id)
        if pruned:
            self.db.flush()
            self.audit.record(
                None, "剪除未走分支", batch.id, after=f"{len(pruned)} 步未走",
                detail="、".join(pruned) + "：所在路径的分支出口没有被选中，预约的工位时间窗已归还",
            )
        return pruned

    def release_step_windows(self, batch: Batch, step_index: int) -> int:
        """这一步不会执行了（跳过 / 未走此分支）：它还没开始的时间窗还给排程。"""
        from ..models import Allocation

        released = 0
        for allocation in self.db.query(Allocation).filter(
            Allocation.batch_id == batch.id, Allocation.step_index == step_index,
        ).all():
            if allocation.ends_at <= now():
                continue
            self.db.delete(allocation)
            released += 1
        return released

    def _plate_busy(self, batch: Batch, run: StepRun, rows: list[StepRun] | None = None) -> bool:
        """批次绑定了载具时，同一时刻只能有一个设备步骤在用它：一块板不能同时在两台设备上。"""
        from .transfer_service import TransferService

        if TransferService(self.db, self.ctx).for_batch(batch.id) is None:
            return False
        rows = rows if rows is not None else self.runs.for_batch(batch.id)
        return any(
            row.id != run.id and row.kind == DEVICE and row.state in {workflow.READY, workflow.RUNNING}
            for row in rows
        )

    def _release_waiting_devices(self, batch: Batch, rows: list[StepRun], finished: StepRun) -> None:
        """设备步骤结束后，把因载具被占而等着的并行设备步骤开起来（按步骤顺序一次一个）。"""
        if finished.kind != DEVICE or batch.state != "running":
            return
        waiting = [row for row in rows if row.kind == DEVICE and row.state == workflow.PENDING]
        for row in waiting:
            if self._plate_busy(batch, row, rows):
                break
            row.state = workflow.READY
            row.row_version = int(row.row_version or 0) + 1
            from .batch_service import BatchService

            BatchService(self.db, self.ctx).issue_command(batch, "dispatch", row.step_index, step_run_id=row.id)
            self.audit.record(
                None, "并行设备步骤开始", batch.id, detail=f"第 {row.step_index + 1} 步：载具已空出，开始投递",
            )
            break

    @staticmethod
    def _join_epoch(rows: list[StepRun], step_id: str) -> int:
        """同一步第几次被开出：返工作废后重新开出时去重键要变，否则再也开不出来。"""
        return 1 + sum(1 for row in rows if row.step_id == step_id)

    def _sync_current_step(self, batch: Batch) -> None:
        """并行时「当前步骤」取仍开着的最靠前一步；保持、恢复、界面都读它。"""
        open_rows = self.runs.open_runs(batch.id)
        if open_rows:
            batch.current_step = min(row.step_index for row in open_rows)

    def enter(self, batch: Batch, run: StepRun, index: int) -> dict:
        """一个步骤实例开出来之后做什么。

        设备步骤下指令；质检关卡与样本拆分由系统立即判定 / 执行，不等人也不等设备；
        人工、等待、审核只留待办，不创建假适配器。
        """
        if run.kind == GATE:
            return self._evaluate_gate(batch, run, index)
        if run.kind == SPLIT:
            return self._split_samples(batch, run)
        if run.kind == BRANCH:
            return self._evaluate_branch(batch, run, index)
        command_id = ""
        if run.kind == DEVICE:
            if workflow.hold_blocks_device_action(batch.state):
                run.state = workflow.PENDING
                return {"next": self.run_out(run), "device_blocked": True}
            if graph.graph_mode(self.steps_of(batch)) and self._plate_busy(batch, run):
                # 并行分支上的另一个设备步骤正在用这块板：等它结束再投递，不把板从设备上抢走
                run.state = workflow.PENDING
                run.reason = "等待载具：并行分支的设备步骤正在使用"
                return {"next": self.run_out(run), "waiting_labware": True}
            from .batch_service import BatchService

            command = BatchService(self.db, self.ctx).issue_command(
                batch, "dispatch", index, step_run_id=run.id
            )
            command_id = command.id
        return {
            "next": self.run_out(run),
            "command_id": command_id,
            "batch_state": batch.state,
        }

    # ---------- 质检关卡 ----------

    def _active_samples(self, batch: Batch) -> list[Sample]:
        return [s for s in self.samples.for_batch(batch.id) if s.state not in {"failed", "split"}]

    def _evaluate_gate(self, batch: Batch, run: StepRun, index: int) -> dict:
        """读测量来源步骤最近一次检查点里的测量值，按阈值判定。

        取不到数值不等于合格：一律转人工判断。逐孔位判定时不合格的样本单独剔除，
        其余样本继续；全部不合格才按关卡的不合格去向处理整批。
        """
        from ..repositories.execution import CheckpointRepository

        gate = (run.step_snapshot or {}).get("gate") or {}
        steps = self.steps_of(batch)
        ids = [step_id_of(step, position) for position, step in enumerate(steps)]
        source = gate.get("source_step_id")
        checkpoint = (
            CheckpointRepository(self.db).latest_for_step(batch.id, ids.index(source))
            if source in ids else None
        )
        delivered = ((checkpoint.payload or {}).get("delivered") or {}) if checkpoint else {}
        field = gate.get("field")
        low, high = gate.get("min"), gate.get("max")
        name = (run.step_snapshot or {}).get("name") or "质检关卡"
        run.started_at = run.started_at or now()

        if gate.get("scope") == "sample":
            wells = delivered.get("wells") or {}
            values, failed, undecided = {}, [], []
            for sample in self._active_samples(batch):
                value = (wells.get(sample.well) or {}).get(field)
                values[sample.well] = value
                verdict = workflow.judge(value, low, high)
                if verdict is True:
                    continue
                (undecided if verdict is None else failed).append(sample.well)
                sample.state = "failed"
                sample.flag_note = (
                    f"质检关卡「{name}」{'无测量值，无法判定' if verdict is None else f'不合格：{field}={value}'}"
                )
            run.form_data = {"field": field, "min": low, "max": high, "scope": "sample",
                             "values": values, "failed": failed, "undecided": undecided,
                             "checkpoint_id": checkpoint.id if checkpoint else ""}
            if len(failed) + len(undecided) < len(values):
                self._close_run(run, workflow.COMPLETED, (
                    f"{len(values) - len(failed) - len(undecided)} 个样本合格；"
                    f"剔除不合格 {len(failed)} 个、无法判定 {len(undecided)} 个"
                ))
                self.audit.record(None, "质检关卡判定", batch.id, before="待判定", after="合格（部分剔除）",
                                  detail=f"{name}：{run.reason}")
                return self._advance(run, batch)
            return self._gate_failed(batch, run, index, gate, f"全部 {len(values)} 个样本不合格或无法判定")

        value = delivered.get(field)
        verdict = workflow.judge(value, low, high)
        limits = "，".join(part for part in (
            f"下限 {low}" if low is not None else "", f"上限 {high}" if high is not None else "",
        ) if part)
        run.form_data = {"field": field, "min": low, "max": high, "scope": "batch", "value": value,
                         "checkpoint_id": checkpoint.id if checkpoint else ""}
        if verdict is True:
            self._close_run(run, workflow.COMPLETED, f"{field}={value} 合格")
            self.audit.record(None, "质检关卡判定", batch.id, before="待判定", after="合格",
                              detail=f"{name}：{field}={value}（{limits}）")
            return self._advance(run, batch)
        if verdict is None:
            return self._gate_hold(batch, run, f"测量来源没有 {field} 的数值，无法判定")
        return self._gate_failed(batch, run, index, gate, f"{field}={value} 超出范围（{limits}）")

    def _close_run(self, run: StepRun, state: str, reason: str) -> None:
        run.state = state
        run.reason = reason
        run.ended_at = now()
        run.row_version = int(run.row_version or 0) + 1

    def _gate_failed(self, batch: Batch, run: StepRun, index: int, gate: dict, reason: str) -> dict:
        name = (run.step_snapshot or {}).get("name") or "质检关卡"
        on_fail = gate.get("on_fail")
        if on_fail == "rework":
            rounds = len([r for r in self.runs.for_batch(batch.id)
                          if r.step_id == run.step_id and r.state == workflow.FAILED]) + 1
            if rounds <= int(gate.get("max_rework") or 0):
                self._close_run(run, workflow.FAILED, f"{reason}；第 {rounds} 次返工")
                return self._rework(batch, run, index, gate, rounds)
            return self._gate_hold(batch, run, f"{reason}；已返工 {rounds - 1} 次仍不合格，转人工判断")
        if on_fail == "scrap":
            self._close_run(run, workflow.FAILED, f"{reason}；按方法报废")
            return self._gate_scrap(batch, run, reason)
        return self._gate_hold(batch, run, reason)

    def _rework(self, batch: Batch, run: StepRun, index: int, gate: dict, rounds: int) -> dict:
        """返工：从返工目标到关卡之间已完成的步骤作废（记录保留），流程从返工目标重做。"""
        steps = self.steps_of(batch)
        ids = [step_id_of(step, position) for position, step in enumerate(steps)]
        target = ids.index(gate["rework_to"])
        if not self.advances.claim(batch.id, run.step_id, run.attempt, f"rework:{gate['rework_to']}"):
            return {"next": None, "duplicate": True}
        for row in self.runs.for_batch(batch.id):
            if target <= row.step_index < index and row.state == workflow.COMPLETED:
                row.state = workflow.SUPERSEDED
                row.reason = f"质检关卡第 {rounds} 次返工，本次结论作废"
                row.row_version = int(row.row_version or 0) + 1
                self._retire_split_children(row)
        new_run = self.open_step(batch, target)
        self.audit.record(
            None, "质检不合格返工", batch.id, before=run.reason,
            after=f"回到第 {target + 1} 步（第 {new_run.attempt} 次）",
            detail=f"{(run.step_snapshot or {}).get('name')}：第 {rounds} 次返工；原记录保留，标为已被返工取代",
        )
        return {**self.enter(batch, new_run, target), "rework": rounds}

    def _gate_scrap(self, batch: Batch, run: StepRun, reason: str) -> dict:
        for sample in self._active_samples(batch):
            sample.state = "failed"
            sample.flag_note = f"质检关卡报废：{reason}"
        batch.state = "fault"
        batch.failure_reason = f"质检不合格，按方法报废：{reason}"
        batch.held_at = batch.held_at or now()
        self._gate_alarm(batch, run, batch.failure_reason, "核对测量；确认后终止批次并处置样品。")
        return {"next": None, "batch_state": batch.state, "scrapped": True}

    def _gate_hold(self, batch: Batch, run: StepRun, reason: str) -> dict:
        """保持待人工判断：关卡留在待判定，批次保持；QA 签名放行或判不合格。"""
        run.reason = reason
        run.row_version = int(run.row_version or 0) + 1
        batch.state = "paused"
        batch.held_at = batch.held_at or now()
        batch.failure_reason = f"质检关卡待人工判断：{reason}"
        self._gate_alarm(batch, run, batch.failure_reason, "QA 在批次页对该关卡签名放行或判为不合格。")
        from .exception_service import ExceptionService

        ExceptionService(self.db, self.ctx).on_hold(batch, run, "gate", batch.failure_reason)
        return {"next": self.run_out(run), "batch_state": batch.state, "awaiting_decision": True}

    def _gate_alarm(self, batch: Batch, run: StepRun, message: str, response: str) -> None:
        from .alarm_service import AlarmService

        AlarmService(self.db, self.ctx).raise_alarm(
            severity=2, source_type="batch", source_id=batch.id, message=message[:500],
            response=response, owner="QA", origin="system", condition_key=f"gate:{run.id}",
        )

    def decide_gate(self, step_run_id: str, payload: dict, user: User) -> dict:
        """人工判定保持中的质检关卡。放行要写理由并签名，判不合格按报废处理。"""
        run = self.runs.lock(step_run_id)
        if run is None:
            raise NotFound("步骤实例不存在")
        if run.kind != GATE:
            raise StateConflict("该步骤不是质检关卡")
        if run.state != workflow.READY:
            raise StateConflict(f"关卡已是 {workflow.STATE_LABEL.get(run.state, run.state)}，不需要人工判定")
        from .identity_service import user_may

        if not user_may(self.ctx, user, "step.review"):
            raise PermissionDenied("当前账号没有质检判定权限（step.review）")
        conclusion = payload.get("conclusion")
        if conclusion not in {"approved", "rejected"}:
            raise ValidationFailed("判定结论只能是 approved 或 rejected")
        reason = (payload.get("reason") or "").strip()
        if not reason:
            raise ValidationFailed("人工判定必须写明依据")
        signature = self.identity.consume_signature(
            payload.get("signature_id"), user, f"质检关卡人工判定：{conclusion}",
            object_ref=run.id, strict=True,
        )
        batch = self.batches.get(run.batch_id)
        from .alarm_service import AlarmService

        AlarmService(self.db, self.ctx).resolve_condition(f"gate:{run.id}", f"QA 判定：{conclusion}；{reason}")
        from .exception_service import ExceptionService

        ExceptionService(self.db, self.ctx).settle_batch(batch, f"QA 质检判定 {conclusion}：{reason}", user)
        run.form_data = {**(run.form_data or {}), "decision": conclusion, "decision_reason": reason,
                         "decided_by": user.id}
        run.reviewed_by = user.id
        self.audit.record(
            user, "质检关卡人工判定", run.batch_id, sign=True, meaning=signature.meaning,
            signature_id=signature.id, before="待人工判断",
            after="放行" if conclusion == "approved" else "不合格", detail=reason,
        )
        if conclusion == "approved":
            self._close_run(run, workflow.COMPLETED, f"人工放行：{reason}")
            batch.state = "running"
            batch.held_at = None
            batch.failure_reason = ""
            outcome = self._advance(run, batch)
        else:
            self._close_run(run, workflow.FAILED, f"人工判不合格：{reason}")
            outcome = self._gate_scrap(batch, run, reason)
        self.db.commit()
        return {"step_run": self.run_out(run), "advance": outcome}

    # ---------- 条件分支与回环 ----------

    def _branch_value(self, batch: Batch, config: dict):
        """分支判据的取值：上游设备步骤最近检查点里的测量值，或上游人工记录的字段值。"""
        from ..repositories.execution import CheckpointRepository

        steps = self.steps_of(batch)
        ids = [step_id_of(step, position) for position, step in enumerate(steps)]
        source = str(config.get("source_step_id") or "")
        field = str(config.get("field") or "")
        if source not in ids:
            return None, ""
        if config.get("mode") == "measure":
            checkpoint = CheckpointRepository(self.db).latest_for_step(batch.id, ids.index(source))
            delivered = ((checkpoint.payload or {}).get("delivered") or {}) if checkpoint else {}
            return delivered.get(field), checkpoint.id if checkpoint else ""
        runs = [
            row for row in self.runs.for_batch(batch.id)
            if row.step_id == source and row.state == workflow.COMPLETED
        ]
        if not runs:
            return None, ""
        latest = runs[-1]
        return ((latest.form_data or {}).get("values") or {}).get(field), latest.id

    def _evaluate_branch(self, batch: Batch, run: StepRun, index: int) -> dict:
        """按判据选出口。人工选择的分支留作待办；判据缺失又没有默认出口时保持待人工判断。"""
        step = run.step_snapshot or {}
        config = branch_config(step)
        run.started_at = run.started_at or now()
        if config.get("mode") == "manual":
            run.reason = "等待人工选择出口"
            return {"next": self.run_out(run), "batch_state": batch.state, "awaiting_choice": True}
        value, evidence = self._branch_value(batch, config)
        run.form_data = {"field": config.get("field"), "value": value, "evidence": evidence, "mode": config.get("mode")}
        case = match_case(step, value)
        if case is None:
            reason = (
                f"判据 {config.get('field')} 没有取值" if value is None
                else f"{config.get('field')}={value} 不满足任何出口条件"
            )
            return self._branch_hold(batch, run, f"{reason}，且没有默认出口")
        return self._take_branch(batch, run, index, case, auto=True)

    def _loops_done(self, batch_id: str, step_id: str, case: str) -> int:
        return len([
            row for row in self.runs.for_batch(batch_id)
            if row.step_id == step_id and row.conclusion == case
            and row.state in {workflow.COMPLETED, workflow.SUPERSEDED}
        ])

    def _take_branch(self, batch: Batch, run: StepRun, index: int, case: str, *, auto: bool, reason: str = "") -> dict:
        step = run.step_snapshot or {}
        config = branch_config(step)
        name = step.get("name") or "条件分支"
        target = next((c for c in branch_cases(step) if str(c.get("key")) == case), {})
        loop_to = str(target.get("loop_to") or "")
        if loop_to:
            done = self._loops_done(batch.id, run.step_id, case)
            limit = int(config.get("max_loops") or 0)
            if done >= limit:
                if not auto:
                    raise StateConflict(f"「{case_label(step, case)}」已回环 {done} 次，达到上限 {limit}，请选择其他出口")
                return self._branch_hold(
                    batch, run, f"判据落在回环出口「{case_label(step, case)}」，但已回环 {done} 次、达到上限 {limit}",
                )
        run.conclusion = case
        run.form_data = {**(run.form_data or {}), "case": case, "label": case_label(step, case),
                         "auto": auto, "decision_reason": reason}
        self._close_run(run, workflow.COMPLETED, (
            f"{'自动' if auto else '人工'}选择出口「{case_label(step, case)}」" + (f"：{reason}" if reason else "")
        ))
        if auto:
            # 人工选择的审计由 decide_branch 带签名记录，这里只记系统自动判定
            self.audit.record(
                None, "条件分支选择出口", batch.id, before="待判定", after=case_label(step, case),
                detail=f"{name}：{run.reason}；判据 {config.get('field')}={(run.form_data or {}).get('value')}",
            )
        if loop_to:
            return self._loop_back(batch, run, index, loop_to, done + 1)
        return self._advance(run, batch)

    def _loop_back(self, batch: Batch, run: StepRun, index: int, target_id: str, rounds: int) -> dict:
        """回环：回环体（目标到分支之间的上游步骤）与分支本身的记录作废，从目标重做。"""
        steps = self.steps_of(batch)
        ids = [step_id_of(step, position) for position, step in enumerate(steps)]
        target = ids.index(target_id)
        body = graph.loop_body(steps, index, target) | {index}
        if not self.advances.claim(batch.id, run.step_id, run.attempt, f"loop:{target_id}"):
            return {"next": None, "duplicate": True}
        voided = 0
        for row in self.runs.for_batch(batch.id):
            if row.step_index in body and row.state in {workflow.COMPLETED, workflow.SKIPPED, workflow.NOT_TAKEN}:
                row.state = workflow.SUPERSEDED
                row.reason = (row.reason + "；" if row.reason else "") + f"分支第 {rounds} 次回环，本次结论作废"
                row.row_version = int(row.row_version or 0) + 1
                self._retire_split_children(row)
                voided += 1
        new_run = self.open_step(batch, target)
        self.audit.record(
            None, "分支回环", batch.id, before=run.reason,
            after=f"回到第 {target + 1} 步（第 {new_run.attempt} 次）",
            detail=f"{(run.step_snapshot or {}).get('name')}：第 {rounds} 次回环；{voided} 条记录保留并标为作废",
        )
        return {**self.enter(batch, new_run, target), "loop": rounds}

    def _branch_hold(self, batch: Batch, run: StepRun, reason: str) -> dict:
        """判据缺失或回环到上限：分支留在待判定，批次保持，等有权限的人选出口。"""
        from .alarm_service import AlarmService

        run.reason = reason
        run.row_version = int(run.row_version or 0) + 1
        batch.state = "paused"
        batch.held_at = batch.held_at or now()
        batch.failure_reason = f"条件分支待人工选择：{reason}"
        batch.current_step = run.step_index
        alarm = AlarmService(self.db, self.ctx).raise_alarm(
            severity=2, source_type="batch", source_id=batch.id, message=batch.failure_reason[:500],
            response="在批次页为该分支选择出口并签名；选择后批次继续。", owner="QA", origin="system",
            condition_key=f"branch:{run.id}",
        )
        from .exception_service import ExceptionService

        ExceptionService(self.db, self.ctx).on_hold(batch, run, "branch", batch.failure_reason, alarm.id)
        return {"next": self.run_out(run), "batch_state": batch.state, "awaiting_decision": True}

    def decide_branch(self, step_run_id: str, payload: dict, user: User) -> dict:
        """人工选择分支出口。人工选择模式的分支是普通待办；判据缺失而保持的分支要 QA 判定并签名。"""
        from .alarm_service import AlarmService
        from .identity_service import user_may

        run = self.runs.lock(step_run_id)
        if run is None:
            raise NotFound("步骤实例不存在")
        if run.kind != BRANCH:
            raise StateConflict("该步骤不是条件分支")
        if run.state != workflow.READY:
            raise StateConflict(f"分支已是 {workflow.STATE_LABEL.get(run.state, run.state)}，不需要选择")
        self.runs.check_version(run, payload.get("row_version"), "步骤实例")
        batch = self.batches.lock(run.batch_id)
        if batch is None:
            raise NotFound("批次不存在")
        step = run.step_snapshot or {}
        held = batch.state == "paused" and (batch.failure_reason or "").startswith("条件分支待人工选择")
        needed = "step.review" if held else "step.submit"
        if not user_may(self.ctx, user, needed):
            raise PermissionDenied(
                "判据缺失的分支要由有质检判定权限（step.review）的人选择" if held
                else "当前账号不能提交流程记录（step.submit）"
            )
        if batch.state not in {"running", "paused"} or (batch.state == "paused" and not held):
            raise StateConflict(f"批次状态为 {batch.state}，不能选择分支出口")
        case = str(payload.get("case") or "")
        keys = [str(c.get("key")) for c in branch_cases(step)]
        if case not in keys:
            raise ValidationFailed(f"出口 {case or '（空）'} 不存在；可选：{'、'.join(keys)}")
        reason = (payload.get("reason") or "").strip()
        if not reason:
            raise ValidationFailed("选择分支出口必须写明依据")
        signature = None
        if held or step.get("requires_signature"):
            signature = self.identity.consume_signature(
                payload.get("signature_id"), user, f"条件分支选择出口：{case_label(step, case)}",
                object_ref=run.id, strict=True,
            )
        run.form_data = {**(run.form_data or {}), "decided_by": user.id}
        run.reviewed_by = user.id
        run.submitted_by = user.id
        self.audit.record(
            user, "人工选择分支出口", batch.id, sign=bool(signature),
            meaning=signature.meaning if signature else "", signature_id=signature.id if signature else "",
            before="待选择", after=case_label(step, case), detail=f"{step.get('name')}：{reason}",
        )
        if held:
            from .exception_service import ExceptionService

            AlarmService(self.db, self.ctx).resolve_condition(f"branch:{run.id}", f"人工选择出口 {case}：{reason}")
            ExceptionService(self.db, self.ctx).settle_batch(batch, f"QA 选择分支出口「{case_label(step, case)}」：{reason}", user)
            batch.state = "running"
            batch.held_at = None
            batch.failure_reason = ""
        outcome = self._take_branch(batch, run, run.step_index, case, auto=False, reason=reason)
        self.db.commit()
        return {"step_run": self.run_out(run), "advance": outcome}

    def branch_todos(self) -> list[dict]:
        return [self.run_out(row) for row in self.runs.pending_branch_choices()]

    # ---------- 业务信号 ----------

    def signal(self, batch_id: str, name: str, payload: dict | None, event_id: str, user: User | None) -> dict:
        """外部系统或现场人员发出的批次业务事件。唤醒等着它的事件等待节点；早到的先登记。

        `event_id` 是发送方的幂等键：同一条信号重发返回第一次的结果，不会唤醒第二个等待节点。
        服务身份只能发授权范围里的事件名（`batch_signals`）。
        """
        name = (name or "").strip()
        if not name or len(name) > 64 or not all(ch.isalnum() or ch in "_-.:" for ch in name):
            raise ValidationFailed("事件名只能包含字母、数字与 _ - . :，最长 64 个字符")
        if self.ctx.is_service:
            allowed = (self.ctx.scopes or {}).get("batch_signals")
            if not (allowed == "all" or (isinstance(allowed, list) and name in allowed)):
                raise PermissionDenied(f"服务身份未被授权发出事件 {name}（batch_signals）", code="service_scope_denied")
        batch = self.batches.lock(batch_id)
        if batch is None:
            raise NotFound("批次不存在")
        key = f"signal:{batch.id}:{(event_id or '').strip() or uid_hex()}"
        existing = self.signals.by_key(key)
        if existing is not None:
            return {**self.signal_out(existing), "replayed": True}
        if batch.state in {"done", "aborted"}:
            raise StateConflict(f"批次已{('完成' if batch.state == 'done' else '终止')}，不再接收业务事件")
        source = f"service:{self.ctx.subject_id}" if self.ctx.is_service else f"user:{user.id if user else ''}"
        signal = BatchSignal(
            org_id=batch.org_id, batch_id=batch.id, name=name, event_key=key, payload=payload or {},
            source=source, source_label=self.ctx.subject_label or (user.display_name if user else ""),
        )
        try:
            with self.db.begin_nested():
                self.db.add(signal)
                self.db.flush()
        except IntegrityError:
            found = self.signals.by_key(key)
            if found is None:
                raise
            return {**self.signal_out(found), "replayed": True}
        waiting = self.runs.waiting_for_event(batch.id, name)
        event = None
        if waiting:
            event = self._bind_signal(batch, waiting[0], signal)
        self.audit.record(
            user, "收到批次业务事件", batch.id, after=name,
            detail=(
                f"来源 {signal.source_label or signal.source}；"
                + (f"唤醒第 {waiting[0].step_index + 1} 步等待" if waiting else "暂无等待节点，已登记待消费")
            ),
        )
        self.db.commit()
        outcome = self.process_event(event.id) if event is not None else None
        return {**self.signal_out(signal), "advance": outcome}

    def _bind_signal(self, batch: Batch, run: StepRun, signal: BatchSignal) -> WorkflowEvent:
        signal.consumed_by_run_id = run.id
        signal.consumed_at = now()
        run.form_data = {**(run.form_data or {}), "signal_id": signal.id, "signal": signal.name,
                         "signal_payload": signal.payload or {}}
        return self.emit(
            batch.id, run.id, "signal", f"signal:{run.id}:{run.attempt}",
            {"signal_id": signal.id, "name": signal.name}, org_id=batch.org_id,
        )

    @staticmethod
    def signal_out(signal: BatchSignal) -> dict:
        return {
            "id": signal.id, "batch_id": signal.batch_id, "name": signal.name, "payload": signal.payload or {},
            "source": signal.source, "source_label": signal.source_label,
            "received_at": signal.received_at.isoformat(timespec="seconds") if signal.received_at else None,
            "consumed_by_run_id": signal.consumed_by_run_id,
            "consumed_at": signal.consumed_at.isoformat(timespec="seconds") if signal.consumed_at else None,
        }

    def signals_for_batch(self, batch_id: str) -> list[dict]:
        return [self.signal_out(row) for row in self.signals.for_batch(batch_id)]

    # ---------- 步骤级超时 ----------

    def _handle_timeouts(self) -> int:
        """到了截止时刻的步骤：报警，或按配置判失败 / 跳过（后两者经推进事件，与其他结论互斥）。"""
        from .alarm_service import AlarmService

        handled = 0
        for run in self.runs.overdue_deadlines():
            batch = self.db.get(Batch, run.batch_id)
            if batch is None or batch.state in {"done", "aborted"}:
                run.timed_out_at = now()
                continue
            step = run.step_snapshot or {}
            timeout = step.get("timeout") or {}
            action = timeout.get("action") or "alarm"
            minutes = timeout.get("minutes")
            name = step.get("name") or f"第 {run.step_index + 1} 步"
            run.timed_out_at = now()
            reason = f"「{name}」超过 {minutes} min 仍未完成（{TIMEOUT_ACTIONS.get(action, action)}）"
            AlarmService(self.db, self.ctx).raise_alarm(
                severity=2 if action != "alarm" else 3, source_type="batch", source_id=batch.id,
                message=f"第 {run.step_index + 1} 步{KIND_NAMES.get(run.kind, run.kind)}节点{reason}"[:500],
                response={
                    "alarm": "催办执行人或到现场查看；步骤完成后条件自动复位。",
                    "fail": "步骤已判为失败，批次进入恢复评估。",
                    "skip": "步骤已按方法配置自动跳过，流程继续；请核对是否需要补做。",
                }.get(action, ""),
                owner="操作员", origin="system", condition_key=f"step:{run.id}:timeout",
            )
            if action in {"fail", "skip"} and not (action == "fail" and run.kind == DEVICE):
                self.emit(
                    batch.id, run.id, "timeout", f"timeout:{run.id}:{run.attempt}",
                    {"action": action, "reason": reason}, org_id=batch.org_id,
                )
            from .exception_service import ExceptionService

            ExceptionService(self.db, self.ctx).on_step_timeout(batch, run, action, reason)
            handled += 1
        return handled

    # ---------- 样本拆分 ----------

    def _split_samples(self, batch: Batch, run: StepRun) -> dict:
        """每个在用样本拆出 N 个子样本：登记子物理样本（谱系指向母样），生成子运行分配。

        母样本的运行分配标为已拆分，之后的步骤、检测与统计都落在子样本上；
        子样本继承条件分组，等于把重复数放大 N 倍。
        """
        from ..models import PhysicalSample

        split = (run.step_snapshot or {}).get("split") or {}
        count = int(split.get("count") or 0)
        child_type = split.get("child_type") or ""
        suffix = "" if run.attempt == 1 else f"r{run.attempt}"
        parents = self._active_samples(batch)
        children: list[str] = []
        for sample in parents:
            container = f"{sample.container_id}-{run.step_id}{suffix}"
            for number in range(1, count + 1):
                physical_id = f"{sample.physical_sample_id or sample.id}-{run.step_id}{suffix}-{number}"
                if self.db.get(PhysicalSample, physical_id) is None:
                    self.db.add(PhysicalSample(
                        id=physical_id, org_id=batch.org_id, barcode=physical_id,
                        source=f"批次 {batch.id} 第 {run.step_index + 1} 步拆分", sample_type=child_type,
                        parent_id=sample.physical_sample_id or None, current_location=container,
                        custodian=batch.operator, lifecycle_state="in_use", origin="batch_generated",
                        created_by=self.ctx.subject_id,
                    ))
                child = Sample(
                    id=f"{sample.id}-{number}{suffix}", org_id=batch.org_id, physical_sample_id=physical_id,
                    batch_id=batch.id, container_id=container, well=f"{sample.well}-{number}",
                    position=sample.position * count + number, condition_group=sample.condition_group,
                    condition_label=sample.condition_label, repeat=(sample.repeat - 1) * count + number,
                    levels=sample.levels, is_control=sample.is_control, state="running",
                )
                self.db.add(child)
                children.append(child.id)
            sample.state = "split"
            sample.flag_note = f"第 {run.step_index + 1} 步拆分为 {count} 个{child_type}"
        self.db.flush()
        run.form_data = {"parents": len(parents), "count": count, "child_type": child_type,
                         "children": children}
        self._close_run(run, workflow.COMPLETED, f"{len(parents)} 个样本各拆分为 {count} 个{child_type}")
        self.audit.record(
            None, "样本拆分", batch.id, before=f"{len(parents)} 个样本", after=f"{len(children)} 个{child_type}",
            detail="子样本谱系指向母样，继承条件分组；母样运行分配标为已拆分",
        )
        return self._advance(run, batch)

    def _retire_split_children(self, run: StepRun) -> None:
        if run.kind != SPLIT:
            return
        for child_id in (run.form_data or {}).get("children") or []:
            child = self.db.get(Sample, child_id)
            if child is not None:
                child.state = "failed"
                child.flag_note = "所属拆分步骤被质检返工作废"

    def finish_batch(self, batch: Batch, user: User | None = None, detail: str = "") -> None:
        from .exception_service import ExceptionService

        before = "运行中" if batch.state == "running" else batch.state
        batch.state = "done"
        batch.held_at = None
        ExceptionService(self.db, self.ctx).settle_batch(batch, "批次运行完成", user)
        self.audit.record(
            user, "批次完成", batch.id, before=before, after="已完成",
            detail=detail or (
                "运行结束；样本质量与结果审核状态不受此影响，"
                "任务仍可处于待数据复核或待报告"
            ),
        )

    def next_open_step(
        self, steps: list[dict], batch: Batch, from_index: int
    ) -> tuple[int | None, str | None]:
        return self._next_open_step(steps, batch, from_index)

    def _next_open_step(
        self, steps: list[dict], batch: Batch, from_index: int
    ) -> tuple[int | None, str | None]:
        """找下一个还没完成过的步骤。

        审核退回后人工记录会重做一遍；中间的设备步骤已经物理执行过，
        跳过它们才是「不自动回退重跑已执行的物理设备步骤」。已完成的步骤不会被
        重新开一个实例，流程直接回到没完成的那一步——通常就是那个审核节点。
        """
        completed = {
            row.step_id for row in self.runs.for_batch(batch.id)
            if row.state in {workflow.COMPLETED, workflow.SKIPPED}
        }
        for index in range(from_index + 1, len(steps)):
            step_id = step_id_of(steps[index], index)
            if step_id not in completed:
                return index, step_id
        return None, None

    def _handle_review_rejection(self, run: StepRun, batch: Batch) -> dict:
        """审核退回。

        退回形成上一个人工步骤的新尝试，旧记录保留；上游是设备步骤时不自动回退重跑——
        物理动作已经发生，重做要走恢复评估或新建运行。
        """
        steps = self.steps_of(batch)
        # 退回落在最近的人工步骤上：那是记录可以更正的地方
        target_step_id = ""
        index = None
        for position in range(run.step_index - 1, -1, -1):
            if kind_of(steps[position]) == MANUAL:
                index, target_step_id = position, step_id_of(steps[position], position)
                break
        if index is None:
            # 上游没有人工节点，只有设备步骤：物理动作已经发生，不能自动回退重跑
            batch.state = "paused"
            batch.held_at = now()
            batch.failure_reason = (
                "审核退回：上游只有设备步骤，不自动回退重跑，请走恢复评估或新建运行"
            )
            self.audit.record(
                None, "审核退回后保持", batch.id, before="运行中", after="已保持",
                detail=batch.failure_reason,
            )
            return {"next": None, "batch_state": batch.state, "needs_recovery": True}
        if not self.advances.claim(batch.id, run.step_id, run.attempt, target_step_id):
            return {"next": None, "duplicate": True}
        new_run = self.open_step(batch, index)
        self.audit.record(
            None, "审核退回生成新的人工尝试", batch.id, before="审核退回",
            after=f"第 {index + 1} 步第 {new_run.attempt} 次",
            detail="旧记录保留，不覆盖",
        )
        return {"next": self.run_out(new_run), "batch_state": batch.state, "reopened": True}

    def tick(self, limit: int | None = None) -> dict:
        """后台推进一轮。无浏览器请求也能处理到期等待与回执。"""
        claimed = 0
        processed = 0
        results: list[dict] = []
        # 步骤级超时：报警，或产出判失败 / 跳过的推进事件
        self._handle_timeouts()
        # 到期的等待步骤先产出事件；保持中可以记录，但推进时会被设备动作检查挡住
        for run in self.runs.due_waits():
            batch = self.db.get(Batch, run.batch_id)
            self.emit(
                run.batch_id, run.id, "wait_due", f"wait:{run.id}:{run.attempt}",
                {"due_at": run.due_at.isoformat() if run.due_at else ""},
                org_id=batch.org_id if batch else "",
            )
        self.db.commit()

        # 领取超时的 processing 行重新排队：崩溃的进程不该永久占着事件
        stale_before = now() - timedelta(seconds=settings.advance_claim_timeout_sec)
        for event in self.events.stale_processing(stale_before):
            event.state = "pending"
            event.claimed_at = None
            event.claimed_by = ""
        self.db.commit()

        events = self.events.claim_batch(limit or settings.advance_batch_size)
        claimed_events: list[tuple[str, str]] = []
        for event in events:
            event.state = "processing"
            event.claimed_at = now()
            event.claimed_by = self.ctx.subject_label or "advancer"
            claimed_events.append((event.id, event.event_key))
        # 所有候选仍持有行锁时一次性完成领取。逐条 commit 会提前释放尚未标记的
        # 候选行，让另一个推进器领取同一批事件。
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            claimed_events = []

        claimed = len(claimed_events)
        for event_id, event_key in claimed_events:
            outcome = self.process_event(event_id)
            if outcome.get("processed"):
                processed += 1
            results.append({"event_id": event_key, **outcome})
        return {
            "claimed": claimed, "processed": processed, "results": results,
            "at": now().isoformat(timespec="seconds"),
        }

    # ---------- 取消 ----------

    def cancel_open_runs(self, batch: Batch, reason: str) -> int:
        cancelled = 0
        for run in self.runs.open_runs(batch.id):
            run.state = workflow.CANCELLED
            run.ended_at = now()
            run.reason = reason
            run.row_version = int(run.row_version or 0) + 1
            cancelled += 1
        for event in self.events.for_batch(batch.id):
            if event.state == "pending":
                event.state = "rejected"
                event.error = reason
                event.processed_at = now()
        return cancelled

    # ---------- 输出 ----------

    def run_out(self, run: StepRun) -> dict:
        step = run.step_snapshot or {}
        assignee = self.users.get(run.assignee_user_id) if run.assignee_user_id else None
        submitter = self.users.get(run.submitted_by) if run.submitted_by else None
        reviewer = self.users.get(run.reviewed_by) if run.reviewed_by else None
        return {
            "id": run.id,
            "batch_id": run.batch_id,
            "step_id": run.step_id,
            "step_index": run.step_index,
            "step_name": step.get("name") or f"第 {run.step_index + 1} 步",
            "kind": run.kind,
            "kind_label": KIND_NAMES.get(run.kind, run.kind),
            "attempt": run.attempt,
            "state": run.state,
            "state_label": workflow.STATE_LABEL.get(run.state, run.state),
            "station_id": run.station_id,
            "assignee_user_id": run.assignee_user_id,
            "assignee_name": assignee.display_name if assignee else "",
            "due_at": run.due_at.isoformat(timespec="seconds") if run.due_at else None,
            "started_at": run.started_at.isoformat(timespec="seconds") if run.started_at else None,
            "ended_at": run.ended_at.isoformat(timespec="seconds") if run.ended_at else None,
            "form": step.get("form") or [],
            "form_data": run.form_data or {},
            "requires_signature": bool(step.get("requires_signature")),
            "review_role": step.get("review_role", ""),
            "wait_for": step.get("wait_for") or {},
            "branch": step.get("branch") or {},
            "branch_cases": [
                {"key": str(c.get("key")), "label": c.get("label") or c.get("key"), "loop": bool(c.get("loop_to"))}
                for c in branch_cases(step)
            ] if run.kind == BRANCH else [],
            "skippable": bool(step.get("skippable")),
            "timeout": step.get("timeout") or None,
            "deadline_at": run.deadline_at.isoformat(timespec="seconds") if run.deadline_at else None,
            "timed_out_at": run.timed_out_at.isoformat(timespec="seconds") if run.timed_out_at else None,
            "groups": step.get("groups") or [],
            "conclusion": run.conclusion,
            "reason": run.reason,
            "submitted_by": run.submitted_by,
            "submitted_by_name": submitter.display_name if submitter else "",
            "reviewed_by": run.reviewed_by,
            "reviewed_by_name": reviewer.display_name if reviewer else "",
            "row_version": run.row_version,
        }

    def runs_for_batch(self, batch_id: str) -> list[dict]:
        return [self.run_out(row) for row in self.runs.for_batch(batch_id)]

    def events_for_batch(self, batch_id: str) -> list[dict]:
        return [
            {
                "id": row.id, "event_key": row.event_key, "event_type": row.event_type,
                "state": row.state, "error": row.error,
                "available_at": row.available_at.isoformat(timespec="seconds"),
                "processed_at": row.processed_at.isoformat(timespec="seconds") if row.processed_at else None,
                "payload": row.payload or {},
            }
            for row in self.events.for_batch(batch_id)
        ]

    def my_manual_todos(self, user_id: str) -> list[dict]:
        return [self.run_out(row) for row in self.runs.pending_manual(user_id)]

    def review_todos(self) -> list[dict]:
        return [self.run_out(row) for row in self.runs.pending_review()]


def system_workflow(db: Session, org_id: str) -> WorkflowService:
    """后台推进器用的受限系统上下文。组织从持久化事件确定。"""
    return WorkflowService(db, system_context(org_id))
