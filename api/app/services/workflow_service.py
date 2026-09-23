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
from ..domain import graph, workflow
from ..domain.access import same_person
from ..domain.steps import (
    DEVICE, GATE, MANUAL, REVIEW, SPLIT, WAIT, KIND_NAMES, kind_of, missing_form_values, normalize,
    step_id_of,
)
from ..domain.permissions import ADMIN, ROLE_NAMES
from ..models import Batch, Sample, StepRun, User, WorkflowEvent, roles_of
from ..repositories.batches import BatchRepository, SampleRepository
from ..repositories.execution import CommandRepository
from ..repositories.governance import UserRepository
from ..repositories.materials import ReservationRepository
from ..repositories.resources import CapabilityRepository
from ..repositories.workflow import (
    StepAdvanceRepository, StepRunRepository, WorkflowEventRepository,
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

    def start_first_step(self, batch: Batch, assignee_user_id: str = "") -> StepRun:
        steps = self.steps_of(batch)
        if not steps:
            raise StateConflict("方法没有步骤，无法开跑")
        return self.open_step(batch, 0, assignee_user_id)

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
        self.runs.add(run)
        batch.current_step = index
        return run

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
        target = {
            "manual_submit": workflow.COMPLETED,
            "wait_due": workflow.COMPLETED,
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

        if target == workflow.FAILED and run.kind == REVIEW:
            return self._handle_review_rejection(run, batch)
        if target in {workflow.FAILED, workflow.CANCELLED}:
            batch.state = "fault" if target == workflow.FAILED else batch.state
            batch.failure_reason = event.payload.get("reason") or "步骤失败"
            return {"batch_state": batch.state, "next": None}
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
        """依赖图模式的推进：一步完成后，开出所有前驱都已完成、且从未尝试过的步骤。

        - 分叉：一步完成可以同时开出多个后继（设备动作与人工记录、等待并行）。
        - 汇合：后继要等它的全部前驱完成；先完成的分支到这里什么也不开，等最后一个前驱。
        - 每个后继的开出各自经 `StepAdvance` 去重：两个前驱几乎同时完成时，汇合步骤只开一次。
        - 全部步骤完成才结束批次，不是「最后一行完成」就结束。
        """
        # 两个分支在不同事务里几乎同时完成时，各自都可能看不到对方的完成而谁也不开汇合步骤。
        # 先取批次行锁再重读步骤实例：后到的事务等前一个提交，读到的是它提交后的状态。
        self.db.flush()
        self.batches.lock(batch.id)
        rows = list(
            self.db.query(StepRun).filter(StepRun.batch_id == batch.id)
            .order_by(StepRun.step_index, StepRun.attempt).populate_existing().all()
        )
        self._release_waiting_devices(batch, rows, finished=run)
        completed = {row.step_id for row in rows if row.state == workflow.COMPLETED}
        attempted = {
            row.step_id for row in rows
            if row.state not in {workflow.COMPLETED, workflow.SUPERSEDED, workflow.CANCELLED}
        }
        if graph.all_completed(steps, completed):
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
        opened: list[dict] = []
        for index in graph.ready_after(steps, completed, attempted):
            step_id = step_id_of(steps[index], index)
            # 去重键用「汇合步骤本身」而不是触发它的前驱：两个前驱同时完成也只开一次
            if not self.advances.claim(batch.id, f"join:{step_id}", self._join_epoch(rows, step_id), step_id):
                continue
            next_run = self.open_step(batch, index)
            opened.append(self.enter(batch, next_run, index))
        self._sync_current_step(batch)
        if not opened:
            waiting = [
                step_id_of(steps[index], index) for index in range(len(steps))
                if step_id_of(steps[index], index) not in completed
            ]
            return {"next": None, "batch_state": batch.state, "waiting_for": waiting}
        first = opened[0]
        return {**first, "opened": [row.get("next") for row in opened]}

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
        before = "运行中" if batch.state == "running" else batch.state
        batch.state = "done"
        batch.held_at = None
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
            if row.state == workflow.COMPLETED
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
