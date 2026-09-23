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
from ..models import PhysicalSample, SampleTransfer, SlotOccupancy, User
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

    def page(self, offset: int, limit: int, keyword: str = "", state: str | None = None,
             project_id: str = ""):
        rows, total = self.samples.page(offset, limit, keyword, state, project_id)
        return [self.out(row) for row in rows], total

    def detail(self, sample_id: str) -> dict:
        sample = self.samples.get(sample_id)
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
                    "container_id": row.container_id, "well": row.well,
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
        transfer = self._write_transfer(sample, kind, payload, user, event_key)
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
        released = 0
        for row in self.slots.query().filter_by(container_id=container_id, released_at=None).all():
            row.released_at = now()
            released += 1
        return released
