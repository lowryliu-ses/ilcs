"""测试环境的管理员级联强制删除。

正常的删除只删「没被引用过的草稿」（见 domain/lifecycle.py）：用过的对象是历史的一部分，只能停用、
退役、报废。测试 / 演示环境里联调数据堆多了，需要把一整串连根删掉——这里就是那条路，只在
ILCS_ADMIN_FORCE_DELETE 开启、非正式环境、且操作人是系统管理员时成立（正式环境开启即判配置不合格）。

删一个对象就把**依赖它的数据**一起删掉，规则只有一条：引用它、离开它就讲不通的记录跟着删；
只是顺带提到它的记录把引用清空后保留。

- 能力 → 用它的设备方法、步骤用它的流程；工位上声明的能力极限、人员的能力资质一并去掉
- 设备方法 → 引用它的流程
- 流程 → 它的修订、把它当子方法调用的流程、用它的方案与批次、按它建的方案模板
- 工位 → 在它上面排过、跑过的批次；它的设备连接、接入验收、点位写入、遥测、预约
- 方案 → 它的实验任务、批次、版本、提案与分析运行
- 实验任务 → 它的子任务与批次
- 批次 → 样品、步骤、指令与检查点、遥测、检测任务与结果、报告、报警、预约与时间窗
- 资产 → 校准、预约、维护单；工位与工步时间窗上的资产关联清空
- 批号 → 预留与它的库存流水；设备模板 → 工位连接上的模板关联清空；废液桶 → 只删登记

保留的：审计记录与电子签名（只追加，删除动作本身也记一条）、库存流水里的批次号（账面库存是实物，
不随批次删除回滚，预留删了未耗用的占用就归还）、登记来的实物样品（配方表登记的样品不是批次生成的）。
正在执行（运行中、保持、终止中，或还有在设备上的指令）的批次不删：先终止。

预览与执行走同一段代码：预览在保存点里删一遍、数完再回滚，看到的条数就是执行时的条数。
"""
from __future__ import annotations

from collections import OrderedDict
from typing import Any, Iterable

from sqlalchemy.orm import Session

from ..core.config import settings
from ..core.context import AccessContext
from ..core.errors import NotFound, PermissionDenied, StateConflict, ValidationFailed
from ..domain import methods as method_rules
from ..domain import subflow
from ..domain.liveness import LIVE_COMMAND_STATES
from ..domain.permissions import ADMIN
from ..domain.steps import normalize
from ..models import (
    AcceptanceRun, Adapter, AdapterExecution, Alarm, Allocation, AnalysisRun, AnalysisTask, Asset, Batch,
    BatchSignal, CalibrationRecord, Capability, Checkpoint, Command, Comment, DatasetSnapshot, DeviceMethod,
    DeviceTemplate, ExceptionEvent, ExperimentTask, IngestEvent, InventoryEvent, InventoryLedger, Labware,
    LabwareMove, Location, Lot, MaintenanceOrder, PersonBooking, PhysicalSample, Plan, PlanBatchLink,
    PlanProposal, PlanTemplate, PlanVersion, PointWrite, Qualification, Recipe, Report, ReportVersion,
    Reservation, ResourceBooking, Result, ResultReview, ResultValue, Sample, SampleTransfer, ScheduleProposal,
    ServiceIdentity, SlotOccupancy, Station, StepAdvance, StepRun, TaskAssignment, Telemetry, User,
    WasteTank, WorkflowEvent, roles_of,
)
from .audit_service import AuditService
from .identity_service import IdentityService

KINDS = OrderedDict([
    ("batch", "批次"), ("task", "实验任务"), ("plan", "实验方案"), ("recipe", "流程"), ("method", "设备方法"),
    ("template", "设备模板"), ("station", "工位"), ("asset", "资产"), ("capability", "能力"),
    ("lot", "批号"), ("waste", "废液桶"),
])
# 这些状态的批次还在执行，删除会和执行器抢同一批行
ACTIVE_BATCH_STATES = {"running", "held", "aborting"}
SIGN_MEANING = "强制删除"


def force_delete_enabled(user: User | None) -> bool:
    return bool(
        user is not None and settings.admin_force_delete and settings.environment != "production"
        and ADMIN in roles_of(user)
    )


