"""设备接入验收：申请（界面 / 配置变更后自动）、执行器执行、报告入库，以及配置变更后的验收闸门。

- 申请只登记一条排队的验收记录；设备 I/O 全在执行器里做。执行器是唯一驱动设备的进程，同一工位的设备 I/O
  在它那里是串行的——验收不会和投递、轮询抢同一台设备、同一个串口；
- 动作级验收会让设备真的动作：要签名，并写明现场批准人（DEC-02）。等工位上没有可能在动作的指令才开始；
  排队期间新的动作指令先留在队列里，免得一直等不到空档；
- 故障项目（丢回执、忙、联锁、失联）只在非正式环境、设备自报为模拟器、登记了模拟设备控制口时才跑；
- 配置变更后工位欠一份验收（级别见 `domain/adapter_rules.acceptance_requirement`），欠着就按「待接入验收」
  挡住下发；同时自动排一次只读级验收，通过了（级别够）就自动放行。设备恢复在线时，上一次因为连不上而没通过的
  自动验收再排一次；
- 动作级验收和动作指令守同一道门：全站执行门关着、设备失联 / 联锁 / 不接受指令 / 心跳超时时一律排队等，
  串行模式的执行器不跑它（会拖住别的工位的保持与终止）；
- 验收留下没结论的指令（设备可能仍在动作）、执行器在验收中途重启：这台设备改回欠动作级，现场核查后重新验收；
- 检查清单证明不了的设备（不支持状态查询、要现场摆位的动作）由人签名放行，放行本身作为一条验收记录存档。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from ..adapters.acceptance import (
    AcceptanceRecord, FAIL, PASS, SKIP, STATE_LABELS, default_template, injector_for, run_acceptance,
)
from ..adapters.base import AdapterError
from ..adapters.registry import REAL_IMPLEMENTATIONS, contract_of, describe
from ..adapters.simulation import SimulationAdapter
from ..core.clock import now
from ..core.config import settings
from ..core.context import AccessContext, system_context
from ..core.errors import NotFound, StateConflict, ValidationFailed
from ..domain.adapter_rules import (
    LEVEL_LABELS, PHYSICAL, READONLY, acceptance_reason, acceptance_requirement, acceptance_satisfies,
)
from ..models import AcceptanceRun, Adapter, Station, User
from ..repositories.execution import CommandRepository
from ..repositories.resources import StationRepository
from .audit_service import AuditService
from .identity_service import IdentityService

ACTIVE = ("queued", "running")
RUN_STATE_LABELS = {"queued": "排队中", "running": "执行中", "done": "已完成", "error": "出错", "cancelled": "已取消"}
TRIGGER_LABELS = {
    "manual": "手动", "config_change": "配置变更后自动", "device_online": "设备恢复在线后自动重跑",
    "restart": "执行器重启后自动重跑", "waiver": "签名放行",
}
LOG = logging.getLogger("ilcs.executor")


def run_out(run: AcceptanceRun, *, full: bool = False) -> dict[str, Any]:
    counts = {state: sum(1 for check in run.checks or [] if check.get("state") == state) for state in (PASS, FAIL, SKIP)}
    row = {
        "id": run.id, "station_id": run.station_id, "level": run.level, "level_label": LEVEL_LABELS.get(run.level, run.level),
        "faults": run.faults, "capability": run.capability, "params": run.params or {},
        "trigger": run.trigger, "trigger_label": TRIGGER_LABELS.get(run.trigger, run.trigger),
        "state": run.state, "state_label": RUN_STATE_LABELS.get(run.state, run.state), "ok": run.ok,
        "simulator": run.simulator, "kind": run.kind, "driver": run.driver, "protocol": run.protocol,
        "adapter_version": run.adapter_version, "config_version": run.config_version,
        "config_digest": (run.config_digest or "")[:16], "identity": run.identity or {},
        "template": {"id": run.template_id, "code": run.template_code, "revision": run.template_revision}
        if run.template_id else None,
        "counts": counts, "error": run.error, "requested_by": run.requested_by, "approval": run.approval,
        "created_at": run.created_at.isoformat(timespec="seconds") if run.created_at else None,
        "started_at": run.started_at.isoformat(timespec="seconds") if run.started_at else None,
        "finished_at": run.finished_at.isoformat(timespec="seconds") if run.finished_at else None,
    }
    if full:
        row["checks"] = [
            {**check, "state_label": STATE_LABELS.get(check.get("state"), check.get("state"))}
            for check in run.checks or []
        ]
        row["report_md"] = run.report_md
    return row


def gate_out(adapter: Adapter) -> dict[str, Any]:
    """适配器的验收闸门：还欠什么级别、为什么挡着、最近一次满足要求的验收。"""
    required = adapter.acceptance_required if adapter.kind == "real" else ""
    return {
        "required": required, "required_label": LEVEL_LABELS.get(required, ""),
        "reason": acceptance_reason(adapter.station_id, required, adapter.config_version) if required else "",
        "accepted_config_version": adapter.accepted_config_version, "accepted_run_id": adapter.accepted_run_id,
    }


def _active_runs(db: Session, station_id: str) -> list[AcceptanceRun]:
    return list(
        db.query(AcceptanceRun).filter(AcceptanceRun.station_id == station_id, AcceptanceRun.state.in_(ACTIVE))
        .order_by(AcceptanceRun.created_at).all()
    )


def _queue(db: Session, adapter: Adapter, org_id: str, *, level: str, trigger: str, requested_by: str,
           requested_by_id: str = "", capability: str = "", params: dict | None = None, faults: bool = False,
           approval: str = "", signature_id: str = "") -> AcceptanceRun:
    run = AcceptanceRun(
        org_id=org_id, station_id=adapter.station_id, level=level, faults=faults, capability=capability,
        params=params or {}, trigger=trigger, approval=approval, signature_id=signature_id,
        requested_by=requested_by, requested_by_id=requested_by_id, state="queued",
        kind=adapter.kind, driver=adapter.driver, protocol=adapter.protocol, adapter_version=adapter.version,
        config_version=adapter.config_version, created_at=now(),
    )
    db.add(run)
    db.flush()
    return run


def after_config_change(db: Session, adapter: Adapter, before: dict[str, Any], *, org_id: str,
                        requested_by: str, requested_by_id: str = "", light: bool = False) -> str:
    """配置刚变过（调用方已递增 `config_version`、并锁着适配器行）：算出欠的验收级别，撤掉按旧配置排队的验收，排一次只读级。

    界面修改、登记新工位、试点切换脚本都走这里。返回欠的级别（'' 表示不设闸门）。
    `light=True`：只改了说明、协议名、版本或超时——不改变连谁、怎么判结论，不新欠验收；原来欠着的照旧欠着，按新版本重排。
    """
    required = adapter.acceptance_required if light else acceptance_requirement(
        before, {"kind": adapter.kind, "driver": adapter.driver}, adapter.acceptance_required,
    )
    if adapter.kind != "real":
        required = ""
    adapter.acceptance_required = required
    flag_modified(adapter, "acceptance_required")  # 值没变也要写：别让并发的验收收尾把它悄悄清掉
    # 比较并交换：执行器此刻可能刚领走一条，领走的不撤（它按领走时的配置验收，结论落不到新配置上）
    db.query(AcceptanceRun).filter(
        AcceptanceRun.station_id == adapter.station_id, AcceptanceRun.state == "queued",
    ).update({"state": "cancelled", "finished_at": now(),
              "error": f"配置已改为 v{adapter.config_version}：按旧配置排队的验收作废，改按新配置验收"},
             synchronize_session=False)
    if required:
        _queue(db, adapter, org_id, level=READONLY, trigger="config_change", requested_by=requested_by,
               requested_by_id=requested_by_id)
    return required


def requeue_if_needed(db: Session, adapter: Adapter, org_id: str) -> bool:
    """设备恢复在线：还欠验收、这一版配置还没有通过的验收（多半是当时连不上）、又没有排队中的，再排一次只读级。

    欠动作级时也排：只读级不动设备，自报为模拟器的设备靠它就能放行；真实设备照样要人申请动作级。
    """
    if adapter.kind != "real" or not adapter.acceptance_required or _active_runs(db, adapter.station_id):
        return False
    last = (
        db.query(AcceptanceRun).filter(
            AcceptanceRun.station_id == adapter.station_id, AcceptanceRun.config_version == adapter.config_version,
        ).order_by(AcceptanceRun.created_at.desc()).first()
    )
    if last is not None and (last.state == "done" and last.ok):
        return False
    _queue(db, adapter, org_id, level=READONLY, trigger="device_online", requested_by="执行器")
    return True


def dispatch_hold(db: Session, adapter: Adapter, command_type: str = "dispatch") -> tuple[str, str]:
    """动作指令能不能投给这台设备：('', '') 可以；('wait', 原因) 留在队列里等；('refuse', 原因) 不投递、批次挂起。

    动作级验收排队或进行中：新动作等它做完（排队期间先不投，免得一直等不到空档）；续跑照投——它接续的是设备上
    已经在保持的动作，动作级验收本来就在等它结束，两边互相等就谁也动不了。
    欠着验收、有验收正在排队：等它出结论；欠着验收、没有排队的验收（上次不通过，或欠动作级）：不投递。
    """
    active = _active_runs(db, adapter.station_id)
    if command_type != "resume" and any(run.level == PHYSICAL for run in active):
        return "wait", "动作级接入验收排队中或进行中"
    if adapter.kind == "real" and adapter.acceptance_required:
        if active:
            return "wait", "配置变更后的接入验收进行中"
        return "refuse", (f"{acceptance_reason(adapter.station_id, adapter.acceptance_required, adapter.config_version)}"
                          "；动作指令未投递，不自动重试")
    return "", ""


def running_stations(db: Session) -> set[str]:
    return {row[0] for row in db.query(AcceptanceRun.station_id).filter(AcceptanceRun.state == "running").all()}


class AcceptanceService:
    """界面侧：申请、查看、取消。"""

    def __init__(self, db: Session, ctx: AccessContext):
        self.db = db
        self.ctx = ctx
        self.stations = StationRepository(db, ctx)
        self.audit = AuditService(db, ctx)
        self.identity = IdentityService(db, ctx)

    def _station(self, station_id: str) -> tuple[Station, Adapter]:
        station = self.stations.get(station_id)
        if station is None:
            raise NotFound("工位不存在")
        adapter = self.db.get(Adapter, station_id)
        if adapter is None:
            raise NotFound("适配器未登记")
        return station, adapter

    def request(self, station_id: str, payload: dict[str, Any], user: User) -> dict[str, Any]:
        station, _ = self._station(station_id)
        # 锁住适配器行：两个人同时申请只排得进一条；动作级签的「这一版配置」在提交前不会被改掉
        adapter = self.db.query(Adapter).filter(Adapter.station_id == station_id).with_for_update().populate_existing().one()
        if not adapter.enabled:
            raise StateConflict("适配器已停用，不能验收", code="adapter_disabled")
        level = payload.get("level") or READONLY
        if level not in LEVEL_LABELS:
            raise ValidationFailed("验收级别只能是 readonly（只读级）或 physical（动作级）", code="acceptance_level_invalid")
        faults = bool(payload.get("faults"))
        if faults and level != PHYSICAL:
            raise ValidationFailed("故障项目会让设备动作：要和动作级验收一起申请", code="acceptance_faults_need_physical")
        if _active_runs(self.db, station_id):
            raise StateConflict(f"{station_id} 已有排队或进行中的接入验收，等它出结论后再申请", code="acceptance_busy")
        limits = station.limits or {}
        capability = str(payload.get("capability") or next(iter(sorted(limits)), ""))
        if limits and capability not in limits:
            raise ValidationFailed(f"{station_id} 没有能力 {capability}（工位能力：{'、'.join(sorted(limits))}）",
                                   code="acceptance_capability_invalid")
        params = payload.get("params")
        if params is not None:
            params = self._checked_params(station_id, limits.get(capability) or {}, params)
        else:
            params = default_template(station_id, limits, capability).params
        approval, signature_id = str(payload.get("approval") or "").strip(), ""
        if level == PHYSICAL:
            if not approval:
                raise ValidationFailed("动作级验收会让设备真的动作：写明现场批准人与批准依据（DEC-02）",
                                       code="acceptance_approval_required")
            signature = self.identity.consume_signature(
                payload.get("signature_id"), user, "批准设备接入验收", object_ref=station_id,
                object_version=adapter.config_version, strict=True,
            )
            signature_id = signature.id
        run = _queue(self.db, adapter, station.org_id or self.ctx.org_id, level=level, trigger="manual",
                     requested_by=user.display_name, requested_by_id=user.id, capability=capability, params=params,
                     faults=faults, approval=approval, signature_id=signature_id)
        waiting = CommandRepository(self.db).acting_on_station(station_id) if level == PHYSICAL else []
        self.audit.record(
            user, "申请设备接入验收", station_id, sign=bool(signature_id), signature_id=signature_id,
            after=f"{LEVEL_LABELS[level]}{' + 故障项目' if faults else ''} · 配置 v{adapter.config_version}",
            detail=f"能力 {capability or '—'}；参数 {params}" + (f"；现场批准：{approval}" if approval else ""),
        )
        self.db.commit()
        return {**run_out(run), "waiting_for": len(waiting)}

    @staticmethod
    def _checked_params(station_id: str, window: dict, params: Any) -> dict:
        """验收指令的参数也要落在工位能力极限里：动作级验收会让设备照这些参数真的动作。

        数值是设定值，必须在极限里；不是数值的（转运的起止位置、载具）是结构化信息，没有极限可核对，照原样下发。
        """
        if not isinstance(params, dict):
            raise ValidationFailed("验收参数必须是 JSON 对象", code="acceptance_params_invalid")
        problems = []
        for name, value in params.items():
            span = window.get(name)
            numeric = isinstance(value, (int, float)) and not isinstance(value, bool)
            if span is None:
                if numeric:
                    problems.append(f"{name} 是数值设定值，但不在工位能力极限里，没法核对范围")
            elif not numeric or not span[0] <= value <= span[1]:
                problems.append(f"{name} = {value} 超出 {span[0]}–{span[1]}")
        if problems:
            raise ValidationFailed(f"{station_id} 的验收参数不可用：{'；'.join(problems)}", code="acceptance_params_invalid")
        return dict(params)

    def waive(self, station_id: str, reason: str, signature_id: str | None, user: User) -> dict[str, Any]:
        """签名放行：检查清单证明不了的设备（不支持状态查询、要现场摆位的动作），现场核对后由人放行。

        放行本身作为一条验收记录存档（来由「签名放行」，写明依据），和验收报告一样出了结论就不能改。
        """
        station, _ = self._station(station_id)
        reason = str(reason or "").strip()
        if len(reason) < 4:
            raise ValidationFailed("写明为什么放行、现场核对了什么", code="acceptance_waiver_reason_required")
        # 先锁住适配器再核签名：签的是「这一版配置」，锁住之后别人改不了它
        adapter = self.db.query(Adapter).filter(Adapter.station_id == station_id).with_for_update().populate_existing().one()
        if adapter.kind != "real" or not adapter.acceptance_required:
            raise StateConflict(f"{station_id} 现在没有欠接入验收", code="acceptance_not_required")
        if _active_runs(self.db, station_id):
            raise StateConflict(f"{station_id} 有排队或进行中的验收，等它出结论后再决定", code="acceptance_busy")
        signature = self.identity.consume_signature(
            signature_id, user, "签名放行接入验收", object_ref=station_id, object_version=adapter.config_version,
            strict=True,
        )
        required = adapter.acceptance_required
        run = AcceptanceRun(
            org_id=station.org_id or self.ctx.org_id, station_id=station_id, level=required, trigger="waiver",
            approval=reason, signature_id=signature.id, requested_by=user.display_name, requested_by_id=user.id,
            state="done", ok=True, kind=adapter.kind, driver=adapter.driver, protocol=adapter.protocol,
            adapter_version=adapter.version, config_version=adapter.config_version,
            checks=[{"key": "waiver", "label": "签名放行", "state": PASS, "detail": reason, "evidence": {}}],
            report_md=(f"# 设备接入验收：{station_id}（签名放行）\n\n- 放行人：{user.display_name}\n"
                       f"- 欠的级别：{LEVEL_LABELS.get(required, required)}；配置 v{adapter.config_version}；驱动 {adapter.driver}\n"
                       f"- 依据：{reason}\n"),
            created_at=now(), started_at=now(), finished_at=now(),
        )
        self.db.add(run)
        self.db.flush()
        adapter.acceptance_required = ""
        adapter.accepted_config_version, adapter.accepted_run_id = adapter.config_version, run.id
        self.audit.record(
            user, "签名放行接入验收", station_id, sign=True, meaning=signature.meaning, signature_id=signature.id,
            before=f"待接入验收（{LEVEL_LABELS.get(required, required)}）", after="已放行",
            detail=f"配置 v{adapter.config_version}；{reason}",
        )
        self.db.commit()
        return run_out(run)

    def list_for_station(self, station_id: str, limit: int = 20) -> dict[str, Any]:
        _, adapter = self._station(station_id)
        runs = (
            self.db.query(AcceptanceRun).filter(AcceptanceRun.station_id == station_id)
            .order_by(AcceptanceRun.created_at.desc()).limit(max(1, min(limit, 100))).all()
        )
        return {"station_id": station_id, "gate": gate_out(adapter), "runs": [run_out(run) for run in runs]}

    def get(self, run_id: str) -> AcceptanceRun:
        run = self.db.get(AcceptanceRun, run_id)
        # 验收记录按工位的组织可见：别的组织的工位当作不存在
        if run is None or self.stations.get(run.station_id) is None:
            raise NotFound("验收记录不存在")
        return run

    def cancel(self, run_id: str, user: User) -> dict[str, Any]:
        run = self.get(run_id)
        cancelled = (
            self.db.query(AcceptanceRun).filter(AcceptanceRun.id == run.id, AcceptanceRun.state == "queued")
            .update({"state": "cancelled", "finished_at": now(), "error": f"{user.display_name} 取消"},
                    synchronize_session=False)
        )
        if not cancelled:
            raise StateConflict("只有排队中的验收可以取消；执行中的验收做完才出结论", code="acceptance_not_queued")
        self.audit.record(user, "取消设备接入验收", run.station_id, before="排队中", after="已取消",
                          detail=f"{LEVEL_LABELS.get(run.level, run.level)} · {run.id}")
        self.db.commit()
        self.db.refresh(run)
        return run_out(run)


class AcceptanceRunner:
    """执行器侧：领取排队的验收，逐项执行，写报告、更新闸门。"""

    def __init__(self, db: Session):
        self.db = db

    def due_stations(self) -> set[str]:
        return {row[0] for row in self.db.query(AcceptanceRun.station_id).filter(AcceptanceRun.state == "queued").all()}

    def interrupt_orphans(self) -> int:
        """执行器启动时：上一个执行器没做完的验收判出错。单活锁保证此刻没有别的执行器在跑它们。

        - 动作级：这台设备改回欠动作级——设备上可能留有没结束的验收指令，要现场核对；
        - 只读级：不让设备动作，留不下什么；还欠着验收的按当前配置重新排一次，工位不会一直卡在待验收。
        """
        rows = self.db.query(AcceptanceRun).filter(AcceptanceRun.state == "running").all()
        for run in rows:
            run.state, run.finished_at = "error", now()
            if run.level == PHYSICAL:
                run.error = ("执行器在验收过程中重启，这次验收中断；设备上可能留有以 ACC- 开头的验收指令，"
                             "请现场核对设备状态后重新验收")
                self._require_physical(run.station_id, run.org_id, "验收中途执行器重启：设备上可能留有没结束的验收指令")
                continue
            run.error = "执行器在只读级验收过程中重启，这次验收中断（只读级不让设备动作）"
            adapter = self.db.get(Adapter, run.station_id)
            if adapter is not None and adapter.kind == "real" and adapter.acceptance_required and adapter.enabled:
                self.db.flush()
                if not _active_runs(self.db, run.station_id):
                    _queue(self.db, adapter, run.org_id, level=READONLY, trigger="restart", requested_by="执行器")
        self.db.commit()
        return len(rows)

    def run_due(self, station_id: str | None = None, *, on_progress: Callable[[], None] | None = None,
                dispatch_open: bool | None = None, physical: bool = True) -> int:
        """`physical=False`：串行模式的执行器不跑动作级验收——几分钟的动作会拖住别的工位的保持与终止。"""
        query = self.db.query(AcceptanceRun).filter(AcceptanceRun.state == "queued")
        if station_id is not None:
            query = query.filter(AcceptanceRun.station_id == station_id)
        finished = 0
        for run_id, run_station, level in [(run.id, run.station_id, run.level)
                                           for run in query.order_by(AcceptanceRun.created_at).all()]:
            adapter = self.db.get(Adapter, run_station)
            if adapter is None or not adapter.enabled:
                self._close(run_id, "error", error="适配器已停用或不存在，验收没有执行")
                continue
            if level == PHYSICAL:
                waiting = self._physical_wait(adapter, dispatch_open, physical)
                if waiting:
                    self._note_waiting(run_id, waiting)
                    continue
            if self._execute(run_id, adapter, on_progress):
                finished += 1
        return finished

    def _physical_wait(self, adapter: Adapter, dispatch_open: bool | None, physical: bool) -> str:
        """动作级验收和动作指令守同一道门。返回要等的原因；空串表示可以开始。"""
        from .gate_service import GateService

        if not physical:
            return "执行器处于串行模式：动作级验收会拖住别的工位的保持与终止，切回并发模式后执行"
        acting = CommandRepository(self.db).acting_on_station(adapter.station_id)
        if acting:
            return f"设备上还有 {len(acting)} 条指令在动作，等它们结束"
        if dispatch_open is None:
            dispatch_open = bool(GateService(self.db).status()["open"])
        if not dispatch_open:
            return "全站执行门关闭（公共保护联锁或执行器存活异常），门开后再开始"
        if not adapter.connected:
            return "设备失联，恢复在线后再开始"
        if adapter.site_interlock:
            return "设备联锁触发，解除后再开始"
        if not adapter.accepts_commands:
            return "设备暂不接受动作指令"
        age = (now() - adapter.last_heartbeat).total_seconds() if adapter.last_heartbeat else None
        if age is None or age > settings.heartbeat_stale_sec:
            return "设备心跳超时，在线状态不可信"
        cap = max(1, settings.executor_workers // 2)
        running = self.db.query(AcceptanceRun.id).filter(
            AcceptanceRun.state == "running", AcceptanceRun.level == PHYSICAL,
        ).count()
        if running >= cap:
            return f"同时进行的动作级验收已有 {running} 个（上限 {cap}），排队等待"
        return ""

    def _note_waiting(self, run_id: str, reason: str) -> None:
        """排队中的动作级验收写明在等什么（原因变了才写，不在每一轮刷一遍）。"""
        note = f"等待：{reason}"
        self.db.query(AcceptanceRun).filter(
            AcceptanceRun.id == run_id, AcceptanceRun.state == "queued", AcceptanceRun.error != note,
        ).update({"error": note}, synchronize_session=False)
        self.db.commit()

    def _close(self, run_id: str, state: str, **values: Any) -> bool:
        closed = self.db.query(AcceptanceRun).filter(AcceptanceRun.id == run_id, AcceptanceRun.state.in_(ACTIVE)).update(
            {"state": state, "finished_at": now(), **values}, synchronize_session=False,
        )
        self.db.commit()
        if not closed:
            LOG.warning("接入验收记录已不在排队 / 执行中，结论没有写进去", extra={"fields": {"run_id": run_id, "state": state}})
        return bool(closed)

    def _execute(self, run_id: str, adapter: Adapter, on_progress: Callable[[], None] | None) -> bool:
        from ..adapters.registry import release
        from .template_service import template_brief

        record = AcceptanceRecord.of(adapter)
        device_template = template_brief(self.db, adapter.template_id) or {}
        # 领取：排队 → 执行中，与取消是同一个比较并交换；只领按当前配置排的——申请时签的是那一版配置
        claimed = self.db.query(AcceptanceRun).filter(
            AcceptanceRun.id == run_id, AcceptanceRun.state == "queued",
            AcceptanceRun.config_version == record.config_version,
        ).update(
            {"state": "running", "started_at": now(), "error": "", "kind": record.kind, "driver": record.driver,
             "protocol": record.protocol, "adapter_version": record.version,
             "template_id": adapter.template_id or "", "template_code": device_template.get("code") or "",
             "template_revision": device_template.get("revision") or 0},
            synchronize_session=False,
        )
        self.db.commit()
        if not claimed:
            self.db.query(AcceptanceRun).filter(AcceptanceRun.id == run_id, AcceptanceRun.state == "queued").update(
                {"state": "cancelled", "finished_at": now(),
                 "error": f"配置已改为 v{record.config_version}：按旧配置申请的验收作废，请按新配置重新申请"},
                synchronize_session=False,
            )
            self.db.commit()
            return False
        org_id, level = "", READONLY
        try:
            run = self.db.get(AcceptanceRun, run_id)
            station = self.db.get(Station, record.station_id)
            limits = dict(station.limits or {}) if station is not None else {}
            capabilities = tuple(sorted(limits))
            template = default_template(record.station_id, limits, run.capability, dict(run.params or {}) or None)
            level, faults, org_id = run.level, run.faults, run.org_id
            trigger, requested_by, approval = run.trigger, run.requested_by, run.approval
            # 设备 I/O 期间不拿着任何行锁、也不开着事务
            self.db.expunge(run)
            self.db.commit()
            # 缓存里的生产驱动实例先关掉：保持连接的设备（单客户端的串口服务器）不能被两个连接同时占着
            release(record.station_id)

            def factory():
                if record.kind == "real":
                    implementation = REAL_IMPLEMENTATIONS.get(record.driver)
                    if implementation is None:
                        raise AdapterError(f"当前版本没有登记驱动 {record.driver}")
                    return implementation(record)
                if not settings.simulation_allowed:
                    raise AdapterError("正式环境禁止模拟适配器")
                return SimulationAdapter(record.station_id, record.protocol, capabilities)

            injector, note = self._injector(record, template.capability, factory) if faults else (None, "")
            report = run_acceptance(
                record, factory, template, contract=contract_of(record, capabilities).as_dict(),
                describe=lambda instance: describe(instance, record), physical=level == PHYSICAL,
                injector=injector, poll_timeout=settings.acceptance_poll_timeout_sec, on_progress=on_progress,
                fault_note=note or ("本次没有申请故障项目" if not faults else ""),
            )
            header = (
                f"\n- 验收记录：{run_id}（{TRIGGER_LABELS.get(trigger, trigger)}，申请人 {requested_by or '—'}）\n"
                f"- 级别：{LEVEL_LABELS.get(level, level)}；配置 v{record.config_version}"
                + (f"；设备接入模板 {device_template['code']} r{device_template['revision']}"
                   if device_template.get("code") else "")
                + (f"；现场批准：{approval}" if approval else "") + "\n"
            )
            text = report.markdown()
            first, _, rest = text.partition("\n")
            self._close(
                run_id, "done", ok=report.ok, simulator=report.simulator, identity=report.identity,
                checks=[check.__dict__ for check in report.checks], report_md=first + "\n" + header + rest,
                config_digest=report.config_digest,
            )
            # 动作项目一项都没真跑的动作级报告，只算只读级证据
            proven = level if level != PHYSICAL or report.physical_ran else READONLY
            cleared = self._settle_gate(record, proven, report.ok, report.simulator, run_id, report.leftovers, org_id)
            counts = {state: sum(1 for check in report.checks if check.state == state) for state in (PASS, FAIL, SKIP)}
            self._audit(
                org_id, record.station_id,
                f"{LEVEL_LABELS.get(level, level)} · {'通过' if report.ok else '不通过'}"
                + ("；解除待接入验收" if cleared else "")
                + (f"；留下没结论的验收指令 {len(report.leftovers)} 条，改回欠动作级" if report.leftovers else ""),
                f"{run_id}；配置 v{record.config_version}；驱动 {record.driver}；通过 {counts[PASS]} / "
                f"不通过 {counts[FAIL]} / 跳过 {counts[SKIP]}",
            )
        except Exception as exc:  # 领走之后的任何意外都要让这条记录出结论：卡在「执行中」会挡住这台设备的动作指令
            self.db.rollback()
            LOG.exception("接入验收执行出错", extra={"fields": {"run_id": run_id, "station_id": record.station_id}})
            self._close(run_id, "error", error=f"验收没能执行完：{exc}"[:2000])
            if level == PHYSICAL:
                self._require_physical(record.station_id, org_id, f"动作级验收中途出错：{exc}"[:200])
                self.db.commit()
            self._audit(org_id, record.station_id, f"{LEVEL_LABELS.get(level, level)} · 出错", str(exc)[:500])
        return True

    def _injector(self, record: AcceptanceRecord, capability: str, factory) -> tuple[Any, str]:
        if settings.environment == "production":
            return None, "正式环境不做故障注入：真实设备的回执丢失要在网络路径上注入"
        try:
            probe = factory()
            health = probe.healthcheck() or {}
            identity = (probe.identity() if hasattr(probe, "identity") else {}) or {}
        except Exception as exc:  # noqa: BLE001  连不上：只读项目会写出原因
            return None, f"读不到设备身份，无法确认是模拟器：{exc}"
        if record.kind == "real" and not (health.get("simulator") or identity.get("simulator")):
            return None, "设备没有自报为模拟器：故障项目只对模拟设备开放，真实设备要在网络路径上注入"
        try:
            return injector_for(record, capability)
        except AdapterError as exc:
            return None, f"模拟设备控制口不可用：{exc}"

    def _require_physical(self, station_id: str, org_id: str, reason: str) -> None:
        """设备状态说不清了（验收留下没结论的指令、中途出错或重启）：改回欠动作级，现场核查后重新验收。"""
        adapter = self.db.query(Adapter).filter(Adapter.station_id == station_id).with_for_update().one_or_none()
        if adapter is None or adapter.kind != "real":
            return
        adapter.acceptance_required = PHYSICAL
        flag_modified(adapter, "acceptance_required")
        if org_id:
            AuditService(self.db, system_context(org_id, "执行器接入验收")).record(
                None, "接入验收改回欠动作级", station_id, after="待接入验收（动作级）", detail=reason, org_id=org_id,
            )

    def _settle_gate(self, record: AcceptanceRecord, level: str, ok: bool, simulator: bool, run_id: str,
                     leftovers: list[str], org_id: str) -> bool:
        """验收结论落到闸门上。按最新的适配器行判断：验收期间配置又改了，这份报告不能替新配置放行。"""
        if leftovers:
            self._require_physical(record.station_id, org_id, f"验收留下没结论的指令：{'、'.join(leftovers)}")
            self.db.commit()
            return False
        adapter = self.db.query(Adapter).filter(Adapter.station_id == record.station_id).with_for_update().one_or_none()
        if adapter is None or adapter.config_version != record.config_version or not ok:
            self.db.commit()
            return False
        cleared = False
        if not adapter.acceptance_required or acceptance_satisfies(adapter.acceptance_required, level, ok, simulator):
            cleared = bool(adapter.acceptance_required)
            adapter.acceptance_required = ""
            adapter.accepted_config_version, adapter.accepted_run_id = record.config_version, run_id
        self.db.commit()
        return cleared

    def _audit(self, org_id: str, station_id: str, after: str, detail: str) -> None:
        if not org_id:
            return
        AuditService(self.db, system_context(org_id, "执行器接入验收")).record(
            None, "设备接入验收", station_id, after=after, detail=detail, org_id=org_id,
        )
        self.db.commit()


def heartbeat_keeper(session_factory: Callable[[], Session], every_sec: float = 10.0) -> Callable[[], None]:
    """串行执行器里跑长时间的动作级验收时续写执行器存活记录：否则执行门会以为执行器停了。"""
    from .monitoring_service import ExecutorLiveness

    last = [time.monotonic()]

    def beat() -> None:
        if time.monotonic() - last[0] < every_sec:
            return
        last[0] = time.monotonic()
        with session_factory() as db:
            ExecutorLiveness(db).beat()
            db.commit()

    return beat
