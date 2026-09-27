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
from ..domain import dataquality, workflow
from ..domain.steps import DEVICE, kind_of, normalize, step_id_of
from ..models import AdapterExecution, Batch, Checkpoint, Command, FileObject, Station, Telemetry
from ..repositories.batches import AllocationRepository, BatchRepository, SampleRepository
from .telemetry import context as telemetry_context
from ..repositories.execution import (
    DISPATCHING, MOTION, AdapterExecutionRepository, CheckpointRepository, CommandRepository, still_occupying,
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
        if command.type in MOTION:
            unavailable = self.station_blocker(batch, command)
            if unavailable:
                self._refuse(batch, command, unavailable)
                return True
            if self._occupied(command) or self._awaiting_clean(batch, command):
                # 通道或资产被占满（在途、已保持、结果未知的动作都算），或上一批用过还没确认清洗：排队等
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

    def _occupied(self, command: Command) -> bool:
        """工位通道或所属资产此刻是否已被占满。

        占用按设备侧事实算，不按计划时间窗：在途、已保持、结果未知且可能已送达的动作都占着设备，
        计划时间到了只是投递的必要条件之一。工位按通道数计，多个工位共用的资产按容量计，
        维护 / 校准预约占满整台资产。续跑 / 重试接续的是它自己针对的那个被保持的动作，不另占一份。
        """
        from ..core.db import serialize

        stations = [
            station for station in (
                self.db.get(Station, station_id)
                for station_id in dict.fromkeys([command.station_id, *(command.assist_station_ids or [])])
            )
            if station is not None
        ]
        # 同一台设备上「数占用 → 领取」串行：并发执行器不会各自看到同一个空位；锁随领取的提交释放。
        # 协同资源与主设备一起取得，按固定顺序加锁，两条指令交叉等待时不会死锁
        for key in sorted({station.asset_id or station.id for station in stations}):
            serialize(self.db, f"occupancy:{key}")
        exempt = {command.id, command.target_command_id}
        return any(self._station_full(station, exempt) for station in stations)

    def _station_full(self, station: Station, exempt: set[str]) -> bool:
        from ..models import Asset

        on_station = [c for c in self.commands.occupying([station.id]) if c.id not in exempt]
        if len(on_station) >= max(1, int(station.channels or 1)):
            return True
        if not station.asset_id:
            return False
        asset = self.db.get(Asset, station.asset_id)
        capacity = max(1, int(asset.capacity or 1)) if asset is not None else 1
        mapped = [row[0] for row in self.db.query(Station.id).filter(Station.asset_id == station.asset_id).all()]
        load = len([c for c in self.commands.occupying(mapped) if c.id not in exempt])
        return load + self._booked_now(station.asset_id, capacity) >= capacity

    def _booked_now(self, asset_id: str, capacity: int) -> int:
        """此刻压在资产上的人工 / 维护 / 校准预约份数。排程产生的占用由指令本身表达，不重复计。"""
        from ..models import ResourceBooking

        moment = now()
        units = 0
        for row in self.db.query(ResourceBooking).filter(
            ResourceBooking.asset_id == asset_id,
            ResourceBooking.state.in_(["pending", "confirmed"]),
            ResourceBooking.starts_at <= moment,
            ResourceBooking.ends_at > moment,
        ).all():
            if row.kind == "schedule":
                continue
            units += capacity if row.kind in {"maintenance", "calibration"} else 1
        return units

    def station_blocker(self, batch: Batch, command: Command) -> str:
        """投递前再核一次工位与资产此刻能不能用。

        长流程只在开跑时查一次不够：工位停用或故障、资产转入维护、校准在执行区间内到期都可能中途发生。
        不满足就不投递（设备没见过这条指令），批次挂起报警，原因消除后可以直接重新下发。
        """
        from ..domain.resources import Window, calibration_blockers
        from ..models import Asset

        for helper_id in command.assist_station_ids or []:
            helper = self.db.get(Station, helper_id)
            problem = self._helper_blocker(helper, helper_id)
            if problem:
                return f"协同资源{problem}；动作指令未投递，不自动重试"
        station = self.db.get(Station, command.station_id)
        if station is None:
            return ""
        if station.retired:
            return f"工位 {station.id} 已停用；动作指令未投递，不自动重试"
        if station.status in {"fault", "offline"}:
            label = "故障" if station.status == "fault" else "离线"
            return f"工位 {station.id} 处于{label}状态；动作指令未投递，不自动重试"
        asset = self.db.get(Asset, station.asset_id) if station.asset_id else None
        if asset is None:
            return ""
        if asset.state in {"maintenance", "retired"}:
            label = "处于维护状态" if asset.state == "maintenance" else "已退役"
            return f"工位 {station.id} 所属资产 {asset.asset_no} {label}；动作指令未投递，不自动重试"
        if command.type not in DISPATCHING:
            return ""
        steps = normalize(batch.recipe_snapshot.get("steps") or [])
        step = steps[command.step_index] if command.step_index < len(steps) else {}
        moment = now()
        window = Window(moment, moment + timedelta(minutes=max(0.0, float(step.get("dur") or 0))))
        problems = calibration_blockers(self._asset_spec(asset), command.capability, window)
        return f"{problems[0]}；动作指令未投递，不自动重试" if problems else ""

    def _helper_blocker(self, helper: Station | None, helper_id: str) -> str:
        """协同工位此刻能不能用：不存在、停用、故障 / 离线、所属资产维护或退役都不行。"""
        from ..models import Asset

        if helper is None:
            return f" {helper_id} 不存在"
        if helper.retired:
            return f" {helper.id} 已停用"
        if helper.status in {"fault", "offline"}:
            return f" {helper.id} 处于{'故障' if helper.status == 'fault' else '离线'}状态"
        asset = self.db.get(Asset, helper.asset_id) if helper.asset_id else None
        if asset is not None and asset.state in {"maintenance", "retired"}:
            return f" {helper.id} 所属资产 {asset.asset_no} {'处于维护状态' if asset.state == 'maintenance' else '已退役'}"
        return ""

    def _asset_spec(self, asset):
        """资产的校准规格。资产与工位一样是跨组织共享的物理对象，校准记录不按组织过滤。"""
        from ..domain.resources import AssetSpec, CalibrationSpec
        from ..models import CalibrationRecord

        return AssetSpec(
            asset_id=asset.id, name=f"{asset.asset_no} {asset.name}", state=asset.state,
            capacity=max(1, asset.capacity), calibration_applicable=asset.calibration_applicable,
            calibration_exempt_reason=asset.calibration_exempt_reason,
            calibrations=tuple(
                CalibrationSpec(
                    effective_from=row.effective_from, expires_at=row.expires_at, result=row.result,
                    capability_scope=tuple(row.capability_scope or []),
                )
                for row in self.db.query(CalibrationRecord).filter(CalibrationRecord.asset_id == asset.id).all()
            ),
        )

    def _awaiting_clean(self, batch: Batch, command: Command) -> bool:
        """用过、需要清洗的设备在确认清洗之前不给别的批次用；同一批次的后续步骤可以接着用。"""
        station = self.db.get(Station, command.station_id)
        if station is None or station.clean or station.dirty_batch_id == batch.id:
            return False
        who = f"批次 {station.dirty_batch_id} 用后" if station.dirty_batch_id else "标为未清洗，"
        self.alarms.raise_alarm(
            severity=3, source_type="batch", source_id=batch.id,
            message=f"{station.id} {who}待清洗确认；{batch.id} 第 {command.step_index + 1} 步排队等待",
            response="完成清洗后在工位页确认就绪（已清洗），排队的动作随即投递",
            owner="操作员", origin="system", condition_key=f"station:{station.id}:awaiting_clean",
        )
        return True

    def _soil(self, batch: Batch, command: Command) -> None:
        """需要清洗的动作做过（或可能做过）之后，工位转为待清洗，记下用过它的批次。"""
        if command.type not in DISPATCHING:
            return
        if not (self.capabilities.recovery_of(command.capability) or {}).get("cleanAfter"):
            return
        station = self.db.get(Station, command.station_id)
        if station is None:
            return
        station.clean = False
        station.dirty_batch_id = batch.id
        # 版本加一：界面上拿着旧版本的「已清洗」确认不能把这次弄脏覆盖掉
        station.row_version = int(station.row_version or 0) + 1

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
        target = command.target_command_id
        if command.type in {"hold", "abort"}:
            if not target:
                # 早先生成、没记目标的控制指令：取这台设备上本批次在途的动作
                acting = [
                    c for c in self.commands.in_flight_for_batch(batch.id, MOTION)
                    if c.station_id == command.station_id
                ]
                target = acting[0].id if acting else ""
            else:
                aimed = self.db.get(Command, target)
                if aimed is None or not still_occupying(aimed):
                    # 目标动作已经结束（完成、确认未执行或已取消）：设备上没有要停的动作，不再发给设备
                    self._settle_moot(batch, command, ledger, record)
                    return
        request = CommandRequest(
            command_id=command.id, station_id=command.station_id, capability=command.capability,
            params=command.params or {}, type=command.type, batch_id=batch.id,
            step_index=command.step_index,
            step_id=self._step_id(batch, command.step_index),
            target_command_id=target, method=dict(command.method or {}),
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
                environment = self.check_environment(batch, command)
                if environment:
                    # 设备确定没见过这条指令：按「未投递」记，环境恢复后可以直接重新下发，不用现场核查
                    ledger.state = "rejected"
                    command.started_at = None
                    self.fault(batch, command, environment, delivery="unreachable")
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

    def _settle_moot(self, batch: Batch, command: Command, ledger: AdapterExecution, record) -> None:
        """控制指令的目标已经结束：不调用设备，直接按「目标已不在动作」确认。"""
        from ..adapters.base import CommandResult

        command.delivery_state = "not_sent"
        self.settle(batch, command, ledger, record, CommandResult(
            command_id=command.id, state="done", device_ts=now(), origin="system",
            delivered={"note": f"目标动作 {command.target_command_id[:8]} 已结束，无需设备侧操作"},
        ))

    def settle(self, batch: Batch, command: Command, ledger: AdapterExecution, record, result) -> None:
        """把一份设备回执落到指令、台账与批次上。投递与轮询共用同一套结论。"""
        ledger.result = result.as_dict()
        ledger.updated_at = now()
        if result.state in {"accepted", "running"}:
            ledger.state = result.state
            command.state = "running"
            command.updated_at = now()
            self._take_over(command)
            return
        self._release(record, command)
        if command.overdue_at is not None:
            self.alarms.resolve_condition(
                f"command:{command.id}:overdue", f"指令 {command.id} 已给出结论 {result.state}",
            )
        if result.state != "done":
            ledger.state = result.state
            # 设备报失败也可能已经动过：需要清洗的设备照样转为待清洗
            self._soil(batch, command)
            self.fault(
                batch, command, result.error or f"设备回执状态 {result.state}",
                delivery="delivered",
            )
            return
        ledger.state = "done"
        if command.type in DISPATCHING:
            self._take_over(command)
            self.complete_device_step(batch, command, result)
            return
        if command.type == "transfer":
            self._complete_transfer(batch, command, record)
            return
        command.state = "done"
        command.updated_at = now()
        if command.type == "hold":
            self.confirm_hold(batch, command)
        if command.type == "abort":
            self._confirm_abort(batch, command, record)

    def _control_target(self, batch: Batch, command: Command) -> list[Command]:
        """一条保持 / 终止指令确认的是哪些动作：记了目标就只是目标；旧指令按这台设备上本批次可能在动作的算。"""
        if command.target_command_id:
            target = self.db.get(Command, command.target_command_id)
            return [target] if target is not None and still_occupying(target) else []
        return [
            row for row in self.commands.possibly_acting(batch.id, MOTION)
            if row.station_id == command.station_id
        ]

    def confirm_hold(self, batch: Batch, command: Command) -> None:
        """设备确认保持：被保持的动作记为「已保持」。它仍占着设备，续跑或终止之前不释放。"""
        for target in self._control_target(batch, command):
            if target.state in {"accepted", "running"}:
                target.state = "held"
                target.updated_at = now()
                self.audit.record(
                    None, "设备确认保持", batch.id, before="执行中", after="已保持", command_id=command.id,
                    detail=f"{target.station_id} 第 {target.step_index + 1} 步动作 {target.id[:8]} 停在保持状态",
                )

    def _take_over(self, command: Command) -> None:
        """续跑 / 重试被设备接受：它接续的那个被保持的动作到此为止，之后只由新指令给出结论。

        设备上同一个动作被两个指令号指着：不把旧指令结束，轮询会让同一个动作再完成一次、再记一次检查点。
        """
        if command.type not in DISPATCHING or not command.target_command_id:
            return
        target = self.db.get(Command, command.target_command_id)
        if target is None or target.state not in {"held", "accepted", "running"}:
            return
        target.state = "superseded"
        target.error = f"由{'续跑' if command.type == 'resume' else '重试'}指令 {command.id[:8]} 接续"
        target.updated_at = now()

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
        """一台设备确认终止：只结束这条终止指令针对的动作、只释放这台设备。

        别的设备上的动作要等它们各自的终止确认（或现场核查）：一台设备停了不代表整批都停了。
        全部可能在动作的目标都有了结论，批次才转为已终止并释放残余。
        """
        from ..models import Adapter
        from .transfer_service import TransferService

        stopped = self._control_target(batch, command)
        for acting in stopped:
            delivered = acting.delivery_state in {"maybe_sent", "delivered"}
            acting.state = "cancelled"
            acting.error = f"设备确认终止（{command.id}）"
            acting.updated_at = now()
            self._release(self.db.get(Adapter, acting.station_id) or record, acting)
            if acting.type == "transfer":
                # 搬到一半停下：板不在起点也不在终点，位置不可信
                TransferService(self.db, self.ctx).lost_by_command(acting, "转运途中终止，载具位置未知")
            elif delivered:
                self._soil(batch, acting)
        pending = [
            row for row in self.commands.for_batch(batch.id)
            if row.type == "abort" and row.id != command.id
            and row.state in {"sent", "accepted", "running", "unknown", "manual"}
        ]
        acting_left = self.commands.possibly_acting(batch.id, MOTION)
        if pending or acting_left:
            waiting = sorted({row.station_id for row in pending} | {row.station_id for row in acting_left})
            self.audit.record(
                None, "设备确认终止", batch.id, before="终止中", after="部分设备已停止",
                command_id=command.id,
                detail=(
                    f"{command.station_id} 终止回执已确认，{len(stopped)} 条动作随之结束；"
                    f"仍等待 {'、'.join(waiting)} 确认停止，批次暂不终止"
                ),
            )
            return
        batch.state = "aborted"
        closed = 0
        for row in self.commands.for_batch(batch.id):
            if row.id != command.id and row.state in {"unknown", "manual"}:
                # 设备没见过或已明确失败的指令：随批次终止结束，不再挂在结果未知清单里
                row.state = "cancelled"
                row.error = (row.error + "；" if row.error else "") + "随批次终止结束"
                row.updated_at = now()
                closed += 1
        from .workflow_service import WorkflowService

        WorkflowService(self.db, self.ctx).close_out(batch)
        self.audit.record(
            None, "设备确认终止", batch.id, before="终止中", after="已终止",
            command_id=command.id,
            detail=(
                f"设备侧终止回执已确认；{len(stopped)} 条动作指令随之结束，全部设备都已确认停止"
                + (f"；{closed} 条未送达的指令随批次结束" if closed else "")
            ),
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

    def check_environment(self, batch: Batch, command: Command) -> str | None:
        """设备步骤投递前再核对一次环境要求：长批次里环境可能中途变坏。不满足就不投递、挂起报警。"""
        if command.type not in {"dispatch", "retry", "resume"}:
            return None
        from .environment_service import EnvironmentService

        problems = EnvironmentService(self.db, self.ctx).step_problems(batch, command.step_index)
        return f"环境条件不满足，指令未投递：{'；'.join(problems)}" if problems else None

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
        # 人工核查确认续跑 / 重试已执行时（没有经过设备接受回执），被接续的那个保持中的动作同样到此为止
        self._take_over(command)
        if command.step_run_id:
            for sibling in self.commands.for_step_run(command.step_run_id):
                if sibling.id != command.id and sibling.state == "held":
                    sibling.state = "superseded"
                    sibling.error = f"由指令 {command.id[:8]} 完成本步"
                    sibling.updated_at = now()
        self._soil(batch, command)
        from .schedule_service import ScheduleService

        ScheduleService(self.db, self.ctx).release_unused(batch, command.step_index, now())

        self.record_telemetry(batch, command, step, result)
        self.check_outputs(batch, command, step, checkpoint, result.delivered or {})
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

    def check_outputs(self, batch: Batch, command: Command, step: dict, checkpoint: Checkpoint, delivered: dict) -> list[dict]:
        """设备回报对照设备方法的输出规则：缺必报项、越界都打标并报一条数据异常报警。

        不阻断流程——值是设备真实回报的，要不要剔除由数据审核（或下游质检关卡）决定。
        """
        outputs = ((step.get("method") or {}).get("outputs")) or []
        flags = dataquality.output_flags(outputs, delivered)
        if not flags:
            return []
        checkpoint.payload = {**(checkpoint.payload or {}), "flags": flags}
        run = self._run_for(command, batch)
        if run is not None:
            run.flags = [*(run.flags or []), *flags]
        method = step.get("method") or {}
        self.alarms.raise_alarm(
            3, "batch", batch.id,
            f"{batch.id} 第 {command.step_index + 1} 步「{step.get('name')}」设备回报与方法 "
            f"{method.get('code', '')} v{method.get('version', '')} 输出规则不符：{flags[0]['message']}"
            + (f" 等 {len(flags)} 项" if len(flags) > 1 else ""),
            response="数据已照常入库并打标；在数据审核里确认是否剔除，必要时从该步骤重做",
            condition_key=f"data:{batch.id}:{command.id}:outputs",
        )
        return flags

    def record_telemetry(self, batch: Batch, command: Command, step: dict, result) -> None:
        """保存真实遥测；只有模拟适配器才生成模拟曲线。每个点带指令、步骤、值守人与（能确定时的）样本。"""
        finished = result.device_ts or now()
        owner = telemetry_context(self.db, batch, command)
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
                            **owner,
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
                        **owner,
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
        alarm = self.alarms.raise_alarm(
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
        if batch.state not in {"done", "aborted"}:
            # 异常引擎：登记影响面，按策略库决定——指令从未送达设备时才可能自动重试 / 改派 / 跳过
            from .exception_service import ExceptionService

            event = ExceptionService(self.db, self.ctx).on_command_fault(batch, command, reason, delivery)
            if event is not None:
                event.alarm_id = alarm.id


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
        from .integration_service import deliver_due

        webhooks = deliver_due(self.db)
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
            "webhooks_sent": webhooks["sent"],
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
                # 超时不等于设备已停下：保留占用（含工位上的当前指令），现场核查给出结论后才释放
                service.fault(
                    batch, command,
                    f"指令执行 {elapsed:.0f} min，超过最长 {hard_after:.0f} min；设备可能卡死，"
                    f"结果未知，转人工核查，不自动重试",
                    delivery="maybe_sent",
                )
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
                # 这条指令已经交给过适配器（maybe_sent）：可能已送达，不能标成「未送达」
                service.fault(batch, command, "对账时找不到适配器，结果未知", delivery="maybe_sent")
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