def _in(column, values: Iterable[str]):
    return column.in_(sorted(values))


class ForceDeleteService:
    def __init__(self, db: Session, ctx: AccessContext):
        self.db = db
        self.ctx = ctx
        self.audit = AuditService(db, ctx)

    # ---------- 对外 ----------

    def preview(self, kind: str, object_id: str, user: User) -> dict:
        self._guard(user)
        closure = self._closure(kind, object_id)
        blockers = self._blockers(closure)
        savepoint = self.db.begin_nested()
        try:
            counts = self._delete(closure)
        finally:
            savepoint.rollback()
        return self._out(kind, object_id, closure, counts, blockers)

    def execute(self, kind: str, object_id: str, reason: str, signature_id: str, user: User) -> dict:
        self._guard(user)
        if not (reason or "").strip():
            raise ValidationFailed("写明为什么要强制删除", code="force_delete_reason_required")
        closure = self._closure(kind, object_id)
        blockers = self._blockers(closure)
        if blockers:
            raise StateConflict("还不能删除", {"blocked": [{"key": kind, "label": text} for text in blockers]})
        signature = IdentityService(self.db, self.ctx).consume_signature(
            signature_id, user, SIGN_MEANING, object_ref=f"{kind}:{object_id}", strict=True,
        )
        counts = self._delete(closure)
        summary = "；".join(f"{label} {count}" for label, count in counts.items() if count)
        self.audit.record(
            user, "强制删除", f"{KINDS[kind]} {object_id}", sign=True, meaning=signature.meaning,
            signature_id=signature.id, before="存在", after="已级联删除",
            detail=f"测试环境 ILCS_ADMIN_FORCE_DELETE；原因：{reason.strip()}；删除：{summary or '仅对象本身'}",
        )
        self.db.commit()
        return self._out(kind, object_id, closure, counts, [])

    # ---------- 守卫 ----------

    def _guard(self, user: User) -> None:
        if not force_delete_enabled(user):
            raise PermissionDenied("强制删除只在测试环境开启 ILCS_ADMIN_FORCE_DELETE 后对系统管理员开放")

    def _blockers(self, closure: dict[str, set[str]]) -> list[str]:
        if not closure["batch"]:
            return []
        batches = self.db.query(Batch).filter(_in(Batch.id, closure["batch"])).all()
        reasons = [
            f"批次 {batch.id} 正在{'运行' if batch.state == 'running' else '保持' if batch.state == 'held' else '终止'}，先终止再删除"
            for batch in batches if batch.state in ACTIVE_BATCH_STATES
        ]
        live = (
            self.db.query(Command)
            .filter(_in(Command.batch_id, closure["batch"]))
            .filter(Command.state.in_(LIVE_COMMAND_STATES + ("sent",)))
            .all()
        )
        for command in live:
            reasons.append(f"批次 {command.batch_id} 的指令还在设备上（{command.station_id} {command.state}），先终止批次")
        return reasons

    # ---------- 级联范围 ----------

    def _root(self, kind: str, object_id: str) -> None:
        model = {
            "batch": Batch, "task": ExperimentTask, "plan": Plan, "recipe": Recipe, "method": DeviceMethod,
            "template": DeviceTemplate, "station": Station, "asset": Asset, "capability": Capability,
            "lot": Lot, "waste": WasteTank,
        }.get(kind)
        if model is None:
            raise ValidationFailed(f"不支持强制删除的对象类型：{kind}")
        row = self.db.get(model, object_id)
        org = getattr(row, "org_id", None) if row is not None else None
        if row is None or (org not in (None, "") and org != self.ctx.org_id):
            raise NotFound(f"{KINDS[kind]} {object_id} 不存在")

    def _closure(self, kind: str, object_id: str) -> dict[str, set[str]]:
        """从根对象出发，把依赖它的对象一层层收进来，直到不再增加。"""
        self._root(kind, object_id)
        closure: dict[str, set[str]] = {key: set() for key in KINDS}
        closure[kind].add(str(object_id))
        org = self.ctx.org_id
        recipes = self.db.query(Recipe).filter(Recipe.org_id == org).all()
        changed = True
        while changed:
            before = sum(len(ids) for ids in closure.values())
            caps, methods, flows = closure["capability"], closure["method"], closure["recipe"]
            if caps:
                closure["method"] |= {
                    row.id for row in self.db.query(DeviceMethod).filter(
                        DeviceMethod.org_id == org, _in(DeviceMethod.capability_id, caps))
                }
                closure["recipe"] |= {
                    recipe.id for recipe in recipes
                    if any((step or {}).get("cap") in caps for step in recipe.steps or [] if isinstance(step, dict))
                }
            if methods:
                closure["recipe"] |= {
                    recipe.id for recipe in recipes
                    if set(method_rules.references(normalize(recipe.steps or []))) & methods
                }
            if flows:
                closure["recipe"] |= {recipe.id for recipe in recipes if recipe.parent in flows}
                closure["recipe"] |= {
                    recipe.id for recipe in recipes if set(subflow.references(recipe.steps or [])) & flows
                }
                closure["plan"] |= _ids(self.db.query(Plan.id).filter(Plan.org_id == org, _in(Plan.recipe_id, flows)))
                closure["batch"] |= _ids(self.db.query(Batch.id).filter(Batch.org_id == org, _in(Batch.recipe_id, flows)))
            if closure["station"]:
                stations = closure["station"]
                for model in (Command, Allocation, StepRun, Sample):
                    closure["batch"] |= _ids(self.db.query(model.batch_id).filter(_in(model.station_id, stations)))
            if closure["plan"]:
                plans = closure["plan"]
                closure["task"] |= _ids(self.db.query(ExperimentTask.id).filter(_in(ExperimentTask.plan_id, plans)))
                closure["batch"] |= _ids(self.db.query(Batch.id).filter(_in(Batch.plan_id, plans)))
            if closure["task"]:
                tasks = closure["task"]
                closure["task"] |= _ids(self.db.query(ExperimentTask.id).filter(_in(ExperimentTask.parent_id, tasks)))
                closure["batch"] |= _ids(self.db.query(Batch.id).filter(_in(Batch.task_id, tasks)))
                closure["batch"] |= {
                    row.batch_id for row in self.db.query(ExperimentTask).filter(_in(ExperimentTask.id, tasks))
                    if row.batch_id
                }
            changed = sum(len(ids) for ids in closure.values()) != before
        closure["batch"].discard("")
        return closure

    # ---------- 删除 ----------

    def _delete(self, closure: dict[str, set[str]]) -> "OrderedDict[str, int]":
        counts: OrderedDict[str, int] = OrderedDict()

        def drop(label: str, query) -> None:
            count = query.delete(synchronize_session=False)
            if count:
                counts[label] = counts.get(label, 0) + count

        B, T, P, R = closure["batch"], closure["task"], closure["plan"], closure["recipe"]
        M, TPL, S, A = closure["method"], closure["template"], closure["station"], closure["asset"]
        C, L, W = closure["capability"], closure["lot"], closure["waste"]
        q = self.db.query

        commands = _ids(q(Command.id).filter(_in(Command.batch_id, B))) if B else set()
        samples = q(Sample).filter(_in(Sample.batch_id, B)).all() if B else []
        sample_ids = {row.id for row in samples}
        physical = {row.physical_sample_id for row in samples if row.physical_sample_id}
        for model in (Plan, ExperimentTask):
            ids = P if model is Plan else T
            if ids:
                for row in q(model).filter(_in(model.id, ids)):
                    physical |= {str(value) for value in (row.sample_ids or []) if value}
        analysis = _ids(q(AnalysisTask.id).filter(_in(AnalysisTask.sample_id, sample_ids))) if sample_ids else set()
        values = _ids(q(ResultValue.id).filter(_in(ResultValue.analysis_task_id, analysis))) if analysis else set()

        # 结果与检测任务
        if values:
            drop("结果复核", q(ResultReview).filter(_in(ResultReview.result_value_id, values)))
            drop("检测结果", q(ResultValue).filter(_in(ResultValue.id, values)))
        if analysis:
            drop("回报事件", q(IngestEvent).filter(_in(IngestEvent.analysis_task_id, analysis)))
            drop("检测任务", q(AnalysisTask).filter(_in(AnalysisTask.id, analysis)))
        if sample_ids or T:
            drop("样品结果", q(Result).filter(_in(Result.sample_id, sample_ids) | _in(Result.task_id, T)))
        reports = _ids(q(Report.id).filter(
            Report.org_id == self.ctx.org_id,
            _in(Report.batch_id, B) | _in(Report.task_id, T) | _in(Report.plan_id, P),
        )) if (B or T or P) else set()
        if reports:
            drop("报告版本", q(ReportVersion).filter(_in(ReportVersion.report_id, reports)))
            drop("报告", q(Report).filter(_in(Report.id, reports)))

        # 批次执行数据
        if B:
            if commands:
                q(Adapter).filter(_in(Adapter.current_command_id, commands)).update(
                    {Adapter.current_command_id: ""}, synchronize_session=False)
            drop("异常事件", q(ExceptionEvent).filter(_in(ExceptionEvent.batch_id, B)))
            drop("遥测", q(Telemetry).filter(_in(Telemetry.batch_id, B)))
            drop("推进事件", q(WorkflowEvent).filter(_in(WorkflowEvent.batch_id, B)))
            drop("步骤跳转", q(StepAdvance).filter(_in(StepAdvance.batch_id, B)))
            drop("批次信号", q(BatchSignal).filter(_in(BatchSignal.batch_id, B)))
            if commands:
                drop("设备执行台账", q(AdapterExecution).filter(_in(AdapterExecution.command_id, commands)))
            drop("检查点", q(Checkpoint).filter(_in(Checkpoint.batch_id, B)))
            drop("载具移动", q(LabwareMove).filter(_in(LabwareMove.batch_id, B)))
            q(Labware).filter(_in(Labware.batch_id, B)).update({Labware.batch_id: ""}, synchronize_session=False)
            drop("指令", q(Command).filter(_in(Command.batch_id, B)))
            drop("步骤实例", q(StepRun).filter(_in(StepRun.batch_id, B)))
            drop("工步时间窗", q(Allocation).filter(_in(Allocation.batch_id, B)))
            drop("资源预约", q(ResourceBooking).filter(_in(ResourceBooking.batch_id, B)))
            drop("人员占用", q(PersonBooking).filter(_in(PersonBooking.batch_id, B)))
            drop("物料预留", q(Reservation).filter(_in(Reservation.batch_id, B)))
            q(Station).filter(_in(Station.dirty_batch_id, B)).update(
                {Station.dirty_batch_id: ""}, synchronize_session=False)
            q(Recipe).filter(_in(Recipe.golden_batch_id, B)).update(
                {Recipe.golden_batch_id: ""}, synchronize_session=False)
            drop("方案批次关联", q(PlanBatchLink).filter(_in(PlanBatchLink.batch_id, B)))
            for proposal in q(ScheduleProposal).filter(ScheduleProposal.org_id == self.ctx.org_id).all():
                kept = [value for value in (proposal.batch_ids or []) if value not in B]
                if len(kept) == len(proposal.batch_ids or []):
                    continue
                if kept:
                    proposal.batch_ids = kept
                else:
                    self.db.delete(proposal)
                    counts["排程提案"] = counts.get("排程提案", 0) + 1
            self.db.flush()
            q(ExperimentTask).filter(_in(ExperimentTask.batch_id, B), ~_in(ExperimentTask.id, T | {""})).update(
                {ExperimentTask.batch_id: ""}, synchronize_session=False)
            drop("样品", q(Sample).filter(_in(Sample.batch_id, B)))
            drop("批次", q(Batch).filter(_in(Batch.id, B)))

        # 任务
        if T:
            drop("任务流转记录", q(TaskAssignment).filter(_in(TaskAssignment.task_id, T)))
            for task in q(ExperimentTask).filter(ExperimentTask.org_id == self.ctx.org_id, ~_in(ExperimentTask.id, T)):
                depends = [value for value in (task.depends_on or []) if value not in T]
                if len(depends) != len(task.depends_on or []):
                    task.depends_on = depends
            self.db.flush()
            drop("实验任务", q(ExperimentTask).filter(_in(ExperimentTask.id, T)))

        # 方案
        if P:
            drop("方案批次关联", q(PlanBatchLink).filter(_in(PlanBatchLink.plan_id, P)))
            drop("方案版本", q(PlanVersion).filter(_in(PlanVersion.plan_id, P)))
            drop("方案提案", q(PlanProposal).filter(_in(PlanProposal.plan_id, P)))
            q(PlanProposal).filter(_in(PlanProposal.created_plan_id, P)).update(
                {PlanProposal.created_plan_id: ""}, synchronize_session=False)
            drop("分析运行", q(AnalysisRun).filter(_in(AnalysisRun.plan_id, P) | _in(AnalysisRun.root_plan_id, P)))
            drop("数据集快照", q(DatasetSnapshot).filter(
                _in(DatasetSnapshot.plan_id, P) | _in(DatasetSnapshot.root_plan_id, P)))
            q(PlanTemplate).filter(_in(PlanTemplate.source_plan_id, P)).update(
                {PlanTemplate.source_plan_id: ""}, synchronize_session=False)
            q(Plan).filter(_in(Plan.parent_plan_id, P), ~_in(Plan.id, P)).update(
                {Plan.parent_plan_id: ""}, synchronize_session=False)
            drop("实验方案", q(Plan).filter(_in(Plan.id, P)))

        # 批次生成的实物样品：没有别的引用了才删；登记来的样品留着
        self._sweep_physical(physical, counts)

        # 流程与方法
        if R:
            drop("方案模板", q(PlanTemplate).filter(_in(PlanTemplate.recipe_id, R)))
            drop("流程", q(Recipe).filter(_in(Recipe.id, R)))
        if M:
            drop("设备方法", q(DeviceMethod).filter(_in(DeviceMethod.id, M)))

        # 能力
        if C:
            drop("能力资质", q(Qualification).filter(
                Qualification.scope_kind == "capability", _in(Qualification.scope_ref, C)))
            for station in q(Station).filter(~_in(Station.id, S | {""})).all():
                limits = dict(station.limits or {})
                if any(key in limits for key in C):
                    station.limits = {key: value for key, value in limits.items() if key not in C}
            self.db.flush()
            drop("能力", q(Capability).filter(_in(Capability.id, C)))

        # 设备模板
        if TPL:
            q(Adapter).filter(_in(Adapter.template_id, TPL)).update(
                {Adapter.template_id: "", Adapter.template_connection: {}}, synchronize_session=False)
            drop("设备模板", q(DeviceTemplate).filter(_in(DeviceTemplate.id, TPL)))

        # 工位
        if S:
            drop("设备执行台账", q(AdapterExecution).filter(_in(AdapterExecution.station_id, S)))
            drop("接入验收", q(AcceptanceRun).filter(_in(AcceptanceRun.station_id, S)))
            drop("点位写入", q(PointWrite).filter(_in(PointWrite.station_id, S)))
            drop("遥测", q(Telemetry).filter(_in(Telemetry.station_id, S)))
            drop("资源预约", q(ResourceBooking).filter(_in(ResourceBooking.station_id, S)))
            drop("异常事件", q(ExceptionEvent).filter(_in(ExceptionEvent.station_id, S)))
            drop("排程提案", q(ScheduleProposal).filter(_in(ScheduleProposal.station_id, S)))
            q(Location).filter(_in(Location.station_id, S)).update({Location.station_id: ""}, synchronize_session=False)
            for identity in q(ServiceIdentity).filter(ServiceIdentity.org_id == self.ctx.org_id).all():
                scopes = dict(identity.scopes or {})
                stations = scopes.get("stations")
                if isinstance(stations, list) and set(stations) & S:
                    scopes["stations"] = [value for value in stations if value not in S]
                    identity.scopes = scopes
            self.db.flush()
            drop("设备连接", q(Adapter).filter(_in(Adapter.station_id, S)))
            drop("工位", q(Station).filter(_in(Station.id, S)))

        # 资产
        if A:
            q(Station).filter(_in(Station.asset_id, A)).update({Station.asset_id: ""}, synchronize_session=False)
            q(Allocation).filter(_in(Allocation.asset_id, A)).update({Allocation.asset_id: ""}, synchronize_session=False)
            drop("校准记录", q(CalibrationRecord).filter(_in(CalibrationRecord.asset_id, A)))
            drop("资源预约", q(ResourceBooking).filter(_in(ResourceBooking.asset_id, A)))
            drop("维护单", q(MaintenanceOrder).filter(_in(MaintenanceOrder.asset_id, A)))
            drop("资产", q(Asset).filter(_in(Asset.id, A)))

        # 批号与废液桶
        if L:
            drop("物料预留", q(Reservation).filter(_in(Reservation.lot_id, L)))
            events = _ids(q(InventoryLedger.event_row_id).filter(_in(InventoryLedger.lot_id, L)))
            drop("库存流水", q(InventoryLedger).filter(_in(InventoryLedger.lot_id, L)))
            if events:
                still = _ids(q(InventoryLedger.event_row_id).filter(_in(InventoryLedger.event_row_id, events)))
                if events - still:
                    drop("库存事件", q(InventoryEvent).filter(_in(InventoryEvent.id, events - still)))
            drop("批号", q(Lot).filter(_in(Lot.id, L)))
        if W:
            drop("废液桶", q(WasteTank).filter(_in(WasteTank.id, W)))

        # 指向已删对象的报警与评论
        gone = B | T | P | R | M | TPL | S | A | C | L | commands
        if gone:
            drop("报警", q(Alarm).filter(Alarm.org_id == self.ctx.org_id, _in(Alarm.source_id, gone)))
            drop("评论", q(Comment).filter(Comment.org_id == self.ctx.org_id, _in(Comment.target_id, gone)))
        self.db.flush()
        return counts

    def _sweep_physical(self, candidates: set[str], counts: "OrderedDict[str, int]") -> None:
        if not candidates:
            return
        q = self.db.query
        rows = q(PhysicalSample).filter(_in(PhysicalSample.id, candidates), PhysicalSample.origin == "batch_generated").all()
        ids = {row.id for row in rows}
        if not ids:
            return
        used = _ids(q(Sample.physical_sample_id).filter(_in(Sample.physical_sample_id, ids)))
        used |= _ids(q(AnalysisTask.physical_sample_id).filter(_in(AnalysisTask.physical_sample_id, ids)))
        used |= _ids(q(ResultValue.physical_sample_id).filter(_in(ResultValue.physical_sample_id, ids)))
        used |= _ids(q(PhysicalSample.parent_id).filter(_in(PhysicalSample.parent_id, ids), ~_in(PhysicalSample.id, ids)))
        for model in (Plan, ExperimentTask):
            for row in q(model).filter(model.org_id == self.ctx.org_id):
                used |= {str(value) for value in (row.sample_ids or [])} & ids
        doomed = ids - used
        if not doomed:
            return
        q(SampleTransfer).filter(_in(SampleTransfer.physical_sample_id, doomed)).delete(synchronize_session=False)
        q(SlotOccupancy).filter(_in(SlotOccupancy.physical_sample_id, doomed)).delete(synchronize_session=False)
        count = q(PhysicalSample).filter(_in(PhysicalSample.id, doomed)).delete(synchronize_session=False)
        if count:
            counts["实物样品"] = counts.get("实物样品", 0) + count

    # ---------- 输出 ----------

    def _out(self, kind: str, object_id: str, closure: dict[str, set[str]], counts: dict[str, int],
             blockers: list[str]) -> dict[str, Any]:
        cascade = [
            {"kind": key, "label": label, "ids": sorted(closure[key])}
            for key, label in KINDS.items() if closure[key] and not (key == kind and closure[key] == {str(object_id)})
        ]
        return {
            "kind": kind, "kind_label": KINDS[kind], "id": str(object_id),
            "sign_target": f"{kind}:{object_id}", "sign_meaning": SIGN_MEANING,
            "cascade": cascade,
            "counts": [{"label": label, "count": count} for label, count in counts.items()],
            "blockers": blockers,
        }


def _ids(query) -> set[str]:
    return {row[0] for row in query if row[0]}
