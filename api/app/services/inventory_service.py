"""库存事件与只追加流水。

每个业务事件有稳定 ID，唯一约束覆盖（组织、来源、事件 ID、明细号）。同一事件重试
只入账一次；同一命令下的不同部分投料用不同事件 ID，各自入账。一个事件里的多条明细
整体成功或整体失败——不存在「扣了两条、第三条失败」的中间态。
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
from ..domain.inventory import (
    EVENT_NAMES, ReservationState, ZERO, balance_delta, check_consume, check_issue,
    check_release, check_return, convert, q,
)
from ..models import InventoryEvent, InventoryLedger, Lot, Reservation, User
from ..repositories.materials import (
    InventoryRepository, LotRepository, MaterialRepository, ReservationRepository,
)
from .audit_service import AuditService

SOURCES = {"device", "manual", "weighing", "return", "migration", "system"}


class InventoryService:
    def __init__(self, db: Session, ctx: AccessContext):
        self.db = db
        self.ctx = ctx
        self.lots = LotRepository(db, ctx)
        self.materials = MaterialRepository(db, ctx)
        self.reservations = ReservationRepository(db, ctx)
        self.events = InventoryRepository(db, ctx)
        self.audit = AuditService(db, ctx)

    # ---------- 状态读取 ----------

    @staticmethod
    def state_of(reservation: Reservation) -> ReservationState:
        return ReservationState(
            authorized=dec(reservation.qty),
            consumed=dec(reservation.consumed_qty),
            loss=dec(reservation.loss_qty),
            released=dec(reservation.released_qty),
            issued=dec(reservation.issued_qty),
            returned=dec(reservation.returned_qty),
        )

    def outstanding_for_lot(self, lot_id: str) -> Decimal:
        total = ZERO
        for row in self.reservations.for_lot(lot_id):
            if row.state != "reserved":
                continue
            total += self.state_of(row).outstanding
        return total

    def balances(self, lot: Lot) -> dict:
        outstanding = self.outstanding_for_lot(lot.id)
        balance = dec(lot.qty)
        issued = ZERO
        for row in self.reservations.for_lot(lot.id):
            if row.state == "reserved":
                issued += self.state_of(row).issued_outstanding
        return {
            "balance": balance,
            "outstanding": outstanding,
            "issued_outstanding": issued,
            "available": inventory.available(balance, outstanding),
        }

    # ---------- 事件入账 ----------

    def post(
        self,
        source: str,
        event_id: str,
        event_type: str,
        items: list[dict],
        user: User | None = None,
        batch_id: str = "",
        step_run_id: str = "",
        command_id: str = "",
        reason: str = "",
        commit: bool = True,
    ) -> dict:
        """入账一个库存事件。

        重复的 (来源, 事件 ID) 直接返回原结果并标 replayed，不重复扣减——
        这一层不看请求头的幂等键，业务级去重由这张表的唯一约束负责。
        """
        if source not in SOURCES:
            raise ValidationFailed(f"库存事件来源只能是 {'、'.join(sorted(SOURCES))}")
        if event_type not in inventory.EVENT_TYPES:
            raise ValidationFailed(f"库存事件类型 {event_type} 不受支持")
        if not (event_id or "").strip():
            raise ValidationFailed("库存事件必须带稳定的 event_id", code="event_id_required")
        if not items:
            raise ValidationFailed("库存事件至少要有一条明细")

        existing = self.events.find_event(source, event_id)
        if existing is not None:
            lines = self.events.lines_for_event(existing.id)
            return {
                "event_id": existing.event_id,
                "id": existing.id,
                "event_type": existing.event_type,
                "replayed": True,
                "lines": [self.line_out(line) for line in lines],
            }

        event = InventoryEvent(
            org_id=self.ctx.org_id, source=source, event_id=event_id, event_type=event_type,
            batch_id=batch_id, step_run_id=step_run_id, command_id=command_id, reason=reason,
            created_by=user.id if user else self.ctx.subject_id,
        )
        self.db.add(event)
        try:
            self.db.flush()
        except IntegrityError:
            # 并发重传：另一个请求刚插进去，回放它的结果
            self.db.rollback()
            existing = self.events.find_event(source, event_id)
            if existing is None:
                raise
            lines = self.events.lines_for_event(existing.id)
            return {
                "event_id": existing.event_id, "id": existing.id,
                "event_type": existing.event_type, "replayed": True,
                "lines": [self.line_out(line) for line in lines],
            }

        written: list[InventoryLedger] = []
        for index, item in enumerate(items, start=1):
            written.append(self._apply_line(event, item.get("line_no") or index, item, user))

        detail = "；".join(
            f"{line.lot_id} {line.quantity:f}{line.unit}（余额 {line.balance_after:f}）"
            for line in written
        )
        self.audit.record(
            user, f"库存{EVENT_NAMES.get(event_type, event_type)}", batch_id or event.id,
            before="—", after=EVENT_NAMES.get(event_type, event_type),
            detail=f"事件 {source}/{event_id}；{detail}；{reason}",
        )
        if commit:
            self.db.commit()
        return {
            "event_id": event.event_id,
            "id": event.id,
            "event_type": event.event_type,
            "replayed": False,
            "lines": [self.line_out(line) for line in written],
        }

    def _apply_line(
        self, event: InventoryEvent, line_no: int, item: dict, user: User | None,
    ) -> InventoryLedger:
        lot = self.lots.lock_for_update(item["lot_id"])
        if lot is None:
            raise NotFound(f"批号 {item['lot_id']} 不存在")
        material = self.materials.get(lot.material_id) if lot.material_id else None
        base_unit = material.base_unit if material else lot.unit
        unit = item.get("unit") or base_unit
        try:
            quantity = inventory.positive(item["quantity"], "明细数量")
            quantity = convert(
                quantity, unit, base_unit, material.conversions if material else {}
            )
        except inventory.QuantityError as exc:
            raise ValidationFailed(str(exc), code="quantity_invalid") from exc

        reservation: Reservation | None = None
        if item.get("reservation_id"):
            reservation = self.reservations.lock(int(item["reservation_id"]))
            if reservation is None:
                raise NotFound(f"预留 {item['reservation_id']} 不存在")
            if reservation.lot_id != lot.id:
                raise StateConflict("预留与批号不匹配")

        balance = dec(lot.qty)
        state = self.state_of(reservation) if reservation else None
        event_type = event.event_type
        problems: list[str] = []

        if event_type == "consume":
            if state is None:
                raise ValidationFailed("消耗入账必须引用预留", code="reservation_required")
            problems = check_consume(state, quantity, balance)
        elif event_type == "loss":
            # 核销预留内的损耗要引用预留；报废清零、盘亏这类直接冲减没有预留可引用，
            # 只要不把账面扣成负数就允许
            if state is not None:
                problems = check_consume(state, quantity, balance)
            elif quantity > balance:
                problems = [f"损耗 {quantity:f} 超出账面库存 {balance:f}"]
        elif event_type == "release":
            if state is None:
                raise ValidationFailed("释放必须引用预留", code="reservation_required")
            problems = check_release(state, quantity)
        elif event_type == "issue":
            if state is None:
                raise ValidationFailed("领用必须引用预留", code="reservation_required")
            problems = check_issue(state, quantity)
        elif event_type == "return":
            if state is None:
                raise ValidationFailed("归还必须引用预留", code="reservation_required")
            problems = check_return(state, quantity)
        elif event_type == "reserve":
            if state is None:
                raise ValidationFailed("预留事件必须引用预留行", code="reservation_required")
            free = inventory.available(balance, self.outstanding_for_lot(lot.id))
            if quantity > free:
                problems = [f"可用量 {free:f}，本次追加预留 {quantity:f} 超出"]

        if problems:
            raise StateConflict(
                "库存校验未通过",
                {"blocked": [{"key": f"line{line_no}", "label": p} for p in problems]},
                code="inventory_rejected",
            )

        # 余额与占用在同一事务里一起动
        delta = balance_delta(event_type, quantity)
        if event_type == "adjust":
            delta = quantity - balance  # 盘点：把账面调到 quantity
            new_balance = quantity
        else:
            new_balance = balance + delta
        if new_balance < ZERO:
            raise StateConflict("账面库存不能为负", code="negative_balance")
        lot.qty = new_balance

        if reservation is not None:
            if event_type == "consume":
                reservation.consumed_qty = dec(reservation.consumed_qty) + quantity
                reservation.delivered_qty = float(dec(reservation.consumed_qty))
            elif event_type == "loss":
                reservation.loss_qty = dec(reservation.loss_qty) + quantity
            elif event_type == "release":
                reservation.released_qty = dec(reservation.released_qty) + quantity
            elif event_type == "issue":
                reservation.issued_qty = dec(reservation.issued_qty) + quantity
            elif event_type == "return":
                reservation.returned_qty = dec(reservation.returned_qty) + quantity
            elif event_type == "reserve":
                reservation.qty = dec(reservation.qty) + quantity
            refreshed = self.state_of(reservation)
            if refreshed.outstanding <= ZERO:
                reservation.state = (
                    "consumed" if refreshed.consumed > ZERO or refreshed.loss > ZERO else "released"
                )
            else:
                reservation.state = "reserved"
            reservation.row_version = int(reservation.row_version or 0) + 1

        line = InventoryLedger(
            org_id=self.ctx.org_id, event_row_id=event.id, source=event.source,
            event_id=event.event_id, line_no=line_no, event_type=event_type, lot_id=lot.id,
            material_id=lot.material_id, reservation_id=reservation.id if reservation else None,
            batch_id=event.batch_id, step_run_id=event.step_run_id, quantity=quantity, unit=base_unit,
            balance_delta=delta, balance_after=new_balance,
            operator=(user.display_name if user else self.ctx.subject_label),
            note=item.get("note", ""),
        )
        return self.events.add_line(line)

    # ---------- 预留 ----------

    def reserve_for_batch(self, batch_id: str, bom: list[dict], user: User | None) -> list[Reservation]:
        """按 BOM 选已放行批号写入预留。可用量不足则抛错，整个创建事务回滚。"""
        created: list[Reservation] = []
        lines: list[dict] = []
        for item in bom or []:
            material_name, unit = item["material"], item["unit"]
            try:
                needed = inventory.positive(item["qty"], f"{material_name} 需求量")
            except inventory.QuantityError as exc:
                raise ValidationFailed(str(exc)) from exc
            remaining = needed
            for lot in self.lots.released_for(material_name, unit):
                locked = self.lots.lock_for_update(lot.id) or lot
                blockers = self.lot_blockers(locked)
                if blockers:
                    continue
                free = inventory.available(dec(locked.qty), self.outstanding_for_lot(locked.id))
                if free <= ZERO:
                    continue
                take = min(free, remaining)
                # 预留行先建成 0，授权量由下面那条 reserve 事件加上去。
                # 建行时就把 qty 写满会和事件重复计一次——占用直接翻倍。
                reservation = Reservation(
                    org_id=self.ctx.org_id, batch_id=batch_id, lot_id=locked.id, qty=0, unit=unit,
                )
                self.reservations.add(reservation)
                created.append(reservation)
                lines.append(
                    {"line_no": len(lines) + 1, "lot_id": locked.id, "quantity": take,
                     "unit": unit, "reservation_id": reservation.id}
                )
                remaining -= take
                if remaining <= ZERO:
                    break
            if remaining > ZERO:
                raise StateConflict(
                    f"{material_name} 可用量不足，缺 {remaining:f}{unit}"
                    f"（可用 = 账面库存 − 未耗用占用，且批号须已放行且在有效期内）",
                    code="material_insufficient",
                )
        if lines:
            # 预留本身不改账面库存，但要进流水，否则占用变化没有可追溯记录
            self.post(
                "system", f"reserve-{batch_id}", "reserve", lines, user=user, batch_id=batch_id,
                reason="按 BOM 建立批次预留", commit=False,
            )
        return created

    def release_batch_reservations(
        self, batch_id: str, user: User | None, reason: str = "批次终止",
    ) -> dict:
        """终止只自动释放未领用未消耗部分；已领用的生成归还或处置待办。"""
        released: list[dict] = []
        pending_return: list[dict] = []
        for reservation in self.reservations.for_batch(batch_id):
            if reservation.state != "reserved":
                continue
            state = self.state_of(reservation)
            if state.unissued_outstanding > ZERO:
                released.append(
                    {
                        "line_no": len(released) + 1, "lot_id": reservation.lot_id,
                        "quantity": state.unissued_outstanding, "unit": reservation.unit,
                        "reservation_id": reservation.id, "note": reason,
                    }
                )
            if state.issued_outstanding > ZERO:
                pending_return.append(
                    {
                        "reservation_id": reservation.id,
                        "lot_id": reservation.lot_id,
                        "quantity": f"{state.issued_outstanding:f}",
                        "unit": reservation.unit,
                        "todo": "已领用未消耗，需归还或处置确认后才释放",
                    }
                )
        if released:
            self.post(
                "system", f"release-{batch_id}-{now():%Y%m%d%H%M%S}", "release", released,
                user=user, batch_id=batch_id, reason=reason, commit=False,
            )
        return {"released": len(released), "pending_return": pending_return}

    # ---------- 有效性 ----------

    def lot_blockers(self, lot: Lot, at: str | None = None) -> list[str]:
        """批号能不能用于预留和投料。"""
        from ..core.clock import today_iso

        today = at or today_iso()
        reasons: list[str] = []
        if lot.state == "scrapped":
            reasons.append(f"{lot.id} 已报废")
        if lot.release != "已放行":
            reasons.append(f"{lot.id} 未放行（{lot.release}）")
        deadline, basis = inventory.effective_expiry(lot.expiry, lot.open_expiry)
        if not deadline:
            reasons.append(f"{lot.id} 未录入有效期，无法判定")
        elif deadline < today:
            reasons.append(f"{lot.id} 已超过有效截止 {deadline}（依据{basis}）")
        return reasons

    # ---------- 输出 ----------

    def line_out(self, line: InventoryLedger) -> dict:
        return {
            "id": line.id,
            "source": line.source,
            "event_id": line.event_id,
            "line_no": line.line_no,
            "event_type": line.event_type,
            "event_label": EVENT_NAMES.get(line.event_type, line.event_type),
            "lot_id": line.lot_id,
            "reservation_id": line.reservation_id,
            "batch_id": line.batch_id,
            "step_run_id": line.step_run_id,
            "quantity": f"{dec(line.quantity):f}",
            "unit": line.unit,
            "balance_delta": f"{dec(line.balance_delta):f}",
            "balance_after": f"{dec(line.balance_after):f}",
            "operator": line.operator,
            "note": line.note,
            "created_at": line.created_at.isoformat(timespec="seconds"),
        }

    def ledger_for_lot(self, lot_id: str) -> dict:
        lot = self.lots.get(lot_id)
        if not lot:
            raise NotFound("批号不存在")
        lines = self.events.ledger_for_lot(lot_id)
        balances = self.balances(lot)
        ledger_sum = self.events.ledger_sum(lot_id)
        return {
            "lot_id": lot.id,
            "material": lot.material,
            "unit": lot.unit,
            "opening_balance": f"{dec(lot.opening_balance):f}",
            "balance": f"{balances['balance']:f}",
            "outstanding": f"{balances['outstanding']:f}",
            "issued_outstanding": f"{balances['issued_outstanding']:f}",
            "available": f"{balances['available']:f}",
            "ledger_sum": f"{ledger_sum:f}",
            # 流水累计应当等于账面库存；不等就是账实差额，直接显示出来
            "reconciled": ledger_sum == balances["balance"],
            "lines": [self.line_out(line) for line in lines],
        }

    def ledger_for_batch(self, batch_id: str) -> list[dict]:
        return [self.line_out(line) for line in self.events.ledger_for_batch(batch_id)]

    def reservation_out(self, reservation: Reservation) -> dict:
        lot = self.lots.get(reservation.lot_id)
        state = self.state_of(reservation)
        return {
            "id": reservation.id,
            "batch_id": reservation.batch_id,
            "lot_id": reservation.lot_id,
            "material": lot.material if lot else "",
            "release": lot.release if lot else "",
            "qty": f"{state.authorized:f}",
            "unit": reservation.unit,
            "state": reservation.state,
            "consumed_qty": f"{state.consumed:f}",
            "loss_qty": f"{state.loss:f}",
            "released_qty": f"{state.released:f}",
            "issued_qty": f"{state.issued:f}",
            "returned_qty": f"{state.returned:f}",
            "outstanding": f"{state.outstanding:f}",
            "issued_outstanding": f"{state.issued_outstanding:f}",
            "row_version": reservation.row_version,
            # 旧字段名保留给现有前端读取
            "delivered_qty": float(state.consumed),
        }

    def list_reservations(self, batch_id: str | None = None) -> list[dict]:
        rows = (
            self.reservations.for_batch(batch_id) if batch_id else self.reservations.list()
        )
        return [self.reservation_out(row) for row in rows]
