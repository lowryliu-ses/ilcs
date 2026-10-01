"""物料主数据、批号有效性与废液桶。库存算术在 inventory_service 里。"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from sqlalchemy.orm import Session

from ..core.clock import now, today_iso
from ..core.config import settings
from ..core.context import AccessContext
from ..core.db import dec
from ..core.errors import DomainError, NotFound, StateConflict, ValidationFailed
from ..domain import inventory
from ..domain.lifecycle import (
    lot_delete_blockers, lot_editable_fields, reject_uneditable, waste_delete_blockers,
)
from ..domain.params import canonical_unit, decimal_of
from ..models import InventoryEvent, InventoryLedger, Lot, Material, User, WasteTank
from ..repositories.governance import AlarmRepository
from ..repositories.materials import (
    LotRepository, MaterialRepository, ReservationRepository, WasteRepository,
)
from .audit_service import AuditService
from .identity_service import IdentityService
from .inventory_service import InventoryService

WASTE_WARN_PCT = 75


def _factor_text(factor: Any) -> str:
    """换算系数按十进制文字给出，去掉多余的尾零（1.32，不是 1.320000）。"""
    value = decimal_of(factor)
    return f"{value.normalize():f}" if value is not None else str(factor)


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

    def material_out(self, material: Material, lot_count: int | None = None) -> dict:
        if lot_count is None:
            lot_count = sum(1 for row in self.lots.list() if row.material_id == material.id)
        return {
            "id": material.id,
            "code": material.code,
            "name": material.name,
            "base_unit": material.base_unit,
            "category": material.category,
            "cas": material.cas,
            "conversions": {unit: _factor_text(factor) for unit, factor in (material.conversions or {}).items()},
            "external_ref": material.external_ref,
            "ghs": material.ghs or [],
            "state": material.state,
            "lot_count": lot_count,
            # 已有批号时名称与基础单位锁定：批号、预留与设备回报的消耗按名称与单位对账
            "locked_fields": ["name", "base_unit"] if lot_count else [],
            "row_version": material.row_version,
            "updated_at": material.updated_at.isoformat() if material.updated_at else "",
        }

    def list_materials(self) -> list[dict]:
        counts: dict[str, int] = {}
        for lot in self.lots.list():
            if lot.material_id:
                counts[lot.material_id] = counts.get(lot.material_id, 0) + 1
        return [self.material_out(row, counts.get(row.id, 0)) for row in self.materials.list()]

    def create_material(self, payload: dict, user: User) -> dict:
        code = (payload.get("code") or "").strip()
        if not code:
            raise ValidationFailed("物料编码必填")
        if self.materials.by_code(code):
            raise StateConflict(f"物料编码 {code} 已存在")
        name = (payload.get("name") or "").strip()
        base_unit = canonical_unit(payload.get("base_unit"))
        if not name or not base_unit:
            raise ValidationFailed("物料名称与基础单位必填", code="material_invalid")
        self._require_unique_name(name, base_unit)
        material = Material(
            code=code, name=name, base_unit=base_unit,
            category=(payload.get("category") or "").strip(), cas=(payload.get("cas") or "").strip(),
            conversions=self._conversions(payload.get("conversions"), base_unit),
            external_ref=payload.get("external_ref", ""), ghs=self._ghs(payload.get("ghs")),
        )
        self.materials.add(material)
        self.audit.record(
            user, "登记物料", material.id, before="—", after=material.name,
            detail=f"{code}；基础单位 {material.base_unit}",
        )
        self.db.commit()
        return self.material_out(material, 0)

    def update_material(self, material_id: str, changes: dict, expected: int | None, user: User) -> dict:
        """改物料主数据。没传的字段不动；已有批号的物料不能改名称与基础单位。

        类别决定配液模板怎么加这种料、单位换算决定入库与设备回报怎么折成基础单位——改了只影响之后的导入与入账，
        已生成的流程、已入账的流水不回溯。"""
        material = self._require_material(material_id)
        self.materials.check_version(material, expected, "物料主数据")
        values = {key: value for key, value in changes.items() if value is not None}
        lot_count = sum(1 for row in self.lots.list() if row.material_id == material.id)
        if "name" in values:
            values["name"] = str(values["name"]).strip()
            if not values["name"]:
                raise ValidationFailed("物料名称不能为空", code="material_invalid")
        if "base_unit" in values:
            values["base_unit"] = canonical_unit(values["base_unit"])
            if not values["base_unit"]:
                raise ValidationFailed("基础单位不能为空", code="material_invalid")
        renamed = [key for key in ("name", "base_unit") if key in values and values[key] != getattr(material, key)]
        if renamed and lot_count:
            labels = "、".join({"name": "名称", "base_unit": "基础单位"}[key] for key in renamed)
            raise StateConflict(
                f"物料 {material.code} 已有 {lot_count} 个批号，不能改{labels}：批号、预留与设备回报的消耗按名称与单位对账；"
                f"要换请登记一条新物料",
                code="material_locked",
            )
        name = values.get("name", material.name)
        base_unit = values.get("base_unit", material.base_unit)
        if renamed:
            self._require_unique_name(name, base_unit, exclude=material.id)
        if "conversions" in values or "base_unit" in values:
            values["conversions"] = self._conversions(values.get("conversions", material.conversions), base_unit)
        for key in ("category", "cas", "external_ref"):
            if key in values:
                values[key] = str(values[key]).strip()
        if "ghs" in values:
            values["ghs"] = self._ghs(values["ghs"])
        changed = {key: value for key, value in values.items() if getattr(material, key) != value}
        before = {key: getattr(material, key) for key in changed}
        for key, value in changed.items():
            setattr(material, key, value)
        if changed:
            material.updated_at = now()
            self.materials.bump(material)
            self.audit.record(
                user, "修改物料主数据", material.id, before=self._describe(before), after=self._describe(changed),
                detail=material.code, object_version=material.row_version,
            )
        self.db.commit()
        return self.material_out(material, lot_count)

    def retire_material(self, material_id: str, expected: int | None, user: User) -> dict:
        """停用：不能再按它入库新批号，配液模板导入不再认它；已有批号照常可用、可消耗。不删除——流水指回它。"""
        material = self._require_material(material_id)
        self.materials.check_version(material, expected, "物料主数据")
        if material.state != "active":
            raise StateConflict(f"物料 {material.code} 已停用", code="material_retired")
        material.state = "retired"
        material.updated_at = now()
        self.materials.bump(material)
        self.audit.record(user, "停用物料", material.id, before="在用", after="已停用",
                          detail=material.code, object_version=material.row_version)
        self.db.commit()
        return self.material_out(material)

    def restore_material(self, material_id: str, expected: int | None, user: User) -> dict:
        material = self._require_material(material_id)
        self.materials.check_version(material, expected, "物料主数据")
        if material.state == "active":
            raise StateConflict(f"物料 {material.code} 在用，不需要恢复", code="material_active")
        self._require_unique_name(material.name, material.base_unit, exclude=material.id)
        material.state = "active"
        material.updated_at = now()
        self.materials.bump(material)
        self.audit.record(user, "恢复物料", material.id, before="已停用", after="在用",
                          detail=material.code, object_version=material.row_version)
        self.db.commit()
        return self.material_out(material)

    def _require_material(self, material_id: str) -> Material:
        material = self.materials.get(material_id)
        if material is None:
            raise NotFound("物料主数据不存在")
        return material

    def _require_unique_name(self, name: str, base_unit: str, exclude: str = "") -> None:
        """同名同单位只能有一条在用：入库按（名称，单位）找主数据，两条就分不清入到哪条。"""
        for row in self.materials.list():
            if row.id != exclude and row.state == "active" and row.name == name and row.base_unit == base_unit:
                raise StateConflict(
                    f"已有在用的物料 {row.code} 叫 {name}、基础单位 {base_unit}：入库按名称与单位找主数据，不能重复",
                    code="material_duplicate",
                )

    @staticmethod
    def _conversions(raw: Any, base_unit: str) -> dict[str, str]:
        """单位换算 {单位: 1 单位折合多少基础单位}。系数必须是正的十进制数；不能写基础单位自己。"""
        if raw in (None, ""):
            return {}
        if not isinstance(raw, dict):
            raise ValidationFailed("单位换算要写成 {单位: 系数}", code="material_invalid")
        out: dict[str, str] = {}
        problems: list[str] = []
        for unit, factor in raw.items():
            key = canonical_unit(unit)
            if not key:
                problems.append("换算的单位不能为空")
                continue
            if key == base_unit:
                problems.append(f"{key} 就是基础单位，不用登记换算")
                continue
            if key in out:
                problems.append(f"单位 {key} 重复")
                continue
            value = decimal_of(factor)
            if value is None or value <= 0:
                problems.append(f"1 {key} 折合的 {base_unit} 数必须是正数（现在是 {factor!r}）")
                continue
            out[key] = f"{value.normalize():f}"
        if problems:
            raise ValidationFailed("单位换算有问题：" + "；".join(problems), code="material_invalid")
        return out

    @staticmethod
    def _ghs(raw: Any) -> list[str]:
        return [str(item).strip() for item in (raw or []) if str(item).strip()]

    @staticmethod
    def _describe(values: dict) -> str:
        labels = {"name": "名称", "base_unit": "基础单位", "category": "类别", "cas": "CAS", "conversions": "单位换算",
                  "external_ref": "外部编号", "ghs": "GHS"}
        parts = []
        for key, value in values.items():
            if isinstance(value, dict):
                value = "、".join(f"{unit}={factor}" for unit, factor in value.items()) or "无"
            elif isinstance(value, list):
                value = "、".join(value) or "无"
            parts.append(f"{labels.get(key, key)} {value or '空'}")
        return "；".join(parts) or "—"

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
            # 没指定主数据时按（名称，单位）找或建，保持旧入口可用；同名同单位有停用的也有在用的，入到在用的那条
            material = self.materials.by_name_unit(payload["material"], payload["unit"])
            if material is None:
                material = Material(
                    org_id=self.ctx.org_id, code=f"{payload['material']}@{payload['unit']}",
                    name=payload["material"], base_unit=payload["unit"],
                    category=payload.get("type", ""), cas=payload.get("cas", ""),
                )
                self.materials.add(material)
        if material.state != "active":
            raise StateConflict(
                f"物料 {material.code}（{material.name}）已停用，不能入库新批号；要用请先在物料主数据里恢复",
                code="material_retired",
            )
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
