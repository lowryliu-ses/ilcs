"""物料主数据、批号有效性与废液桶。库存算术在 inventory_service 里。"""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy.orm import Session

from ..core.clock import today_iso
from ..core.config import settings
from ..core.context import AccessContext
from ..core.db import dec
from ..core.errors import DomainError, NotFound, StateConflict, ValidationFailed
from ..domain import inventory
from ..domain.lifecycle import (
    lot_delete_blockers, lot_editable_fields, reject_uneditable, waste_delete_blockers,
)
from ..models import InventoryEvent, InventoryLedger, Lot, Material, User, WasteTank
from ..repositories.governance import AlarmRepository
from ..repositories.materials import (
    LotRepository, MaterialRepository, ReservationRepository, WasteRepository,
)
from .audit_service import AuditService
from .identity_service import IdentityService
from .inventory_service import InventoryService

WASTE_WARN_PCT = 75


class MaterialService:
    def __init__(self, db: Session, ctx: AccessContext):
        self.db = db
        self.ctx = ctx
        self.lots = LotRepository(db, ctx)
        self.materials = MaterialRepository(db, ctx)
        self.reservations = ReservationRepository(db, ctx)
        self.waste = WasteRepository(db, ctx)
        self.alarms = AlarmRepository(db, ctx)
        self.audit = AuditService(db, ctx)
        self.identity = IdentityService(db, ctx)
        self.inventory = InventoryService(db, ctx)

    # ---------- 物料主数据 ----------

    def material_out(self, material: Material) -> dict:
        lots = [row for row in self.lots.list() if row.material_id == material.id]
        return {
            "id": material.id,
            "code": material.code,
            "name": material.name,
            "base_unit": material.base_unit,
            "category": material.category,
            "cas": material.cas,
            "conversions": material.conversions or {},
            "external_ref": material.external_ref,
            "ghs": material.ghs or [],
            "state": material.state,
            "lot_count": len(lots),
        }

    def list_materials(self) -> list[dict]:
        return [self.material_out(row) for row in self.materials.list()]

    def create_material(self, payload: dict, user: User) -> dict:
        code = (payload.get("code") or "").strip()
        if not code:
            raise ValidationFailed("物料编码必填")
        if self.materials.by_code(code):
            raise StateConflict(f"物料编码 {code} 已存在")
        material = Material(
            code=code, name=payload["name"], base_unit=payload["base_unit"],
            category=payload.get("category", ""), cas=payload.get("cas", ""),
            conversions=payload.get("conversions") or {},
            external_ref=payload.get("external_ref", ""), ghs=payload.get("ghs") or [],
        )
        self.materials.add(material)
        self.audit.record(
            user, "登记物料", material.id, before="—", after=material.name,
            detail=f"{code}；基础单位 {material.base_unit}",
        )
        self.db.commit()
        return self.material_out(material)

    # ---------- 批号 ----------

    def lot_out(self, lot: Lot) -> dict:
        balances = self.inventory.balances(lot)
        deadline, basis = inventory.effective_expiry(lot.expiry, lot.open_expiry)
        today = today_iso()
        material = self.materials.get(lot.material_id) if lot.material_id else None
        return {
            "id": lot.id,
            "material": lot.material,
            "material_id": lot.material_id,
            "material_code": material.code if material else "",
            "base_unit": material.base_unit if material else lot.unit,
            "cas": lot.cas,
            "type": lot.type,
            # 三个量分列，界面不能只显示一个「库存」
            "qty": f"{balances['balance']:f}",
            "reserved": f"{balances['outstanding']:f}",
            "outstanding": f"{balances['outstanding']:f}",
            "issued_outstanding": f"{balances['issued_outstanding']:f}",
            "available": f"{balances['available']:f}",
            "opening_balance": f"{dec(lot.opening_balance):f}",
            "unit": lot.unit,
            "release": lot.release,
            "sds": lot.sds,
            "compat": lot.compat,
            "expiry": lot.expiry,
            "opened": lot.opened,
            "open_expiry": lot.open_expiry,
            "open_expiry_basis": lot.open_expiry_basis,
            "effective_expiry": deadline,
            "effective_expiry_basis": basis,
            "expired": bool(deadline and deadline < today),
            "expiring_soon": bool(
                deadline and deadline <= self._in_days(settings.lot_expiry_warn_days)
            ),
            "storage": lot.storage,
            "ghs": lot.ghs,
            "state": lot.state,
            "scrap_reason": lot.scrap_reason,
            "usable": not self.inventory.lot_blockers(lot),
            "use_blockers": self.inventory.lot_blockers(lot),
            "editable_fields": sorted(lot_editable_fields(lot.release, lot.state)),
            "delete_blockers": lot_delete_blockers(
                self.reservations.count_for_lot(lot.id), lot.state
            ),
        }

    @staticmethod
    def _in_days(days: int) -> str:
        return (date.today() + timedelta(days=days)).isoformat()

    def list_lots(self) -> list[dict]:
        return [self.lot_out(lot) for lot in self.lots.list()]

    def page_lots(self, offset: int, limit: int, keyword: str = "", state: str | None = None):
        rows, total = self.lots.page(offset, limit, keyword, state)
        return [self.lot_out(row) for row in rows], total

    def list_reservations(self, batch_id: str | None = None) -> list[dict]:
        return self.inventory.list_reservations(batch_id)

    def tank_out(self, tank: WasteTank) -> dict:
        return {
            "id": tank.id, "kind": tank.kind, "level_pct": tank.level_pct,
            "capacity_l": tank.capacity_l,
            "over_threshold": tank.level_pct >= WASTE_WARN_PCT,
            "delete_blockers": waste_delete_blockers(tank.level_pct),
        }

    def list_waste(self) -> list[dict]:
        return [self.tank_out(tank) for tank in self.waste.list()]

    # ---------- 批号写 ----------

    def receive_lot(self, payload: dict, user: User) -> dict:
        if self.lots.get(payload["id"]):
            raise StateConflict(f"批号 {payload['id']} 已存在")
        material_id = payload.get("material_id") or ""
        material = self.materials.get(material_id) if material_id else None
        if material_id and not material:
            raise NotFound("物料主数据不存在")
        if material is None:
            # 没指定主数据时按（名称，单位）找或建，保持旧入口可用
            material = self.materials.by_name_unit(payload["material"], payload["unit"])
            if material is None:
                material = Material(
                    org_id=self.ctx.org_id, code=f"{payload['material']}@{payload['unit']}",
                    name=payload["material"], base_unit=payload["unit"],
                    category=payload.get("type", ""), cas=payload.get("cas", ""),
                )
                self.materials.add(material)
        if payload["unit"] != material.base_unit and payload["unit"] not in (
            material.conversions or {}
        ):
            raise ValidationFailed(
                f"单位 {payload['unit']} 与物料基础单位 {material.base_unit} 不一致，"
                f"且未登记精确换算",
                code="unit_mismatch",
            )
        try:
            quantity = inventory.positive(payload["qty"], "入库数量")
        except inventory.QuantityError as exc:
            raise ValidationFailed(str(exc)) from exc
        lot = Lot(
            id=payload["id"], org_id=self.ctx.org_id, material_id=material.id,
            material=payload["material"], cas=payload.get("cas", ""), type=payload.get("type", ""),
            qty=0, opening_balance=0, unit=payload["unit"], release="待复验",
            sds=payload.get("sds", ""), compat=payload.get("compat", ""),
            expiry=payload["expiry"], opened="", open_expiry=payload.get("open_expiry", ""),
            open_expiry_basis=payload.get("open_expiry_basis", ""),
            storage=payload.get("storage", ""), ghs=payload.get("ghs") or [],
        )
        self.lots.add(lot)
        # 入库走库存事件，账面库存由流水推出来，不直接写 qty
        self.inventory.post(
            "manual", payload.get("event_id") or f"receive-{lot.id}", "receive",
            [{"line_no": 1, "lot_id": lot.id, "quantity": quantity, "unit": payload["unit"]}],
            user=user, reason="入库登记", commit=False,
        )
        self.audit.record(
            user, "入库登记", lot.id, before="—", after="待复验",
            detail=f"{lot.material} {quantity:f}{lot.unit}，有效期 {lot.expiry}",
        )
        self.db.commit()
        return self.lot_out(lot)

    def release_lot(self, lot_id: str, signature_id: str, user: User) -> dict:
        lot = self.lots.require(lot_id, "批号不存在")
        if lot.release == "已放行":
            raise StateConflict("批号已放行")
        signature = self.identity.consume_signature(signature_id, user, "批号放行", lot.id)
        lot.release = "已放行"
        self.audit.record(
            user, "批号放行", lot_id, sign=True, meaning=signature.meaning,
            before="待复验", after="已放行", signature_id=signature.id,
        )
        self.db.commit()
        return self.lot_out(lot)

    def record_opening(self, lot_id: str, payload: dict, user: User) -> dict:
        """登记开封。开封截止时间与依据都要录，不按类别编造天数。"""
        lot = self.lots.require(lot_id, "批号不存在")
        if lot.state == "scrapped":
            raise StateConflict("已报废批号不能登记开封")
        opened = payload.get("opened") or today_iso()
        open_expiry = payload.get("open_expiry") or ""
        basis = (payload.get("open_expiry_basis") or "").strip()
        if open_expiry and not basis:
            raise ValidationFailed(
                "录入开封截止时间必须写明依据（SOP、供应商说明或实验室规定）",
                code="open_expiry_basis_required",
            )
        if open_expiry and open_expiry < opened:
            raise ValidationFailed("开封截止时间不能早于开封日期")
        before = f"{lot.opened or '未开封'}/{lot.open_expiry or '—'}"
        lot.opened = opened
        lot.open_expiry = open_expiry
        lot.open_expiry_basis = basis
        deadline, source = inventory.effective_expiry(lot.expiry, lot.open_expiry)
        self.audit.record(
            user, "登记开封", lot.id, before=before, after=f"{opened}/{open_expiry or '—'}",
            detail=(
                f"依据：{basis or '未录入开封期限'}；"
                f"有效截止取较早者 {deadline or '—'}（{source}）"
            ),
        )
        self.db.commit()
        return self.lot_out(lot)

    def update_lot(self, lot_id: str, changes: dict, user: User) -> dict:
        lot = self.lots.require(lot_id, "批号不存在")
        allowed = lot_editable_fields(lot.release, lot.state) | {"open_expiry", "open_expiry_basis"}
        if "qty" in changes:
            raise StateConflict(
                "数量不能直接改",
                {"blocked": [{"key": "qty", "label": "数量变化必须通过库存事件或盘点调整入账"}]},
                code="quantity_not_editable",
            )
        rejected = reject_uneditable(changes, allowed)
        if rejected:
            raise StateConflict(
                "这些字段当前不可修改",
                {"blocked": [
                    {"key": key,
                     "label": f"{key}：批号{'已报废' if lot.state == 'scrapped' else '已放行'}，"
                              f"该字段进过质量判断不能再改；数量偏差请用盘点调整"}
                    for key in rejected
                ]},
            )
        before = {key: getattr(lot, key) for key in changes}
        for key, value in changes.items():
            setattr(lot, key, value)
        self.audit.record(
            user, "编辑批号", lot_id,
            detail="；".join(f"{k}: {before[k]} → {v}" for k, v in changes.items()),
        )
        self.db.commit()
        return self.lot_out(lot)

    def delete_lot(self, lot_id: str, user: User) -> dict:
        lot = self.lots.require(lot_id, "批号不存在")
        blockers = lot_delete_blockers(self.reservations.count_for_lot(lot_id), lot.state)
        # 只有进过生产使用的流水才阻止删除：入库与盘点是这条批号自己的登记痕迹，
        # 录错重录时它们不该把人锁在一个错的批号上。
        used = [
            line for line in self.inventory.events.ledger_for_lot(lot_id)
            if line.reservation_id or line.batch_id
        ]
        if used:
            blockers.append(f"已有 {len(used)} 条投料 / 消耗流水，只能报废不能删除")
        if blockers:
            raise StateConflict("批号不可删除", {"blocked": [{"key": "lot", "label": b} for b in blockers]})
        self.audit.record(
            user, "删除批号", lot_id, before=lot.release, after="已删除",
            detail=f"{lot.material} {dec(lot.qty):f}{lot.unit}；从未被预留、无流水",
        )
        # 允许删除的批号只可能有登记/盘点流水。先删其明细，再清掉已经没有任何明细的
        # 事件头；生产使用流水在上面的 blockers 已拒绝。否则 PG 外键会阻止删 lot。
        event_ids = {
            row[0] for row in self.db.query(InventoryLedger.event_row_id)
            .filter(InventoryLedger.lot_id == lot_id).all()
        }
        self.db.query(InventoryLedger).filter(InventoryLedger.lot_id == lot_id).delete(
            synchronize_session=False
        )
        for event_id in event_ids:
            remaining = self.db.query(InventoryLedger).filter(
                InventoryLedger.event_row_id == event_id
            ).count()
            if not remaining:
                self.db.query(InventoryEvent).filter(InventoryEvent.id == event_id).delete(
                    synchronize_session=False
                )
        self.db.delete(lot)
        self.db.commit()
        return {"id": lot_id, "deleted": True}

    def scrap_lot(self, lot_id: str, reason: str, signature_id: str, user: User) -> dict:
        lot = self.lots.require(lot_id, "批号不存在")
        if lot.state == "scrapped":
            raise StateConflict("批号已报废")
        if not reason.strip():
            raise DomainError("报废必须填写理由")
        open_reservations = [
            row for row in self.reservations.for_lot(lot_id) if row.state == "reserved"
        ]
        if open_reservations:
            raise StateConflict(
                "批号不可报废",
                {"blocked": [{"key": "reservation",
                              "label": f"还有 {len(open_reservations)} 条未消耗预留占用它："
                                       f"{'、'.join(r.batch_id for r in open_reservations[:5])}"}]},
            )
        signature = self.identity.consume_signature(signature_id, user, "批号报废", lot.id)
        before_qty = dec(lot.qty)
        if before_qty > inventory.ZERO:
            # 报废清零也走流水：库存不能凭一个状态字段无声消失
            self.inventory.post(
                "manual", f"scrap-{lot.id}", "loss",
                [{"line_no": 1, "lot_id": lot.id, "quantity": before_qty, "unit": lot.unit,
                  "note": f"报废：{reason}"}],
                user=user, reason=f"批号报废：{reason}", commit=False,
            )
        lot.state = "scrapped"
        lot.scrap_reason = reason
        self.audit.record(
            user, "批号报废", lot_id, sign=True, meaning=signature.meaning, signature_id=signature.id,
            before=f"{before_qty:f}{lot.unit} 在库", after="已报废",
            detail=f"{reason}；不可再预留，历史投料记录保留",
        )
        self.db.commit()
        return self.lot_out(lot)

    def adjust_lot(self, lot_id: str, qty, reason: str, user: User) -> dict:
        lot = self.lots.require(lot_id, "批号不存在")
        if lot.state == "scrapped":
            raise StateConflict("已报废批号不能盘点调整")
        if not reason.strip():
            raise DomainError("盘点调整必须填写理由")
        try:
            target = inventory.q(qty)
        except inventory.QuantityError as exc:
            raise ValidationFailed(str(exc)) from exc
        if target < inventory.ZERO:
            raise DomainError("盘点数量不能为负")
        outstanding = self.inventory.outstanding_for_lot(lot_id)
        if target < outstanding:
            raise StateConflict(
                "盘点数量低于未耗用占用",
                {"blocked": [{"key": "reserved",
                              "label": f"未耗用占用 {outstanding:f}{lot.unit}，盘点值不能低于它；"
                                       f"先终止相关批次释放预留"}]},
            )
        before = dec(lot.qty)
        self.inventory.post(
            "manual", f"adjust-{lot.id}-{today_iso()}-{before:f}-{target:f}", "adjust",
            [{"line_no": 1, "lot_id": lot.id, "quantity": target, "unit": lot.unit, "note": reason}],
            user=user, reason=f"盘点调整：{reason}", commit=False,
        )
        self.audit.record(
            user, "批号盘点调整", lot_id, before=f"{before:f}{lot.unit}", after=f"{target:f}{lot.unit}",
            detail=f"差额 {target - before:+f}{lot.unit}；{reason}",
        )
        self.db.commit()
        return self.lot_out(lot)

    # ---------- 废液桶 ----------

    def create_tank(self, payload: dict, user: User) -> dict:
        if self.waste.get(payload["id"]):
            raise DomainError(f"废液桶 {payload['id']} 已登记")
        tank = WasteTank(
            id=payload["id"], org_id=self.ctx.org_id, kind=payload["kind"],
            level_pct=payload.get("level_pct", 0), capacity_l=payload.get("capacity_l", 20),
        )
        self.waste.add(tank)
        self.audit.record(
            user, "登记废液桶", tank.id, before="—", after=f"{tank.kind} {tank.capacity_l:g}L",
            detail=f"初始液位 {tank.level_pct:g}%",
        )
        self.db.commit()
        return self.tank_out(tank)

    def update_tank(self, tank_id: str, changes: dict, user: User) -> dict:
        tank = self.waste.get(tank_id)
        if not tank:
            raise NotFound("废液桶不存在")
        rejected = reject_uneditable(changes, {"kind", "capacity_l", "level_pct"})
        if rejected:
            raise DomainError(f"不支持修改：{'、'.join(rejected)}")
        before = {key: getattr(tank, key) for key in changes}
        for key, value in changes.items():
            setattr(tank, key, value)
        self.audit.record(
            user, "编辑废液桶", tank_id,
            detail="；".join(f"{k}: {before[k]} → {v}" for k, v in changes.items()),
        )
        self.db.commit()
        return self.tank_out(tank)

    def delete_tank(self, tank_id: str, user: User) -> dict:
        tank = self.waste.get(tank_id)
        if not tank:
            raise NotFound("废液桶不存在")
        blockers = waste_delete_blockers(tank.level_pct)
        if blockers:
            raise StateConflict("废液桶不可移除", {"blocked": [{"key": "waste", "label": b} for b in blockers]})
        self.audit.record(
            user, "移除废液桶登记", tank_id, before=f"{tank.kind} {tank.capacity_l:g}L", after="已移除",
            detail="液位为 0，现场无实物",
        )
        self.db.delete(tank)
        self.db.commit()
        return {"id": tank_id, "deleted": True}

    def swap_tank(self, tank_id: str, user: User) -> dict:
        tank = self.waste.require(tank_id, "废液桶不存在")
        before = tank.level_pct
        tank.level_pct = 0
        for alarm in self.alarms.for_source("material", tank_id):
            alarm.condition_active = False
        self.audit.record(
            user, "AGV 换桶", tank_id, before=f"{before:.0f}%", after="0%",
            detail=f"{tank.kind}；换桶后设备侧上报条件恢复",
        )
        self.db.commit()
        return {"id": tank.id, "level_pct": tank.level_pct}

    # ---------- 供其他服务调用 ----------

    def reserve_for_batch(self, batch_id: str, bom: list[dict], user: User | None = None):
        return self.inventory.reserve_for_batch(batch_id, bom, user)

    def release_reservations(self, batch_id: str, user: User | None = None) -> dict:
        return self.inventory.release_batch_reservations(batch_id, user)

    def bom_satisfied(self, batch_id: str, bom: list[dict]) -> bool:
        reservations = self.reservations.for_batch(batch_id)
        for item in bom or []:
            total = inventory.ZERO
            for row in reservations:
                if row.state not in {"reserved", "consumed"}:
                    continue
                lot = self.lots.get(row.lot_id)
                if lot is None or lot.material != item["material"]:
                    continue
                total += dec(row.qty)
            if total < inventory.q(item["qty"]):
                return False
        return True

    def expired_reserved_lots(self, batch_id: str) -> list[str]:
        """预留里已过有效期或开封超期的批号。开跑检查直接用它。"""
        blocked: list[str] = []
        for row in self.reservations.for_batch(batch_id):
            lot = self.lots.get(row.lot_id)
            if lot is None:
                continue
            reasons = self.inventory.lot_blockers(lot)
            if reasons:
                blocked.append(reasons[0])
        return blocked

    def expiry_warnings(self) -> list[dict]:
        today = today_iso()
        soon = self._in_days(7)
        rows = []
        for lot in self.lots.list():
            deadline, basis = inventory.effective_expiry(lot.expiry, lot.open_expiry)
            if not deadline or deadline > soon:
                continue
            rows.append(
                {
                    "lot_id": lot.id, "material": lot.material, "expiry": lot.expiry,
                    "open_expiry": lot.open_expiry, "effective_expiry": deadline, "basis": basis,
                    "expired": deadline < today, "release": lot.release,
                }
            )
        return rows

    def seed_waste_tank(self, tank: WasteTank) -> None:
        self.waste.add(tank)
