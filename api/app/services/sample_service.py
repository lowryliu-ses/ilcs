"""样本中心：物理样本、运行分配与流转。

物理实体和「这一次运行里的位置」是两件事。登记样本不需要先有批次，也不需要已有
检测结果；重测不复制物理样本，只新建检测任务或运行分配。
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..core.clock import now
from ..core.context import AccessContext
from ..core.db import dec
from ..core.errors import NotFound, StateConflict, ValidationFailed
from ..domain import inventory
from ..models import Labware, LabwareType, Location, PhysicalSample, SampleTransfer, SlotOccupancy, User
from ..repositories.batches import AnalysisTaskRepository, SampleRepository
from ..repositories.files import FileRepository
from ..repositories.organization import ProjectRepository
from ..repositories.samples import (
    PhysicalSampleRepository, SampleTransferRepository, SlotOccupancyRepository,
)
from .audit_service import AuditService

LIFECYCLE = ("registered", "received", "in_use", "stored", "exhausted", "disposed")
LIFECYCLE_LABEL = {
    "registered": "已登记", "received": "已收样", "in_use": "使用中",
    "stored": "已入库", "exhausted": "已用尽", "disposed": "已处置",
}
TRANSFER_KINDS = ("receive", "handover", "split", "store", "dispose", "move")
TRANSFER_LABEL = {
    "receive": "收样", "handover": "交接", "split": "分样", "store": "入库存放",
    "dispose": "处置", "move": "移动",
}
ZERO = Decimal("0.000000")


class SampleService:
    def __init__(self, db: Session, ctx: AccessContext):
        self.db = db
        self.ctx = ctx
        self.samples = PhysicalSampleRepository(db, ctx)
        self.assignments = SampleRepository(db, ctx)
        self.slots = SlotOccupancyRepository(db, ctx)
        self.transfers = SampleTransferRepository(db, ctx)
        self.tasks = AnalysisTaskRepository(db, ctx)
        self.projects = ProjectRepository(db, ctx)
        self.files = FileRepository(db, ctx)
        self.audit = AuditService(db, ctx)

    # ---------- 读 ----------

    def out(self, sample: PhysicalSample) -> dict:
        return {
            "id": sample.id,
            "barcode": sample.barcode,
            "project_id": sample.project_id,
            "source": sample.source,
            "sample_type": sample.sample_type,
            "parent_id": sample.parent_id,
            "quantity": f"{dec(sample.quantity):f}" if sample.quantity is not None else None,
            "unit": sample.unit,
            "storage_condition": sample.storage_condition,
            "current_location": sample.current_location,
            "location": self.location_of(sample),
            "location_note": sample.location_note,
            "custodian": sample.custodian,
            "lifecycle_state": sample.lifecycle_state,
            "lifecycle_label": LIFECYCLE_LABEL.get(sample.lifecycle_state, sample.lifecycle_state),
            "note": sample.note,
            "origin": sample.origin,
            "created_at": sample.created_at.isoformat(timespec="seconds"),
            "row_version": sample.row_version,
            "child_count": len(self.samples.children(sample.id)),
            "assignment_count": len(self.assignments.for_physical(sample.id)),
        }

    def location_of(self, sample: PhysicalSample) -> dict:
        """结构化位置。在载具上时，位置 = 载具当前所在的位置 + 孔位；否则是登记过的库位 / 放置位；
        都没有时只有文本位置（历史数据、外部交接）。"""
        labware = self.db.get(Labware, sample.labware_id) if sample.labware_id else None
        place_id = (labware.location_id if labware is not None else None) or sample.location_id
        place = self.db.get(Location, place_id) if place_id else None
        kind = self.db.get(LabwareType, labware.type_id) if labware is not None else None
        text = sample.current_location
        if labware is not None:
            text = f"{labware.barcode} · {sample.well or '—'}" + (f" @ {place.name}" if place else "（载具未上线）")
        elif place is not None:
            text = place.name
        return {
            "kind": "labware" if labware is not None else "location" if place is not None else "text" if text else "none",
            "labware": {"id": labware.id, "barcode": labware.barcode, "type_name": kind.name if kind else labware.type_id,
                        "state": labware.state} if labware is not None else None,
            "well": sample.well if labware is not None else "",
            "place": {"id": place.id, "name": place.name, "kind": place.kind, "station_id": place.station_id}
            if place is not None else None,
            "text": text,
        }

    def find(self, code: str) -> PhysicalSample | None:
        """按样本号或条码（扫码）找样本。"""
        return self.samples.get(code) or self.samples.by_barcode(code)

    def qr(self, code: str) -> dict:
        sample = self.find(code)
        if sample is None:
            raise NotFound("样本不存在")
        content = sample.barcode or sample.id
        return {"id": sample.id, "content": content, "svg": qr_svg(content),
                "label": [sample.id, sample.sample_type, sample.source][:3]}

    def page(self, offset: int, limit: int, keyword: str = "", state: str | None = None,
             project_id: str = ""):
        rows, total = self.samples.page(offset, limit, keyword, state, project_id)
        return [self.out(row) for row in rows], total

    def detail(self, sample_id: str) -> dict:
        sample = self.find(sample_id)
        if not sample:
            raise NotFound("样本不存在")
        assignments = self.assignments.for_physical(sample.id)
        tasks = self.tasks.for_physical(sample.id)
        return {
            **self.out(sample),
            # 来源谱系、运行分配、流转历史分开呈现，不混成一条「位置」
            "lineage": [self.out(row) for row in reversed(self.samples.lineage(sample))],
            "children": [self.out(row) for row in self.samples.children(sample.id)],
            "assignments": [
                {
                    "id": row.id, "batch_id": row.batch_id, "container_id": row.container_id,
                    "well": row.well, "condition_group": row.condition_group,
                    "condition_label": row.condition_label, "repeat": row.repeat,
                    "state": row.state, "legacy_quality": row.quality,
                }
                for row in assignments
            ],
            "transfers": [self.transfer_out(row) for row in self.transfers.for_sample(sample.id)],
            "slots": [
                {
                    "container_id": row.container_id, "well": row.well, "labware_id": row.labware_id or "",
                    "occupied_at": row.occupied_at.isoformat(timespec="seconds"),
                    "released_at": row.released_at.isoformat(timespec="seconds") if row.released_at else None,
                }
                for row in self.slots.for_sample(sample.id)
            ],
            "analysis_tasks": [
                {
                    "id": row.id, "round_no": row.round_no, "state": row.state,
                    "method": row.method, "method_version": row.method_version,
                    "required_metrics": row.required_metrics or [],
                    "retest_of": row.retest_of,
                    "created_at": row.created_at.isoformat(timespec="seconds"),
                }
                for row in tasks
            ],
            "attachments": [
                {"id": f.id, "filename": f.filename, "media_type": f.media_type, "state": f.state}
                for f in self.files.for_ref("sample", sample.id)
            ],
            "audit": [
                {
                    "time": e.time.isoformat(timespec="seconds"), "user": e.user, "action": e.action,
                    "before": e.before, "after": e.after, "detail": e.detail,
                }
                for e in self.audit.for_target(sample.id)
            ],
        }

    def transfer_out(self, row: SampleTransfer) -> dict:
        return {
            "id": row.id,
            "physical_sample_id": row.physical_sample_id,
            "kind": row.kind,
            "kind_label": TRANSFER_LABEL.get(row.kind, row.kind),
            "from_location": row.from_location,
            "to_location": row.to_location,
            "from_party": row.from_party,
            "to_party": row.to_party,
            "quantity": f"{dec(row.quantity):f}" if row.quantity is not None else None,
            "unit": row.unit,
            "confirm_method": row.confirm_method,
            "note": row.note,
            "created_by": row.created_by,
            "occurred_at": row.occurred_at.isoformat(timespec="seconds"),
        }

    # ---------- 写 ----------

    def register(self, payload: dict, user: User) -> dict:
        """手工登记。不要求批次，也不要求已有检测结果。"""
        barcode = (payload.get("barcode") or "").strip()
        if barcode and self.samples.by_barcode(barcode):
            raise StateConflict(f"条码 {barcode} 在本组织内已存在", code="barcode_taken")
        project_id = payload.get("project_id") or ""
        if project_id and not self.projects.get(project_id):
            raise NotFound("项目不存在或不在当前组织范围内")
        parent_id = payload.get("parent_id") or None
        if parent_id and not self.samples.get(parent_id):
            # 跨组织的父样本在这里就是「不存在」
            raise NotFound("母样本不存在或不在当前组织范围内")
        quantity = None
        if payload.get("quantity") is not None:
            try:
                quantity = inventory.positive(payload["quantity"], "样本数量")
            except inventory.QuantityError as exc:
                raise ValidationFailed(str(exc)) from exc
        sample_id = (payload.get("id") or "").strip() or self.samples.next_id(
            f"PS-{now():%y%m}-"
        )
        if self.samples.get(sample_id):
            raise StateConflict(f"样本编号 {sample_id} 已存在")
        sample = PhysicalSample(
            id=sample_id, org_id=self.ctx.org_id, project_id=project_id,
            barcode=barcode or sample_id, source=payload.get("source", ""),
            sample_type=payload.get("sample_type", ""), parent_id=parent_id,
            quantity=quantity, unit=payload.get("unit", ""),
            storage_condition=payload.get("storage_condition", ""),
            current_location=payload.get("current_location", ""),
            custodian=payload.get("custodian", "") or user.display_name,
            lifecycle_state=payload.get("lifecycle_state", "registered"),
            note=payload.get("note", ""), origin="registered", created_by=user.id,
        )
        self.samples.add(sample)
        self.audit.record(
            user, "登记样本", sample.id, before="—", after="已登记",
            detail=(
                f"条码 {sample.barcode}；来源 {sample.source or '—'}；"
                f"{f'{quantity:f}{sample.unit}' if quantity is not None else '数量未录'}"
            ),
            object_version=sample.row_version,
        )
        self.db.commit()
        return self.out(sample)

    def receive(self, sample_id: str, payload: dict, user: User) -> dict:
        """扫码接收。重复扫描靠 event_key 去重，不多写一条交接。"""
        sample = self.samples.get(sample_id) or self.samples.by_barcode(sample_id)
        if not sample:
            raise NotFound("样本不存在")
        event_key = (payload.get("event_key") or "").strip()
        existing = self.transfers.find_event(sample.id, event_key)
        if existing is not None:
            return {
                **self.out(sample), "replayed": True,
                "transfer": self.transfer_out(existing),
                "hint": "该扫码事件已记录，未重复写入交接",
            }
        before = sample.lifecycle_state
        transfer = self._write_transfer(
            sample, "receive", payload, user, event_key,
            to_location=payload.get("to_location") or payload.get("location") or "",
        )
        sample.lifecycle_state = "received"
        sample.current_location = transfer.to_location or sample.current_location
        sample.custodian = payload.get("to_party") or user.display_name
        sample.updated_at = now()
        self.samples.bump(sample)
        self.audit.record(
            user, "收样", sample.id, before=LIFECYCLE_LABEL.get(before, before), after="已收样",
            detail=f"位置 {sample.current_location or '—'}；确认方式 {transfer.confirm_method}",
            object_version=sample.row_version,
        )
        self.db.commit()
        return {**self.out(sample), "replayed": False, "transfer": self.transfer_out(transfer)}

    def transfer(self, sample_id: str, payload: dict, user: User) -> dict:
        sample = self.samples.get(sample_id) or self.samples.by_barcode(sample_id)
        if not sample:
            raise NotFound("样本不存在")
        kind = payload.get("kind", "handover")
        if kind not in TRANSFER_KINDS:
            raise ValidationFailed(f"流转类型只能是 {'、'.join(TRANSFER_KINDS)}")
        event_key = (payload.get("event_key") or "").strip()
        existing = self.transfers.find_event(sample.id, event_key)
        if existing is not None:
            return {
                **self.out(sample), "replayed": True,
                "transfer": self.transfer_out(existing),
                "hint": "该交接事件已记录，未重复写入",
            }
        place = None
        if payload.get("to_location_id"):
            place = self.db.get(Location, payload["to_location_id"])
            if place is None or not place.active:
                raise NotFound(f"位置 {payload['to_location_id']} 不存在或已停用")
            if sample.labware_id and kind in {"store", "move", "handover"}:
                labware = self.db.get(Labware, sample.labware_id)
                raise StateConflict(
                    f"样本还在载具 {labware.barcode if labware else sample.labware_id} 的孔位 {sample.well} 上；"
                    "先把它从载具取出（结束批次或释放孔位），再登记去向",
                    code="sample_on_labware",
                )
            payload = {**payload, "to_location": payload.get("to_location") or place.name}
        transfer = self._write_transfer(sample, kind, payload, user, event_key)
        if place is not None:
            transfer.to_location_id = place.id
            sample.location_id = place.id
        elif payload.get("to_location") and kind in {"store", "move", "handover"}:
            # 去向写成自由文本：结构化位置作废，免得界面上两处位置对不上
            sample.location_id = None
        before = sample.lifecycle_state
        if kind == "store":
            sample.lifecycle_state = "stored"
        elif kind == "dispose":
            sample.lifecycle_state = "disposed"
        elif kind == "handover":
            sample.lifecycle_state = "in_use" if before in {"received", "in_use"} else before
        sample.current_location = transfer.to_location or sample.current_location
        if payload.get("to_party"):
            sample.custodian = payload["to_party"]
        sample.updated_at = now()
        self.samples.bump(sample)
        self.audit.record(
            user, TRANSFER_LABEL.get(kind, kind), sample.id,
            before=transfer.from_location or "—", after=transfer.to_location or "—",
            detail=f"{transfer.from_party or '—'} → {transfer.to_party or '—'}；{transfer.note}",
            object_version=sample.row_version,
        )
        self.db.commit()
        return {**self.out(sample), "replayed": False, "transfer": self.transfer_out(transfer)}

    def _write_transfer(
        self, sample: PhysicalSample, kind: str, payload: dict, user: User, event_key: str,
        to_location: str | None = None,
    ) -> SampleTransfer:
        quantity = None
        if payload.get("quantity") is not None:
            try:
                quantity = inventory.positive(payload["quantity"], "交接数量")
            except inventory.QuantityError as exc:
                raise ValidationFailed(str(exc)) from exc
        transfer = SampleTransfer(
            org_id=self.ctx.org_id, physical_sample_id=sample.id, event_key=event_key, kind=kind,
            from_location=payload.get("from_location") or sample.current_location,
            to_location=to_location if to_location is not None else payload.get("to_location", ""),
            from_party=payload.get("from_party") or sample.custodian,
            to_party=payload.get("to_party", ""), quantity=quantity,
            unit=payload.get("unit") or sample.unit,
            confirm_method=payload.get("confirm_method", "barcode"),
            note=payload.get("note", ""), created_by=user.id,
            occurred_at=payload.get("occurred_at") or now(),
        )
        self.transfers.add(transfer)
        try:
            self.db.flush()
        except IntegrityError:
            self.db.rollback()
            existing = self.transfers.find_event(sample.id, event_key)
            if existing is None:
                raise
            return existing
        return transfer

    def split(self, sample_id: str, payload: dict, user: User) -> dict:
        """分样。校验母样剩余量、子样数量与明确记录的损耗，不允许生成超量子样。"""
        parent = self.samples.get(sample_id)
        if not parent:
            raise NotFound("母样本不存在")
        if parent.quantity is None:
            raise StateConflict(
                "母样本没有录入数量，无法核对分样是否超量；请先补录数量",
                code="parent_quantity_missing",
            )
        if parent.lifecycle_state in {"disposed", "exhausted"}:
            raise StateConflict(f"母样本状态为 {parent.lifecycle_state}，不能分样")
        children = payload.get("children") or []
        if not children:
            raise ValidationFailed("至少要有一个子样")
        try:
            loss = inventory.q(payload.get("loss", 0))
            amounts = [inventory.positive(row["quantity"], "子样数量") for row in children]
        except inventory.QuantityError as exc:
            raise ValidationFailed(str(exc)) from exc
        total = sum(amounts, ZERO) + loss
        remaining = dec(parent.quantity)
        if total > remaining:
            raise StateConflict(
                f"子样合计 {sum(amounts, ZERO):f} 加损耗 {loss:f} 超出母样剩余 {remaining:f}{parent.unit}",
                {"blocked": [{"key": "quantity", "label": "分样不能生成超量子样"}]},
                code="split_over_quantity",
            )
        if loss > ZERO and not (payload.get("loss_reason") or "").strip():
            raise ValidationFailed("记录损耗必须写明原因", code="loss_reason_required")

        created: list[PhysicalSample] = []
        for index, (row, amount) in enumerate(zip(children, amounts), start=1):
            child_id = (row.get("id") or "").strip() or f"{parent.id}-{index:02d}"
            if self.samples.get(child_id):
                raise StateConflict(f"子样编号 {child_id} 已存在")
            barcode = (row.get("barcode") or "").strip() or child_id
            if self.samples.by_barcode(barcode):
                raise StateConflict(f"子样条码 {barcode} 已存在", code="barcode_taken")
            child = PhysicalSample(
                id=child_id, org_id=self.ctx.org_id, project_id=parent.project_id,
                barcode=barcode, source=f"分自 {parent.id}",
                sample_type=row.get("sample_type") or parent.sample_type, parent_id=parent.id,
                quantity=amount, unit=parent.unit,
                storage_condition=row.get("storage_condition") or parent.storage_condition,
                current_location=row.get("current_location") or parent.current_location,
                custodian=user.display_name, lifecycle_state="registered",
                note=row.get("note", ""), origin="split", created_by=user.id,
            )
            self.samples.add(child)
            created.append(child)

        parent.quantity = remaining - total
        if parent.quantity <= ZERO:
            parent.lifecycle_state = "exhausted"
        parent.updated_at = now()
        self.samples.bump(parent)
        self._write_transfer(
            parent, "split",
            {
                "quantity": total, "unit": parent.unit,
                "note": (
                    f"生成 {len(created)} 个子样；损耗 {loss:f}"
                    f"{('（' + payload['loss_reason'] + '）') if loss > ZERO else ''}"
                ),
                "to_location": parent.current_location,
            },
            user, event_key=(payload.get("event_key") or f"split-{parent.id}-{now():%Y%m%d%H%M%S}"),
        )
        self.audit.record(
            user, "分样", parent.id, before=f"{remaining:f}{parent.unit}",
            after=f"{dec(parent.quantity):f}{parent.unit}",
            detail=(
                f"子样 {'、'.join(c.id for c in created)}；合计 {sum(amounts, ZERO):f}；"
                f"损耗 {loss:f}{('：' + payload['loss_reason']) if loss > ZERO else ''}"
            ),
            object_version=parent.row_version,
        )
        self.db.commit()
        return {
            "parent": self.out(parent),
            "children": [self.out(child) for child in created],
            "loss": f"{loss:f}",
        }

    def dispose(self, sample_id: str, reason: str, user: User) -> dict:
        sample = self.samples.get(sample_id)
        if not sample:
            raise NotFound("样本不存在")
        if not reason.strip():
            raise ValidationFailed("处置必须填写理由")
        if sample.lifecycle_state == "disposed":
            raise StateConflict("样本已处置")
        before = sample.lifecycle_state
        sample.lifecycle_state = "disposed"
        sample.updated_at = now()
        self.samples.bump(sample)
        for slot in self.slots.for_sample(sample.id):
            if slot.released_at is None:
                slot.released_at = now()
        self.audit.record(
            user, "处置样本", sample.id, before=LIFECYCLE_LABEL.get(before, before), after="已处置",
            detail=reason, object_version=sample.row_version,
        )
        self.db.commit()
        return self.out(sample)

    # ---------- 孔位占用 ----------

    def occupy_slot(
        self, container_id: str, well: str, physical_sample_id: str, assignment_id: str = "",
    ) -> SlotOccupancy:
        """占用在途孔位。唯一约束负责并发，不做「先查再插」。"""
        occupancy = SlotOccupancy(
            org_id=self.ctx.org_id, container_id=container_id, well=well,
            physical_sample_id=physical_sample_id, assignment_id=assignment_id,
        )
        self.db.add(occupancy)
        try:
            self.db.flush()
        except IntegrityError:
            self.db.rollback()
            live = self.slots.live(container_id, well)
            raise StateConflict(
                f"容器 {container_id} 的孔位 {well} 已被在途样本 "
                f"{live.physical_sample_id if live else '未知'} 占用",
                {"blocked": [{"key": "slot", "label": "一个在途孔位同时只能分配给一个样本"}]},
                code="slot_occupied",
            ) from None
        return occupancy

    def release_slots(self, container_id: str) -> int:
        """释放孔位占用（批次结束 / 终止 / 删除）。样本随之从载具上解开，最后所在的载具孔位留作文本位置。"""
        released = 0
        for row in self.slots.query().filter_by(container_id=container_id, released_at=None).all():
            row.released_at = now()
            released += 1
            sample = self.db.get(PhysicalSample, row.physical_sample_id)
            if sample is not None and row.labware_id and sample.labware_id == row.labware_id:
                labware = self.db.get(Labware, row.labware_id)
                sample.current_location = f"{labware.barcode if labware else row.labware_id} · {sample.well or row.well}"
                sample.labware_id = None
                sample.well = ""
        return released

    def link_labware(self, container_id: str, labware: Labware | None) -> int:
        """批次绑定（或解绑）实体载具：在途孔位占用与样本的结构化位置指向这块载具。

        载具上原来的样本（上一次使用留下的关联）先解开：一块板同一时刻只装一批样本。
        """
        from ..domain.labware import physical_wells
        from ..models import Sample

        live = self.slots.query().filter_by(container_id=container_id, released_at=None).all()
        mine = {row.physical_sample_id for row in live}
        linked = 0
        # 布局孔位（逻辑位）按布局顺序对到载具的实体孔位
        mapping: dict[str, str] = {}
        if labware is not None:
            kind = self.db.get(LabwareType, labware.type_id)
            order = {row.id: row.position for row in self.db.query(Sample).filter(
                Sample.id.in_([row.assignment_id for row in live if row.assignment_id] or [""]),
            ).all()}
            logical = [row.well for row in sorted(live, key=lambda row: (order.get(row.assignment_id, 0), row.well))]
            mapping = physical_wells(kind.rows if kind else 1, kind.cols if kind else len(logical), logical)
        if labware is not None:
            for stale in self.db.query(PhysicalSample).filter(
                PhysicalSample.labware_id == labware.id, PhysicalSample.id.notin_(mine or {""}),
            ).all():
                stale.labware_id = None
                stale.well = ""
        for row in live:
            row.labware_id = labware.id if labware is not None else None
            sample = self.db.get(PhysicalSample, row.physical_sample_id)
            if sample is None:
                continue
            if labware is not None:
                sample.labware_id = labware.id
                sample.well = mapping.get(row.well, row.well)
                sample.location_id = None
                linked += 1
            elif sample.labware_id:
                sample.labware_id = None
                sample.well = ""
        return linked


def qr_svg(content: str) -> str:
    """标签二维码（SVG）。内容就是样本号 / 条码，扫码后走同一个查找入口。"""
    import io

    import segno

    buffer = io.BytesIO()
    segno.make(content, error="m").save(buffer, kind="svg", scale=4, border=2, xmldecl=False, svgns=True)
    return buffer.getvalue().decode()
