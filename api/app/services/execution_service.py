"""执行层用例：命令投递、设备回执、检查点、重启对账。

三条刻意的分界：
1. 设备动作与流程推进分离——`_perform` 只让设备动一次并写回执事件，下一步由
   `WorkflowService` 的推进器决定。
2. 网络 I/O 不在长事务里：先持久化命令与投递状态，再调适配器，回来再写结果。
3. 结果未知不等于失败：`maybe_sent` 的命令重启后先按原 command_id 查设备侧状态，
   查不到也不盲目重发，而是转人工核查。
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy.orm import Session

from ..adapters import AdapterError, AdapterUnreachable, CommandRequest, adapter_for
from ..core.clock import now
from ..core.config import settings
from ..core.context import AccessContext, system_context
from ..domain import workflow
from ..domain.steps import DEVICE, kind_of, normalize, step_id_of
from ..models import AdapterExecution, Batch, Checkpoint, Command, FileObject, Station, Telemetry
from ..repositories.batches import AllocationRepository, BatchRepository, SampleRepository
from ..repositories.execution import (
    DISPATCHING, MOTION, AdapterExecutionRepository, CheckpointRepository, CommandRepository,
)
from ..repositories.materials import ReservationRepository
from ..repositories.resources import AdapterRepository, CapabilityRepository
from ..repositories.workflow import StepRunRepository
from .alarm_service import AlarmService
from .audit_service import AuditService
from .file_service import FileService
from .gate_service import GateService



class ExecutionService:
    """一个 ExecutionService 实例服务一个组织上下文。

    执行器按持久化命令上的 org_id 构造上下文，不存在绕过范围的全局身份。
    """

    def __init__(self, db: Session, ctx: AccessContext):
        self.db = db
        self.ctx = ctx
        self.batches = BatchRepository(db, ctx)
        self.allocations = AllocationRepository(db)
        self.samples = SampleRepository(db, ctx)
        self.commands = CommandRepository(db, ctx)
        self.checkpoints = CheckpointRepository(db)
        self.executions = AdapterExecutionRepository(db)
        self.reservations = ReservationRepository(db, ctx)
        self.adapters = AdapterRepository(db)
        self.capabilities = CapabilityRepository(db)
        self.runs = StepRunRepository(db, ctx)
        self.audit = AuditService(db, ctx)
        self.alarms = AlarmService(db, ctx)

    # ---------- 单条命令 ----------

    def execute(self, command: Command) -> bool:
        ledger = self.executions.get(command.id)
        if ledger:
            return False  # 重复投递：回放终态，不产生第二次物理动作
        # 与保持 / 终止串行：先锁批次，再确认这条指令仍在队列里。保持撤回队列指令也
        # 先拿同一把锁，所以「批次已保持」与「指令已发出」不会各自基于对方提交前的状态。
        batch = self.batches.lock(command.batch_id) if command.batch_id else None
        if not batch:
            return False
        self.db.refresh(command)
        if command.state != "sent" or command.delivery_state != "queued":
            return False  # 已被撤回，或已被另一轮领走
        if command.type in MOTION and batch.state != "running":
            self.withdraw(command, f"批次处于 {batch.state}，动作指令未投递")
            self.db.commit()
            return True

        record = self.adapters.get(command.station_id)
        if not record:
            self.fault(
                batch, command,
                f"工位 {command.station_id or '未分配'} 没有可用适配器，指令无法投递",
                delivery="unreachable",
            )
            self.db.commit()
            return True
        blocker = self.delivery_blocker(command, record)
        if blocker:
            self._refuse(batch, command, blocker)
            return True
        if command.not_before is not None and now() < command.not_before:
            # 按时开工：还没到排程时间窗，留在队列里；此时保持 / 终止仍可撤回它
            self.db.commit()
            return False
        if command.type in MOTION and not GateService(self.db).status()["open"]:
            # 全站执行门关闭：动作指令留在队列里，门打开后再投；此时保持 / 终止仍可撤回它
            self.db.commit()
            return False
        if command.type in MOTION and self._station_busy(command, record):
            # 单通道工位（AGV、机械臂）上一条动作还没结论：排队等，不同时塞给它两条
            self.db.commit()
            return False
        try:
            adapter = adapter_for(record, tuple(self.capabilities.specs()))
        except (AdapterError, NotImplementedError) as exc:
            self._refuse(batch, command, f"适配器不可用：{exc}；指令未投递，不自动重试")
            return True

        # 比较并交换：只有仍在队列里的指令才能被领走，与撤回互斥
        claimed = (
            self.db.query(Command)
            .filter(
                Command.id == command.id,
                Command.state == "sent",
                Command.delivery_state == "queued",
            )
            .update(
                # 先把「可能已发出」落库，再做设备侧调用：崩在中间时我们知道要去对账
                {"state": "accepted", "delivery_state": "maybe_sent", "updated_at": now(),
                 "started_at": now()},
                synchronize_session=False,
            )
        )
        if not claimed:
            self.db.commit()
            return False
        self.db.refresh(command)
        ledger = AdapterExecution(
            command_id=command.id, station_id=command.station_id, state="accepted"
        )
        self.db.add(ledger)
        if command.type in MOTION:
            # 工位上的「当前指令」只属于动作指令；保持 / 终止不能把它覆盖掉，
            # 否则下一轮对账会把仍在执行的原指令判成不一致
            record.current_command_id = command.id
        self.db.flush()
        self.db.commit()

        command.state = "running"
        ledger.state = "running"
        # 设备接受命令后，对应步骤实例从「待办」转成「执行中」；
        # 回执事件要的是 running → completed，不是 ready → completed
        run = self._run_for(command, batch) if command.type in DISPATCHING else None
        if run is not None and run.state == workflow.READY:
            run.state = workflow.RUNNING
            run.started_at = run.started_at or now()
            run.station_id = command.station_id
            run.row_version = int(run.row_version or 0) + 1
            self.db.flush()
        self._perform(batch, command, ledger, record, adapter)
        ledger.updated_at = now()
        return True

    def _station_busy(self, command: Command, record) -> bool:
        station = self.db.get(Station, command.station_id)
        if station is not None and (station.channels or 1) > 1:
            return False
        current = record.current_command_id
        if not current or current == command.id:
            return False
        other = self.db.get(Command, current)
        return other is not None and other.state in {"accepted", "running"}

    @staticmethod
    def delivery_blocker(command: Command, record) -> str:
        """投递前的工位条件。保持 / 终止是安全动作，只要求设备可达。"""
        if not record.enabled:
            return "适配器已停用；指令未投递，不自动重试"
        if not record.connected:
            return "适配器失联；指令未投递，不自动重试"
        if command.type not in MOTION:
            return ""
        if record.site_interlock:
            return "安全联锁触发；动作指令未投递，不自动重试"
        if not record.accepts_commands:
            return "适配器拒绝动作指令；指令未投递，不自动重试"
        age = (now() - record.last_heartbeat).total_seconds() if record.last_heartbeat else None
        if age is None or age > settings.heartbeat_stale_sec:
            return "适配器心跳超时，在线状态不可信；动作指令未投递，不自动重试"
        return ""

    def _refuse(self, batch: Batch, command: Command, reason: str) -> None:
        """指令没有离开系统就被拒：记入幂等台账，保证它以后也不会被投递。"""
        self.db.add(
            AdapterExecution(command_id=command.id, station_id=command.station_id, state="rejected")
        )
        self.fault(batch, command, reason, delivery="unreachable")
        self.db.commit()

    def withdraw(self, command: Command, reason: str) -> bool:
        """撤回还没交给适配器的指令。与执行器领取是同一个比较并交换，二者只有一个成功。"""
        withdrawn = (
            self.db.query(Command)
            .filter(
                Command.id == command.id,
                Command.state == "sent",
                Command.delivery_state == "queued",
            )
            .update(
                {"state": "cancelled", "delivery_state": "not_sent", "error": reason,
                 "updated_at": now()},
                synchronize_session=False,
            )
        )
        self.db.refresh(command)
        if withdrawn:
            self.audit.record(
                None, "撤回未投递指令", command.id, before="排队中", after="已撤回",
                detail=f"{reason}；设备侧从未收到该指令", command_id=command.id,
            )
        return bool(withdrawn)

    def _perform(
        self, batch: Batch, command: Command, ledger: AdapterExecution, record, adapter,
    ) -> None:
        target = ""
        if command.type in {"hold", "abort"}:
            acting = [
                c for c in self.commands.in_flight_for_batch(batch.id, MOTION)
                if c.station_id == command.station_id
            ]
            target = acting[0].id if acting else ""
        request = CommandRequest(
            command_id=command.id, station_id=command.station_id, capability=command.capability,
            params=command.params or {}, type=command.type, batch_id=batch.id,
            step_index=command.step_index,
            step_id=self._step_id(batch, command.step_index),
            target_command_id=target,
        )
        if command.type in {"hold", "abort"} and not (
            record.supports_hold if command.type == "hold" else record.supports_abort
        ):
            ledger.state = "rejected"
            self.fault(
                batch, command,
                f"该适配器声明不支持{'保持' if command.type == 'hold' else '终止'}，"
                f"请按现场规程处理，系统不假装支持",
                delivery="delivered",
            )
            return
        try:
            if command.type == "hold":
                result = adapter.hold(request)
            elif command.type == "abort":
                result = adapter.abort(request)
            else:
                violation = self.check_hard_window(batch, command)
                if violation:
                    ledger.state = "rejected"
                    self.fault(batch, command, violation, delivery="delivered")
                    self._release(record, command)
                    return
                result = adapter.submit(request)
        except AdapterUnreachable as exc:
            # 网络超时或回执无法解读：动作可能已经发生，结论未知。保留占用，等对账或人工核查。
            ledger.state = "unknown"
            self.fault(
                batch, command,
                f"设备无响应或回执无法确认（{exc}）；动作可能已执行，结果未知，不自动重试",
                delivery="maybe_sent",
            )
            return
        except AdapterError as exc:
            ledger.state = "failed"
            self.fault(batch, command, f"适配器明确失败：{exc}", delivery="delivered")
            self._release(record, command)
            return
        except Exception as exc:
            # 驱动内部的意外错误不能当作「设备明确拒绝」：请求可能已经发出
            ledger.state = "unknown"
            self.fault(
                batch, command,
                f"适配器内部错误（{exc.__class__.__name__}）；动作可能已执行，结果未知，不自动重试",
                delivery="maybe_sent",
            )
            return

        command.delivery_state = "delivered"
        self.settle(batch, command, ledger, record, result)

    def settle(self, batch: Batch, command: Command, ledger: AdapterExecution, record, result) -> None:
        """把一份设备回执落到指令、台账与批次上。投递与轮询共用同一套结论。"""
        ledger.result = result.as_dict()
        ledger.updated_at = now()
        if result.state in {"accepted", "running"}:
            ledger.state = result.state
            command.state = "running"
            command.updated_at = now()
            return
        self._release(record, command)
        if command.overdue_at is not None:
            self.alarms.resolve_condition(
                f"command:{command.id}:overdue", f"指令 {command.id} 已给出结论 {result.state}",
            )
        if result.state != "done":
            ledger.state = result.state
            self.fault(
                batch, command, result.error or f"设备回执状态 {result.state}",
                delivery="delivered",
            )
            return
        ledger.state = "done"
        if command.type in DISPATCHING:
            self.complete_device_step(batch, command, result)
            return
        if command.type == "transfer":
            self._complete_transfer(batch, command, record)
            return
        command.state = "done"
        command.updated_at = now()
        if command.type == "abort":
            self._confirm_abort(batch, command, record)

    def _complete_transfer(self, batch: Batch, command: Command, record) -> None:
        """转运回执确认完成：板的位置按回执更新，等着它的设备动作这时才进候选。"""
        from .transfer_service import TransferService

        mismatch = TransferService(self.db, self.ctx).complete(command)
        if mismatch:
            self.fault(batch, command, f"{mismatch}；载具位置已标为未知，需扫码重新定位", delivery="delivered")
            return
        command.state = "done"
        command.updated_at = now()

    def _confirm_abort(self, batch: Batch, command: Command, record) -> None:
        from ..models import Adapter
        from .transfer_service import TransferService

        batch.state = "aborted"
        superseded = 0
        for acting in self.commands.in_flight_for_batch(batch.id, MOTION):
            acting.state = "cancelled"
            acting.error = f"设备确认终止（{command.id}）"
            acting.updated_at = now()
            self._release(self.db.get(Adapter, acting.station_id) or record, acting)
            if acting.type == "transfer":
                # 搬到一半停下：板不在起点也不在终点，位置不可信
                TransferService(self.db, self.ctx).lost_by_command(acting, "转运途中终止，载具位置未知")
            superseded += 1
        self.audit.record(
            None, "设备确认终止", batch.id, before="终止中", after="已终止",
            command_id=command.id,
            detail=f"设备侧终止回执已确认；{superseded} 条在途动作指令随之结束",
        )

    @staticmethod
    def _release(record, command: Command) -> None:
        if record is not None and record.current_command_id == command.id:
            record.current_command_id = ""

    def _run_for(self, command: Command, batch: Batch):
        if command.step_run_id:
            return self.db.get(__import__("app.models", fromlist=["StepRun"]).StepRun, command.step_run_id)
        return self.runs.latest(batch.id, self._step_id(batch, command.step_index))

    def _step_id(self, batch: Batch, index: int) -> str:
        steps = normalize(batch.recipe_snapshot.get("steps") or [])
        if index < len(steps):
            return step_id_of(steps[index], index)
        return ""

    def check_hard_window(self, batch: Batch, command: Command) -> str | None:
        if command.step_index == 0:
            return None
        steps = normalize(batch.recipe_snapshot.get("steps") or [])
        hard = (steps[command.step_index] or {}).get("hard") or {}
        if "maxGapMin" not in hard:
            return None
        from ..domain import graph

        if graph.graph_mode(steps):
            # 依赖图：从最晚结束的前驱起算（设备前驱看检查点，其余看步骤实例的结束时刻）
            anchors = []
            for parent in graph.predecessors(steps)[command.step_index]:
                checkpoint = self.checkpoints.latest_for_step(batch.id, parent)
                if checkpoint is not None:
                    anchors.append(checkpoint.created_at)
                    continue
                ended = self.runs.latest(batch.id, step_id_of(steps[parent], parent))
                if ended is not None and ended.state == workflow.COMPLETED and ended.ended_at:
                    anchors.append(ended.ended_at)
            if not anchors:
                return "缺少前驱步骤的完成记录，无法验证硬时限"
            anchor = max(anchors)
        else:
            previous = self.checkpoints.latest_for_step(batch.id, command.step_index - 1)
            if not previous:
                return "缺少前一步检查点，无法验证硬时限"
            anchor = previous.created_at
        deadline = anchor + timedelta(minutes=float(hard["maxGapMin"]))
        if now() > deadline:
            overrun = (now() - deadline).total_seconds() / 60
            return f"硬时限 {hard['maxGapMin']} min 已被触碰（超出 {overrun:.0f} min），批次挂起"
        return None

    # ---------- 设备步骤完成 ----------

    def complete_device_step(self, batch: Batch, command: Command, result) -> None:
        """写检查点、遥测与回执事件。

        这里不推进下一步、不动样品质量、不按步骤比例扣库存——这三件以前都耦合在
        「步骤完成」里，现在分别由推进器、复核和库存事件负责。
        """
        steps = normalize(batch.recipe_snapshot.get("steps") or [])
        step = steps[command.step_index] if command.step_index < len(steps) else {}
        checkpoint = Checkpoint(
            batch_id=batch.id,
            command_id=command.id,
            step_index=command.step_index,
            step_run_id=command.step_run_id,
            state="done",
            payload={
                "station_id": command.station_id,
                "capability": command.capability,
                "params": command.params,
                "delivered": result.delivered or {},
                "device_ts": result.device_ts.isoformat(timespec="seconds") if result.device_ts else None,
                "quality": result.quality,
                "origin": result.origin,
                "finished_at": now().isoformat(timespec="seconds"),
            },
        )
        self.db.add(checkpoint)
        self.db.flush()
        command.checkpoint_id = checkpoint.id
        command.state = "done"
        command.updated_at = now()
        from .schedule_service import ScheduleService

        ScheduleService(self.db, self.ctx).release_unused(batch, command.step_index, now())

        self.record_telemetry(batch, command, step, result)
        from .consumption_service import ConsumptionService

        consumption = ConsumptionService(self.db, self.ctx).book(
            batch, command, result.delivered or {}, step_run_id=command.step_run_id,
        )
        self.audit.record(
            None, "步骤检查点", batch.id,
            before=f"步骤 {command.step_index + 1} 执行中", after="已完成",
            command_id=command.id, checkpoint_id=checkpoint.id,
            detail=(
                f"{step.get('name')} @ {command.station_id}；来源 {result.origin}；"
                + (
                    f"设备回报消耗入账 {consumption['booked']} 项"
                    + (f"、被拒 {consumption['rejected']} 项" if consumption["rejected"] else "")
                    if consumption["booked"] or consumption["rejected"]
                    else "设备未回报实际消耗；实际投料需由库存事件入账，不按步骤比例推算"
                )
            ),
        )
        # 回执只产生事件；状态转换与下一节点由推进器在自己的短事务里做
        from .workflow_service import WorkflowService

        workflow = WorkflowService(self.db, self.ctx)
        run = self._run_for(command, batch)
        run_id = run.id if run else ""
        event = workflow.device_ack(
            batch.id, run_id, command.id, "done",
            {"checkpoint_id": checkpoint.id, "origin": result.origin},
            org_id=batch.org_id,
        )
        self.db.commit()
        workflow.process_event(event.id)

    def record_telemetry(self, batch: Batch, command: Command, step: dict, result) -> None:
        """保存真实遥测；只有模拟适配器才生成模拟曲线。"""
        finished = result.device_ts or now()
        if result.origin != "simulation":
            if result.telemetry:
                for metric, value, setpoint in result.telemetry:
                    self.db.add(
                        Telemetry(
                            station_id=command.station_id,
                            batch_id=batch.id,
                            metric=metric,
                            setpoint=float(setpoint) if setpoint is not None else None,
                            value=float(value),
                            quality=result.quality,
                            origin=result.origin,
                            device_ts=finished,
                        )
                    )
            # 真实设备没有回传遥测就是“无数据”，不能用设定值合成一条真实曲线。
            return
        points = max(2, settings.telemetry_points_per_step)
        duration_min = float(step.get("dur") or 1)
        from ..domain import simulation

        for metric, setpoint in (step.get("params") or {}).items():
            if not isinstance(setpoint, (int, float)) or isinstance(setpoint, bool):
                continue
            series = simulation.telemetry_series(float(setpoint), f"{command.id}{metric}", points)
            for index, value in enumerate(series):
                offset_min = duration_min * (len(series) - 1 - index) / (len(series) - 1)
                self.db.add(
                    Telemetry(
                        station_id=command.station_id,
                        batch_id=batch.id,
                        metric=metric,
                        setpoint=float(setpoint),
                        value=value,
                        quality=result.quality,
                        origin=result.origin,
                        device_ts=finished - timedelta(minutes=offset_min),
                    )
                )

    # ---------- 异常 ----------

    def fault(
        self, batch: Batch | None, command: Command, reason: str, delivery: str = "delivered",
    ) -> None:
        command.state = "unknown"
        command.error = reason
        command.delivery_state = delivery
        command.updated_at = now()
        if not batch:
            return
        if batch.state not in {"done", "aborted"}:
            # 已结束的批次不被迟到的回执改回故障；指令本身仍记为结果未知并报警
            batch.state = "fault"
            batch.failure_reason = reason
            # 并行分支时恢复评估针对出问题的这一步，不是列表里最靠前的那一步
            if command.type not in {"hold", "abort"}:
                batch.current_step = command.step_index
            if not batch.held_at:
                batch.held_at = now()
        for dependent in self.commands.dependents_of(command.id):
            # 前置转运没成：等着它的设备动作不会再有投递的机会，撤回（恢复时重新生成）
            self.withdraw(dependent, f"前置指令 {command.id[:8]} 结果为 {command.state}，设备动作未投递")
        # 转运失败不改步骤实例：步骤本身还没开始，恢复时重新生成转运与动作
        run = self._run_for(command, batch) if command.type != "transfer" else None
        if run is not None and run.state in {"ready", "running", "pending"}:
            run.state = "unknown"
            run.reason = reason
            run.row_version = int(run.row_version or 0) + 1
        self.alarms.raise_alarm(
            severity=2, source_type="batch", source_id=batch.id, message=reason,
            response=(
                "到现场核实设备实态，在批次页对结果未知的指令下核查结论；"
                "异常原因消除后清除报警条件，再走恢复评估。"
            ),
            owner="操作员", origin="system", condition_key=f"command:{command.id}:fault",
        )
        self.audit.record(
            None, "指令结果未知", command.id, before="running", after="unknown",
            detail=f"{reason}；投递状态 {delivery}", command_id=command.id,
        )


class ExecutorLoop:
    """执行器进程的一轮。

    命令队列是跨组织的，但每条命令带 org_id：这里按命令上的组织构造受限系统上下文，
    不用一个能看所有数据的全局身份。
    """

    def __init__(self, db: Session):
        self.db = db
        self.commands = CommandRepository(db)
        self.adapters = AdapterRepository(db)
        self.capabilities = CapabilityRepository(db)

    def tick(
        self, limit: int = 50, simulate_heartbeat: bool = True, cleanup_files: bool = False,
        monitor_assets: bool = False,
    ) -> dict:
        from .monitoring_service import DeviceMonitor, ExecutorLiveness

        # 先报到：这一轮里的投递要看执行门，执行门要看执行器是否存活
        ExecutorLiveness(self.db).beat()
        if simulate_heartbeat:
            self.heartbeat_simulated()
        self.probe_devices()
        self.db.commit()
        monitor = DeviceMonitor(self.db)
        stations = monitor.stations()
        assets = monitor.calibrations() if monitor_assets else {"raised": 0, "cleared": 0}
        self.db.commit()
        mismatches = self.reconcile()
        polled = self.poll_running()
        overdue = self.check_timeouts()
        executed = self.execute_pending(limit)
        advanced = self.advance()
        files_cleaned = self.cleanup_orphan_files() if cleanup_files else 0
        telemetry_purged = self.purge_telemetry() if cleanup_files else 0
        return {
            "executed": executed,
            "polled": polled,
            "reconciled": mismatches,
            "advanced": advanced,
            "files_cleaned": files_cleaned,
            "telemetry_purged": telemetry_purged,
            "overdue": overdue["overdue"],
            "timed_out": overdue["timed_out"],
            "alarms_raised": stations["raised"] + assets["raised"],
            "alarms_cleared": stations["cleared"] + assets["cleared"],
            "at": now().isoformat(timespec="seconds"),
        }

    def execute_pending(
        self, limit: int = 50, station_id: str | None = None, dispatch_open: bool | None = None,
    ) -> int:
        """投递已到点的队列指令。执行门只算一次：门关着时动作指令不进候选。"""
        if dispatch_open is None:
            dispatch_open = bool(GateService(self.db).status()["open"])
        executed = 0
        for command in self.commands.pending(limit, dispatch_open=dispatch_open, station_id=station_id):
            service = ExecutionService(self.db, system_context(command.org_id, "执行器"))
            if service.execute(command):
                executed += 1
        self.db.commit()
        return executed

    def station_pass(self, station_id: str, *, limit: int = 50, dispatch_open: bool | None = None) -> dict:
        """一台工位的一轮设备侧工作：探测、对账、轮询、超时、投递。

        涉及设备 I/O 的步骤都在这里，并发执行器按工位把它们分给不同线程：一台设备的网关
        卡住只拖住它自己的线程，不拖慢其他工位，也不拖慢执行器心跳（心跳在控制回路里写）。
        同一工位同一时刻只有一个线程在处理，工位内的指令仍按原顺序串行。
        """
        probed = self.probe_devices(station_id=station_id)
        self.db.commit()
        reconciled = self.reconcile(station_id=station_id)
        polled = self.poll_running(station_id=station_id)
        overdue = self.check_timeouts(station_id=station_id)
        executed = self.execute_pending(limit, station_id=station_id, dispatch_open=dispatch_open)
        return {
            "probed": probed, "reconciled": reconciled, "polled": polled, "executed": executed,
            "overdue": overdue["overdue"], "timed_out": overdue["timed_out"],
        }

    def control_pass(self, *, simulate_heartbeat: bool = True, monitor_assets: bool = False) -> dict:
        """不碰设备网络的控制回路：执行器心跳、模拟心跳、设备监控报警。每轮必跑且很快。"""
        from .monitoring_service import DeviceMonitor, ExecutorLiveness

        ExecutorLiveness(self.db).beat()
        if simulate_heartbeat:
            self.heartbeat_simulated()
        self.db.commit()
        monitor = DeviceMonitor(self.db)
        stations = monitor.stations()
        assets = monitor.calibrations() if monitor_assets else {"raised": 0, "cleared": 0}
        self.db.commit()
        return {
            "alarms_raised": stations["raised"] + assets["raised"],
            "alarms_cleared": stations["cleared"] + assets["cleared"],
        }

    def stations_needing_work(self) -> set[str]:
        """本轮要派活的工位：有指令要处理的，加上到了探测周期的主动探测设备。"""
        from ..adapters.registry import probe_interval

        wanted = self.commands.stations_with_open_work()
        moment = now()
        for record in self.adapters.list():
            interval = probe_interval(record) if record.enabled else None
            if interval is None:
                continue
            fresh = (
                record.connected and record.last_heartbeat
                and (moment - record.last_heartbeat).total_seconds() < interval
            )
            if not fresh:
                wanted.add(record.station_id)
        return wanted

    def check_timeouts(self, station_id: str | None = None) -> dict:
        """在途指令的超时。

        设备卡死时轮询只会一直返回「执行中」，不支持查询的设备连这个都没有。按步骤预计时长
        的倍数判：超过「超时线」先报警，超过「硬上限」转结果未知、人工核查——不自动重试，
        也不假定设备已经停下。能力的恢复规则里声明了 `maxRunMin` 时以它为硬上限。
        """
        overdue = timed_out = 0
        moment = now()
        for command in self.commands.in_flight(station_id):
            if command.started_at is None:
                continue
            batch = self.db.get(Batch, command.batch_id)
            if batch is None:
                continue
            service = ExecutionService(self.db, system_context(command.org_id, "执行器超时检查"))
            steps = normalize(batch.recipe_snapshot.get("steps") or [])
            step = steps[command.step_index] if command.step_index < len(steps) else {}
            if command.type == "transfer":
                expected = float(settings.transfer_min)
                grace = settings.command_timeout_grace_min
                warn_after = expected * settings.command_overdue_factor + grace
                hard_after = expected * settings.command_hard_limit_factor + grace
            elif command.type in DISPATCHING:
                expected = float(step.get("dur") or 0)
                grace = settings.command_timeout_grace_min
                warn_after = expected * settings.command_overdue_factor + grace
                recovery = self.capabilities.recovery_of(command.capability) or {}
                hard_after = float(
                    recovery.get("maxRunMin")
                    or expected * settings.command_hard_limit_factor + grace
                )
            else:
                warn_after = hard_after = settings.control_command_timeout_min
            elapsed = (moment - command.started_at).total_seconds() / 60
            if elapsed > hard_after:
                ledger = service.executions.get(command.id)
                if ledger is not None:
                    ledger.state = "unknown"
                record = self.adapters.get(command.station_id)
                service.fault(
                    batch, command,
                    f"指令执行 {elapsed:.0f} min，超过最长 {hard_after:.0f} min；设备可能卡死，"
                    f"结果未知，转人工核查，不自动重试",
                    delivery="maybe_sent",
                )
                service._release(record, command)
                timed_out += 1
            elif elapsed > warn_after and command.overdue_at is None:
                command.overdue_at = moment
                service.alarms.raise_alarm(
                    severity=2, source_type="batch", source_id=batch.id,
                    message=(
                        f"第 {command.step_index + 1} 步 {command.type} 指令已执行 {elapsed:.0f} min，"
                        f"超过预计 {warn_after:.0f} min，设备仍未给出结论"
                    ),
                    response=f"到现场查看设备；超过 {hard_after:.0f} min 仍无结论将转结果未知、人工核查。",
                    owner="操作员", origin="system", condition_key=f"command:{command.id}:overdue",
                )
                overdue += 1
        self.db.commit()
        return {"overdue": overdue, "timed_out": timed_out}

    def purge_telemetry(self, batch_size: int = 50_000) -> int:
        """删除超过保留期的遥测点。分批删，避免一次长事务锁住遥测表。"""
        from sqlalchemy import delete, select

        cutoff = now() - timedelta(days=max(1, settings.telemetry_retention_days))
        removed = 0
        while True:
            ids = select(Telemetry.id).where(Telemetry.device_ts < cutoff).limit(batch_size)
            result = self.db.execute(delete(Telemetry).where(Telemetry.id.in_(ids)))
            self.db.commit()
            removed += result.rowcount or 0
            if (result.rowcount or 0) < batch_size:
                return removed

    def cleanup_orphan_files(self) -> int:
        """按组织清理过期暂存文件；系统身份仍受组织仓储过滤，不能跨域误删。"""
        org_ids = [
            row[0]
            for row in self.db.query(FileObject.org_id).distinct().order_by(FileObject.org_id).all()
            if row[0]
        ]
        removed = 0
        for org_id in org_ids:
            result = FileService(
                self.db, system_context(org_id, "文件清理任务")
            ).cleanup_orphans(
                older_than_hours=max(1, settings.file_orphan_retention_hours),
                limit=max(1, settings.file_cleanup_batch_size),
            )
            removed += result["count"]
        return removed

    def poll_running(self, station_id: str | None = None) -> int:
        """轮询已被真实设备接受的长任务；新命令在下一轮才查，避免紧密自旋。"""
        completed = 0
        for command in self.commands.in_flight(station_id):
            if command.delivery_state != "delivered":
                continue
            record = self.adapters.get(command.station_id)
            if record is None or not record.supports_query:
                continue
            service = ExecutionService(self.db, system_context(command.org_id, "执行器轮询"))
            batch = self.db.get(Batch, command.batch_id)
            if batch is None:
                continue
            try:
                result = adapter_for(record).query(command.id)
            except AdapterUnreachable:
                # 一次轮询超时不等于动作失败；保留 running，下一轮继续查。
                continue
            except Exception as exc:
                # 查询失败不能证明设备没在动作：保留占用，转人工核查
                service.fault(
                    batch, command, f"设备状态查询失败：{exc}；结果未知，转人工核查",
                    delivery="maybe_sent",
                )
                continue
            if result is None:
                # 设备曾确认接受，现在却查不到：动作是否发生不可知，不能放行续跑
                service.fault(
                    batch, command, "设备侧查不到已接受的命令，结果未知，转人工核查",
                    delivery="maybe_sent",
                )
                continue
            ledger = service.executions.get(command.id)
            if ledger is None:
                continue
            finished = result.state not in {"accepted", "running"}
            service.settle(batch, command, ledger, record, result)
            if finished and result.state == "done":
                completed += 1
        self.db.commit()
        return completed

    def probe_devices(self, station_id: str | None = None) -> int:
        """主动探测真实设备的在线状态。

        SiLA 2 / Modbus TCP / OPC UA 设备不会往系统推心跳：由执行器按周期读取设备身份，读到了才算
        在线，并同步设备自报的联锁与是否接受指令。探测失败只标失联，不编造心跳。心跳方式可在适配器配置里用
        `heartbeat_mode` 指定：`probe`（sila2_v1 / modbus_tcp_v1 / opcua_v1 默认）或 `push`（设备自己上报，
        http_json_v1 默认）。
        """
        from ..adapters.registry import probe_interval

        probed = 0
        moment = now()
        for record in self.adapters.list():
            if station_id is not None and record.station_id != station_id:
                continue
            interval = probe_interval(record) if record.enabled else None
            if interval is None:
                continue
            if record.connected and record.last_heartbeat and (moment - record.last_heartbeat).total_seconds() < interval:
                continue
            probed += 1
            try:
                health = adapter_for(record).healthcheck()
            except AdapterUnreachable as exc:
                record.connected = False
                record.note = f"探测失败：{exc}"[:500]
                continue
            except (AdapterError, NotImplementedError) as exc:
                # 身份不符、正式环境里的模拟器：设备在线也不能用
                record.connected = False
                record.accepts_commands = False
                record.note = f"探测拒绝：{exc}"[:500]
                continue
            record.connected = True
            record.last_heartbeat = moment
            record.site_interlock = bool(health.get("interlock"))
            record.accepts_commands = bool(health.get("accepts_commands", True))
            record.note = f"探测在线：{health.get('device_id', '')}"
        return probed

    def heartbeat_simulated(self) -> None:
        """只给模拟适配器补心跳。真实设备的在线状态必须由它自己上报。"""
        timestamp = now()
        for record in self.adapters.list():
            if record.kind == "simulation" and record.connected:
                record.last_heartbeat = timestamp

    def reconcile(self, station_id: str | None = None) -> int:
        """重启对账。

        对可能已发出但未确认的命令，先按原 command_id 问设备侧；能确认就按确认的结论走，
        不能可靠查询就留在结果未知并转人工核查——不生成新命令盲目重试。
        """
        mismatches = 0
        for command in self.commands.maybe_sent(station_id):
            record = self.adapters.get(command.station_id)
            service = ExecutionService(self.db, system_context(command.org_id, "执行器对账"))
            batch = self.db.get(Batch, command.batch_id)
            if record is None:
                service.fault(batch, command, "对账时找不到适配器，结果未知", delivery="unreachable")
                mismatches += 1
                continue
            if not record.supports_query:
                service.fault(
                    batch, command,
                    "该适配器不支持可靠状态查询，也不支持设备端去重；"
                    "命令结果未知，已转人工核查，不重发",
                    delivery="maybe_sent",
                )
                mismatches += 1
                continue
            try:
                adapter = adapter_for(record)
                found = adapter.query(command.id)
            except Exception as exc:
                service.fault(batch, command, f"对账查询失败：{exc}", delivery="maybe_sent")
                mismatches += 1
                continue
            if found is None:
                service.fault(
                    batch, command,
                    "设备侧查不到该命令，可能未送达也可能已执行；结果未知，转人工核查",
                    delivery="maybe_sent",
                )
                mismatches += 1
                continue
            ledger = service.executions.get(command.id)
            if batch is None or ledger is None:
                service.fault(batch, command, "对账时缺少批次或幂等台账，结果未知", delivery="maybe_sent")
                mismatches += 1
                continue
            # 设备侧能按原 command_id 给出结论：复用原命令身份继续，不重复动作
            command.delivery_state = "delivered"
            service.settle(batch, command, ledger, record, found)
            if found.state not in {"accepted", "running", "done"}:
                mismatches += 1
        # 在途命令与设备侧当前命令不一致同样挂起
        for command in self.commands.in_flight(station_id):
            if command.type not in MOTION:
                continue  # 保持 / 终止不占用工位的「当前指令」
            station = self.db.get(Station, command.station_id)
            if station is not None and (station.channels or 1) > 1:
                continue  # 多通道设备同时有多条在途指令，「当前指令」只记最近一条，不能据此判不一致
            record = self.adapters.get(command.station_id)
            if record and record.current_command_id == command.id:
                continue
            if command.delivery_state == "maybe_sent":
                continue
            batch = self.db.get(Batch, command.batch_id)
            ExecutionService(self.db, system_context(command.org_id, "执行器对账")).fault(
                batch, command, "执行器重启对账不一致，需人工核查", delivery="maybe_sent"
            )
            mismatches += 1
        self.db.commit()
        return mismatches

    def advance(self) -> int:
        """推进到期等待与待处理事件。无浏览器请求也要能往前走。"""
        from ..models import WorkflowEvent
        from .workflow_service import WorkflowService

        org_ids = {
            row[0]
            for row in self.db.query(WorkflowEvent.org_id)
            .filter(WorkflowEvent.state.in_(["pending", "processing"]))
            .distinct()
            .all()
        }
        org_ids |= {
            row[0]
            for row in self.db.query(Batch.org_id)
            .filter(Batch.state.notin_(["done", "aborted"]))
            .distinct()
            .all()
        }
        processed = 0
        for org_id in {value for value in org_ids if value}:
            workflow = WorkflowService(self.db, system_context(org_id, "后台推进器"))
            processed += workflow.tick().get("processed", 0)
        return processed
