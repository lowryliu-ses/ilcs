"""载具与转运用例：登记、绑定批次、扫码放置、转运指令的生成与结论、现场总览。

转运是一条真正的设备指令（`type=transfer`，能力 `cap.transfer`），投给 AGV / 机械臂工位的
适配器，走与设备动作相同的执行门、联锁、投递比较并交换、超时与结果未知核查。设备步骤的
动作指令把它设成前置指令：板没被确认送到，设备就不会收到动作。

位置追踪按批次启用：批次绑定了载具才转运，没绑定的批次行为与之前完全一致。
`ILCS_LABWARE_REQUIRED=1` 时下发要求先绑定载具（全自动产线应打开）。
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy.orm import Session

from ..core.clock import now
from ..core.config import settings
from ..core.context import AccessContext
from ..core.errors import NotFound, StateConflict, ValidationFailed
from ..domain.labware import (
    CarrierSpec, LocationSpec, manual_move_blockers, plan_transfer,
)
from ..models import (
    Adapter, Allocation, Batch, Command, Labware, LabwareMove, LabwareType, Location, Station, User,
)
from ..repositories.base import ScopedRepository
from .audit_service import AuditService

TRANSFER = "transfer"
TRANSFER_CAPABILITY = "cap.transfer"
OPEN_COMMAND_STATES = ("sent", "accepted", "running", "unknown", "manual")
ENDED_BATCH_STATES = ("done", "aborted")


class LabwareRepository(ScopedRepository[Labware]):
    model = Labware


class TransferService:
    def __init__(self, db: Session, ctx: AccessContext):
        self.db = db
        self.ctx = ctx
        self.labware = LabwareRepository(db, ctx)
        self.audit = AuditService(db, ctx)

    # ---------- 读 ----------

    def _location_specs(self) -> list[LocationSpec]:
        return [
            LocationSpec(
                id=row.id, kind=row.kind, station_id=row.station_id or "",
                accepts=tuple(row.accepts or ()), active=bool(row.active), position=row.position or 0,
            )
            for row in self.db.query(Location).all()
        ]

    def _open_transfers(self) -> list[Command]:
        return list(
            self.db.query(Command)
            .filter(Command.type == TRANSFER, Command.state.in_(OPEN_COMMAND_STATES))
            .all()
        )

    def _occupied(self) -> dict[str, str]:
        """位置 → 占着它的载具。在途转运的目的位置也算占用（板正在往那里送）。"""
        taken: dict[str, str] = {}
        for row in self.db.query(Labware).filter(
            Labware.location_id.isnot(None), Labware.state != "retired"
        ).all():
            taken[row.location_id] = row.barcode
        for command in self._open_transfers():
            destination = ((command.params or {}).get("to") or {}).get("location_id")
            if destination:
                taken.setdefault(destination, f"在途 {command.id[:8]}")
        return taken

    def _kind_of(self, labware: Labware) -> str:
        kind = self.db.get(LabwareType, labware.type_id)
        return kind.kind if kind else "plate"

    def carriers(self) -> list[CarrierSpec]:
        from .execution_service import ExecutionService

        probe = Command(type=TRANSFER)
        found = []
        open_transfers = self._open_transfers()
        for station in self.db.query(Station).order_by(Station.id).all():
            if TRANSFER_CAPABILITY not in (station.limits or {}):
                continue
            record = self.db.get(Adapter, station.id)
            why = ""
            if station.retired:
                why = "已退役"
            elif station.status in {"fault", "offline"}:
                why = "故障" if station.status == "fault" else "离线"
            elif record is None:
                why = "没有适配器"
            else:
                why = ExecutionService.delivery_blocker(probe, record).split("；")[0]
            busy = bool(record and record.current_command_id) or any(
                command.station_id == station.id and command.state in {"sent", "accepted", "running"}
                for command in open_transfers
            )
            found.append(CarrierSpec(id=station.id, usable=not why, why_not=why, busy=busy))
        return found

    def for_batch(self, batch_id: str) -> Labware | None:
        return (
            self.labware.query()
            .filter(Labware.batch_id == batch_id, Labware.state != "retired")
            .order_by(Labware.created_at.desc())
            .first()
        )

    def _in_transit(self, labware: Labware) -> Command | None:
        return next((c for c in self._open_transfers() if c.labware_id == labware.id), None)

    # ---------- 转运计划与指令 ----------

    def plan_for_step(self, batch: Batch, step_index: int, station_id: str):
        """设备步骤开始前：要不要转运、能不能转运。返回 (载具, 计划)；没绑定载具返回 (None, None)。"""
        labware = self.for_batch(batch.id)
        if labware is None:
            return None, None
        specs = self._location_specs()
        current = next((spec for spec in specs if spec.id == labware.location_id), None)
        in_transit = self._in_transit(labware)
        preferred = (
            self.db.query(Allocation)
            .filter(Allocation.batch_id == batch.id, Allocation.step_index == step_index,
                    Allocation.kind == "transfer")
            .first()
        )
        plan = plan_transfer(
            current=current,
            labware_known=labware.state != "lost" and labware.location_id is not None,
            target_station_id=station_id,
            labware_kind=self._kind_of(labware),
            locations=specs,
            occupied=set(self._occupied()),
            carriers=self.carriers(),
            preferred_carrier=preferred.station_id if preferred else "",
        )
        if in_transit is not None:
            plan.blocked.insert(0, f"载具 {labware.barcode} 有未结束的转运指令 {in_transit.id[:8]}：先等它完成或核查")
        return labware, plan

    def require_ready(self, batch: Batch, step_index: int, station_id: str) -> None:
        """下发 / 恢复前的检查：不能转运就不签名、不下发，把原因原样给界面。"""
        labware, plan = self.plan_for_step(batch, step_index, station_id)
        if labware is None:
            if settings.labware_required:
                raise StateConflict(
                    "批次没有绑定载具：全自动产线要求先扫码绑定板 / 托盘",
                    {"blocked": [{"key": "labware", "label": "未绑定载具"}]}, code="labware_required",
                )
            return
        if plan is not None and plan.needed and not plan.ok:
            raise StateConflict(
                "载具不能送到首个设备工位",
                {"blocked": [{"key": "labware", "label": reason} for reason in plan.blocked]},
                code="transfer_blocked",
            )

    def prepare(self, batch: Batch, step_index: int, station_id: str, step_run_id: str):
        """需要时生成转运指令。返回 (转运指令或 None, 阻塞原因)。"""
        labware, plan = self.plan_for_step(batch, step_index, station_id)
        if labware is None or plan is None or not plan.needed:
            return None, ""
        if not plan.ok:
            return None, "；".join(plan.blocked)
        source = self.db.get(Location, plan.source)
        destination = self.db.get(Location, plan.destination)
        allocation = (
            self.db.query(Allocation)
            .filter(Allocation.batch_id == batch.id, Allocation.step_index == step_index,
                    Allocation.kind == "transfer")
            .first()
        )
        command = Command(
            org_id=batch.org_id or self.ctx.org_id,
            batch_id=batch.id,
            step_run_id=step_run_id,
            station_id=plan.carrier,
            capability=TRANSFER_CAPABILITY,
            type=TRANSFER,
            state="sent",
            delivery_state="queued",
            step_index=step_index,
            labware_id=labware.id,
            not_before=(
                allocation.starts_at - timedelta(minutes=settings.early_start_tolerance_min)
                if allocation else None
            ),
            params={
                "labware_id": labware.id,
                "barcode": labware.barcode,
                "labware_type": labware.type_id,
                "from": {"location_id": source.id, "station_id": source.station_id, "kind": source.kind},
                "to": {
                    "location_id": destination.id, "station_id": destination.station_id,
                    "kind": destination.kind,
                },
            },
        )
        self.db.add(command)
        self.db.flush()
        self.audit.record(
            None, "生成转运指令", batch.id, command_id=command.id,
            detail=(
                f"第 {step_index + 1} 步开始前把 {labware.barcode} 从 {source.id} 送到 {destination.id}，"
                f"承运 {plan.carrier}；设备动作等转运确认完成后才投递"
            ),
        )
        return command, ""

    # ---------- 转运结论 ----------

    def _move(
        self, labware: Labware, to_location_id: str | None, *, source: str, command_id: str = "",
        batch_id: str = "", barcode_confirmed: bool = False, reason: str = "", by: str = "",
    ) -> LabwareMove:
        move = LabwareMove(
            org_id=labware.org_id, labware_id=labware.id,
            from_location_id=labware.location_id or "", to_location_id=to_location_id or "",
            source=source, command_id=command_id, batch_id=batch_id,
            barcode_confirmed=barcode_confirmed, reason=reason, by=by,
        )
        self.db.add(move)
        labware.location_id = to_location_id
        labware.updated_at = now()
        labware.row_version = int(labware.row_version or 0) + 1
        return move

    def complete(self, command: Command, *, source: str = "transfer", by: str = "", note: str = "") -> str:
        """转运已完成（设备回执或现场核查）：写移位记录、更新载具位置。返回不一致原因（空 = 正常）。"""
        labware = self.db.get(Labware, command.labware_id) if command.labware_id else None
        destination = ((command.params or {}).get("to") or {}).get("location_id")
        if labware is None or not destination:
            return "转运指令缺少载具或目的位置"
        occupant = (
            self.db.query(Labware)
            .filter(Labware.location_id == destination, Labware.id != labware.id, Labware.state != "retired")
            .first()
        )
        if occupant is not None:
            # 目的位置上已经有另一块板（有人手动放了）：设备说送到了，现场却对不上
            self.mark_lost(labware, command, f"目的位置 {destination} 上已有 {occupant.barcode}，转运结论与现场不一致")
            return f"目的位置 {destination} 上已有载具 {occupant.barcode}，转运结论与现场不一致"
        self._move(
            labware, destination, source=source, command_id=command.id, batch_id=command.batch_id,
            barcode_confirmed=source == "transfer", reason=note, by=by,
        )
        if labware.state == "lost":
            labware.state = "idle"
        return ""

    def mark_lost(self, labware: Labware, command: Command | None, reason: str, by: str = "") -> None:
        """载具位置不再可信（部分执行、转运中被终止）：清空位置，必须扫码重新定位。"""
        self._move(
            labware, None, source="lost", command_id=command.id if command else "",
            batch_id=command.batch_id if command else labware.batch_id, reason=reason, by=by,
        )
        labware.state = "lost"

    def lost_by_command(self, command: Command, reason: str, by: str = "") -> None:
        labware = self.db.get(Labware, command.labware_id) if command.labware_id else None
        if labware is not None:
            self.mark_lost(labware, command, reason, by)

    # ---------- 登记、绑定、扫码放置 ----------

    def register(self, payload: dict, user: User) -> dict:
        barcode = (payload.get("barcode") or "").strip()
        if not barcode:
            raise ValidationFailed("载具条码必填")
        kind = self.db.get(LabwareType, payload.get("type_id") or "")
        if kind is None or not kind.active:
            raise ValidationFailed("载具类型不存在或已停用")
        if self.labware.query().filter(Labware.barcode == barcode).first():
            raise StateConflict(f"条码 {barcode} 已登记", code="barcode_taken")
        labware = Labware(org_id=self.ctx.org_id, barcode=barcode, type_id=kind.id, note=payload.get("note") or "")
        self.db.add(labware)
        self.db.flush()
        self.audit.record(user, "登记载具", labware.id, after=barcode, detail=f"{kind.name}（{kind.rows}×{kind.cols}）")
        if payload.get("location_id"):
            self.move(labware.id, {"to_location_id": payload["location_id"], "barcode": barcode,
                                   "reason": "登记时放置"}, user, commit=False)
        self.db.commit()
        return self.labware_out(labware)

    def move(self, labware_id: str, payload: dict, user: User, *, commit: bool = True) -> dict:
        """扫码人工放置 / 取下。条码必须与载具一致：放错板比不记录更糟。"""
        labware = self.labware.get(labware_id)
        if labware is None:
            raise NotFound("载具不存在")
        if (payload.get("barcode") or "").strip() != labware.barcode:
            raise ValidationFailed("扫到的条码与载具不一致", code="barcode_mismatch")
        target_id = payload.get("to_location_id") or None
        destination = self.db.get(Location, target_id) if target_id else None
        occupied_by = ""
        if target_id:
            occupant = (
                self.db.query(Labware)
                .filter(Labware.location_id == target_id, Labware.id != labware.id, Labware.state != "retired")
                .first()
            )
            occupied_by = occupant.barcode if occupant else ""
            if not occupied_by:
                pending = next(
                    (c for c in self._open_transfers()
                     if ((c.params or {}).get("to") or {}).get("location_id") == target_id), None,
                )
                occupied_by = f"在途转运 {pending.id[:8]} 的目的位置" if pending else ""
        spec = (
            LocationSpec(id=destination.id, kind=destination.kind, station_id=destination.station_id,
                         accepts=tuple(destination.accepts or ()), active=bool(destination.active))
            if destination else None
        )
        blocked = manual_move_blockers(
            destination=spec, labware_kind=self._kind_of(labware), occupied_by=occupied_by,
            in_transit=self._in_transit(labware) is not None, labware_state=labware.state,
        ) if target_id else (
            ["载具有在途转运指令：等转运完成或先核查结果未知的转运"] if self._in_transit(labware) else []
        )
        if blocked:
            raise StateConflict("不能放到这个位置", {"blocked": [{"key": "move", "label": b} for b in blocked]},
                                code="move_blocked")
        before = labware.location_id or "未上线"
        self._move(
            labware, target_id, source="manual", batch_id=labware.batch_id, barcode_confirmed=True,
            reason=(payload.get("reason") or "").strip(), by=user.display_name,
        )
        if labware.state == "lost":
            labware.state = "idle"
        self.audit.record(
            user, "扫码放置载具" if target_id else "扫码取下载具", labware.id,
            before=before, after=target_id or "未上线", detail=payload.get("reason") or "",
        )
        if commit:
            self.db.commit()
        return self.labware_out(labware)

    def bind(self, batch_id: str, labware_id: str, user: User) -> dict:
        from ..repositories.batches import BatchRepository

        batch = BatchRepository(self.db, self.ctx).get(batch_id)
        if batch is None:
            raise NotFound("批次不存在")
        if batch.state not in {"planned", "scheduled"}:
            raise StateConflict("只有未下发的批次可以绑定载具")
        labware = self.labware.get(labware_id)
        if labware is None:
            raise NotFound("载具不存在")
        if labware.state in {"retired", "lost"}:
            raise StateConflict(f"载具{'已报废' if labware.state == 'retired' else '位置未知，先扫码定位'}")
        if labware.batch_id and labware.batch_id != batch.id:
            other = self.db.get(Batch, labware.batch_id)
            if other is not None and other.state not in ENDED_BATCH_STATES:
                raise StateConflict(f"载具已绑定在用批次 {other.id}", code="labware_in_use")
        kind = self.db.get(LabwareType, labware.type_id)
        from ..models import Sample

        samples = self.db.query(Sample).filter(Sample.batch_id == batch.id).count()
        if kind is not None and kind.rows * kind.cols < samples:
            raise ValidationFailed(f"{kind.name} 只有 {kind.rows * kind.cols} 个位，批次有 {samples} 个样本")
        previous = self.for_batch(batch.id)
        if previous is not None and previous.id != labware.id:
            previous.batch_id = ""
        labware.batch_id = batch.id
        labware.row_version = int(labware.row_version or 0) + 1
        self.audit.record(user, "绑定载具", batch.id, after=labware.barcode,
                          detail=f"当前位置 {labware.location_id or '未上线'}")
        self.db.commit()
        return self.labware_out(labware)

    def unbind(self, batch_id: str, user: User) -> dict:
        from ..repositories.batches import BatchRepository

        batch = BatchRepository(self.db, self.ctx).get(batch_id)
        if batch is None:
            raise NotFound("批次不存在")
        if batch.state not in {"planned", "scheduled"}:
            raise StateConflict("批次已下发，不能解绑载具")
        labware = self.for_batch(batch.id)
        if labware is None:
            return {"unbound": False}
        labware.batch_id = ""
        self.audit.record(user, "解绑载具", batch.id, before=labware.barcode)
        self.db.commit()
        return {"unbound": True}

    # ---------- 位置与类型维护 ----------

    def create_location(self, payload: dict, user: User) -> dict:
        from ..domain.labware import KINDS

        ident = (payload.get("id") or "").strip()
        if not ident:
            raise ValidationFailed("位置编号必填")
        if self.db.get(Location, ident):
            raise StateConflict(f"位置 {ident} 已存在")
        if payload.get("kind", "nest") not in KINDS:
            raise ValidationFailed(f"位置类型只能是 {'、'.join(KINDS)}")
        if payload.get("kind", "nest") == "nest" and not self.db.get(Station, payload.get("station_id") or ""):
            raise ValidationFailed("工位放置位必须指向一个工位")
        location = Location(
            id=ident, name=payload.get("name") or ident, kind=payload.get("kind", "nest"),
            station_id=payload.get("station_id") or "", group=payload.get("group") or "",
            position=int(payload.get("position") or 0), accepts=list(payload.get("accepts") or []),
        )
        self.db.add(location)
        self.audit.record(user, "登记位置", ident, after=location.kind, detail=location.station_id or location.group)
        self.db.commit()
        return self.location_out(location, {})

    def set_location_active(self, location_id: str, active: bool, user: User) -> dict:
        location = self.db.get(Location, location_id)
        if location is None:
            raise NotFound("位置不存在")
        occupied = self._occupied()
        if not active and location_id in occupied:
            raise StateConflict(f"{location_id} 上有载具或正有转运送过来（{occupied[location_id]}），不能停用")
        location.active = active
        location.row_version = int(location.row_version or 0) + 1
        self.audit.record(user, "启用位置" if active else "停用位置", location_id)
        self.db.commit()
        return self.location_out(location, occupied)

    # ---------- 输出 ----------

    def labware_out(self, labware: Labware) -> dict:
        kind = self.db.get(LabwareType, labware.type_id)
        batch = self.db.get(Batch, labware.batch_id) if labware.batch_id else None
        transit = self._in_transit(labware)
        return {
            "id": labware.id, "barcode": labware.barcode, "type_id": labware.type_id,
            "type_name": kind.name if kind else labware.type_id,
            "rows": kind.rows if kind else 1, "cols": kind.cols if kind else 1,
            "batch_id": labware.batch_id,
            "batch_active": bool(batch and batch.state not in ENDED_BATCH_STATES),
            "location_id": labware.location_id or "", "state": labware.state,
            "in_transit": (
                {"command_id": transit.id, "state": transit.state, "carrier": transit.station_id,
                 "to": ((transit.params or {}).get("to") or {}).get("location_id", "")}
                if transit else None
            ),
            "row_version": labware.row_version,
        }

    @staticmethod
    def location_out(location: Location, occupied: dict[str, str]) -> dict:
        return {
            "id": location.id, "name": location.name, "kind": location.kind,
            "station_id": location.station_id, "group": location.group, "position": location.position,
            "accepts": location.accepts or [], "active": location.active,
            "occupant": occupied.get(location.id, ""),
        }

    def list_labware(self, keyword: str = "") -> list[dict]:
        query = self.labware.query()
        if keyword:
            query = query.filter(Labware.barcode.ilike(f"%{keyword}%"))
        return [self.labware_out(row) for row in query.order_by(Labware.barcode).limit(500).all()]

    def moves(self, labware_id: str) -> list[dict]:
        labware = self.labware.get(labware_id)
        if labware is None:
            raise NotFound("载具不存在")
        rows = (
            self.db.query(LabwareMove).filter(LabwareMove.labware_id == labware.id)
            .order_by(LabwareMove.at.desc()).limit(200).all()
        )
        return [
            {"id": row.id, "from": row.from_location_id, "to": row.to_location_id, "source": row.source,
             "command_id": row.command_id, "batch_id": row.batch_id,
             "barcode_confirmed": row.barcode_confirmed, "reason": row.reason, "by": row.by,
             "at": row.at.isoformat(timespec="seconds")}
            for row in rows
        ]

    def types(self) -> list[dict]:
        return [
            {"id": row.id, "name": row.name, "kind": row.kind, "rows": row.rows, "cols": row.cols,
             "active": row.active}
            for row in self.db.query(LabwareType).order_by(LabwareType.id).all()
        ]

    def locations(self) -> list[dict]:
        occupied = self._occupied()
        return [
            self.location_out(row, occupied)
            for row in self.db.query(Location).order_by(Location.group, Location.station_id, Location.position, Location.id).all()
        ]

    def floor(self) -> dict:
        """现场总览：每个工位的实时状态、在途指令、放置位上的板；板库槽位；在途转运。

        载具条码只对本组织可见；别的组织的板只显示「已占用」。
        """
        from ..repositories.execution import MOTION

        mine = {row.id: row for row in self.labware.query().all()}
        where = {
            row.location_id: row for row in self.db.query(Labware)
            .filter(Labware.location_id.isnot(None), Labware.state != "retired").all()
        }
        moment = now()
        adapters = {row.station_id: row for row in self.db.query(Adapter).all()}
        open_commands = (
            self.db.query(Command)
            .filter(Command.state.in_(["sent", "accepted", "running", "unknown", "manual"]))
            .all()
        )
        by_station: dict[str, list[Command]] = {}
        for command in open_commands:
            by_station.setdefault(command.station_id, []).append(command)
        locations = self.db.query(Location).order_by(Location.group, Location.position, Location.id).all()
        in_flight_to = {
            ((c.params or {}).get("to") or {}).get("location_id"): c
            for c in open_commands if c.type == TRANSFER
        }

        def slot(location: Location) -> dict:
            found = where.get(location.id)
            visible = found is not None and found.id in mine
            incoming = in_flight_to.get(location.id)
            return {
                "id": location.id, "name": location.name, "kind": location.kind, "active": location.active,
                "position": location.position,
                "labware": (
                    {"id": found.id, "barcode": found.barcode, "batch_id": found.batch_id, "state": found.state}
                    if visible else ({"id": "", "barcode": "已占用", "batch_id": "", "state": "other"} if found else None)
                ),
                "incoming": (
                    {"command_id": incoming.id, "state": incoming.state,
                     "barcode": (incoming.params or {}).get("barcode", "") if incoming.org_id == self.ctx.org_id else ""}
                    if incoming else None
                ),
            }

        stations = []
        for station in self.db.query(Station).order_by(Station.island, Station.id).all():
            record = adapters.get(station.id)
            heartbeat_age = (
                round((moment - record.last_heartbeat).total_seconds()) if record and record.last_heartbeat else None
            )
            commands = by_station.get(station.id, [])
            stations.append({
                "id": station.id, "name": station.name, "island": station.island, "status": station.status,
                "retired": station.retired, "channels": station.channels or 1,
                "capabilities": sorted((station.limits or {}).keys()),
                "adapter": (
                    {"connected": record.connected, "enabled": record.enabled, "interlock": record.site_interlock,
                     "accepts_commands": record.accepts_commands, "kind": record.kind,
                     "heartbeat_age_sec": heartbeat_age, "current_command_id": record.current_command_id}
                    if record else None
                ),
                "commands": [
                    {"id": c.id, "type": c.type, "state": c.state,
                     "batch_id": c.batch_id if c.org_id == self.ctx.org_id else "",
                     "step_index": c.step_index, "motion": c.type in MOTION,
                     "since": (c.started_at or c.created_at).isoformat(timespec="seconds")}
                    for c in sorted(commands, key=lambda c: c.created_at)
                ],
                "nests": [slot(loc) for loc in locations if loc.kind == "nest" and loc.station_id == station.id],
            })
        groups: dict[str, list[dict]] = {}
        for location in locations:
            if location.kind != "nest":
                groups.setdefault(location.group or location.kind, []).append(slot(location))
        transfers = [
            {"id": c.id, "state": c.state, "carrier": c.station_id,
             "batch_id": c.batch_id if c.org_id == self.ctx.org_id else "",
             "barcode": (c.params or {}).get("barcode", "") if c.org_id == self.ctx.org_id else "",
             "from": ((c.params or {}).get("from") or {}).get("location_id", ""),
             "to": ((c.params or {}).get("to") or {}).get("location_id", ""),
             "since": (c.started_at or c.created_at).isoformat(timespec="seconds")}
            for c in open_commands if c.type == TRANSFER
        ]
        lost = [
            {"id": row.id, "barcode": row.barcode, "batch_id": row.batch_id}
            for row in mine.values() if row.state == "lost"
        ]
        return {
            "now": moment.isoformat(timespec="seconds"),
            "tracking": bool(locations),
            "stations": stations,
            "storage": [{"group": name, "slots": slots} for name, slots in groups.items()],
            "transfers": transfers,
            "lost": lost,
        }
