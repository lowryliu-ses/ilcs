from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, or_

from ..core.db import dec
from ..models import (
    InventoryEvent, InventoryLedger, Lot, Material, Reservation, WasteTank,
)
from .base import Repository, ScopedRepository

ZERO = Decimal("0.000000")


class MaterialRepository(ScopedRepository[Material]):
    model = Material

    def list(self) -> list[Material]:
        return list(self.query().order_by(Material.name, Material.code).all())

    def by_code(self, code: str) -> Material | None:
        return self.query().filter(Material.code == code).first()

    def by_name_unit(self, name: str, unit: str) -> Material | None:
        return self.query().filter(Material.name == name, Material.base_unit == unit).first()


class LotRepository(ScopedRepository[Lot]):
    model = Lot

    def list(self) -> list[Lot]:
        return list(self.query().order_by(Lot.material, Lot.id).all())

    def page(self, offset: int, limit: int, keyword: str = "", state: str | None = None):
        query = self.query()
        if keyword:
            like = f"%{keyword}%"
            query = query.filter(or_(Lot.id.like(like), Lot.material.like(like)))
        if state:
            query = query.filter(Lot.state == state)
        total = query.count()
        rows = query.order_by(Lot.material, Lot.id).offset(offset).limit(limit).all()
        return list(rows), total

    def released_for(self, material: str, unit: str) -> list[Lot]:
        """可预留的批号：已放行、未报废、单位一致。有效期在服务层按业务规则再判。"""
        return list(
            self.query()
            .filter(
                Lot.material == material,
                Lot.release == "已放行",
                Lot.unit == unit,
                Lot.state == "active",
            )
            .order_by(Lot.expiry)
            .all()
        )

    def open_reservations(self, lot_id: str) -> list[Reservation]:
        return list(
            self.db.query(Reservation)
            .filter(Reservation.lot_id == lot_id, Reservation.state == "reserved")
            .all()
        )

    def outstanding_qty(self, lot_id: str) -> Decimal:
        """未耗用占用 = Σ(授权预留 − 已消耗 − 已核销损耗 − 已释放)。"""
        total = ZERO
        for row in self.open_reservations(lot_id):
            total += outstanding(row)
        return total

    # 旧名保留：调用方语义就是「被占用的量」
    def reserved_qty(self, lot_id: str) -> Decimal:
        return self.outstanding_qty(lot_id)

    def available(self, lot: Lot) -> Decimal:
        """可用量 = 账面库存 − 全部未耗用占用。"""
        return dec(lot.qty) - self.outstanding_qty(lot.id)

    def lock_for_update(self, lot_id: str) -> Lot | None:
        """取行锁：同一批号的预留 / 领用 / 调整串行执行。"""
        query = self.query().filter(Lot.id == lot_id)
        query = query.with_for_update()
        return query.first()


def outstanding(reservation: Reservation) -> Decimal:
    value = (
        dec(reservation.qty)
        - dec(reservation.consumed_qty)
        - dec(reservation.loss_qty)
        - dec(reservation.released_qty)
    )
    return value if value > ZERO else ZERO


def issued_outstanding(reservation: Reservation) -> Decimal:
    """已领用但未消耗、未归还的量。终止时这部分不能直接变回可用库存。"""
    value = (
        dec(reservation.issued_qty)
        - dec(reservation.consumed_qty)
        - dec(reservation.loss_qty)
        - dec(reservation.returned_qty)
    )
    return value if value > ZERO else ZERO


class ReservationRepository(ScopedRepository[Reservation]):
    model = Reservation

    def for_batch(self, batch_id: str) -> list[Reservation]:
        return list(self.query().filter(Reservation.batch_id == batch_id).all())

    def count_for_lot(self, lot_id: str) -> int:
        return self.query().filter(Reservation.lot_id == lot_id).count()

    def for_lot(self, lot_id: str) -> list[Reservation]:
        return list(self.query().filter(Reservation.lot_id == lot_id).all())

    def lock(self, reservation_id: int) -> Reservation | None:
        query = self.query().filter(Reservation.id == reservation_id)
        query = query.with_for_update()
        return query.first()


class InventoryRepository(ScopedRepository[InventoryEvent]):
    model = InventoryEvent

    def find_event(self, source: str, event_id: str) -> InventoryEvent | None:
        return (
            self.query()
            .filter(InventoryEvent.source == source, InventoryEvent.event_id == event_id)
            .first()
        )

    def lines_for_event(self, event_row_id: str) -> list[InventoryLedger]:
        return list(
            self.db.query(InventoryLedger)
            .filter(InventoryLedger.event_row_id == event_row_id)
            .order_by(InventoryLedger.line_no)
            .all()
        )

    def ledger_for_lot(self, lot_id: str, limit: int = 200) -> list[InventoryLedger]:
        return list(
            self.db.query(InventoryLedger)
            .filter(
                InventoryLedger.lot_id == lot_id,
                InventoryLedger.org_id == self.org_id,
            )
            .order_by(InventoryLedger.created_at, InventoryLedger.line_no)
            .limit(limit)
            .all()
        )

    def ledger_for_batch(self, batch_id: str) -> list[InventoryLedger]:
        return list(
            self.db.query(InventoryLedger)
            .filter(
                InventoryLedger.batch_id == batch_id,
                InventoryLedger.org_id == self.org_id,
            )
            .order_by(InventoryLedger.created_at, InventoryLedger.line_no)
            .all()
        )

    def ledger_sum(self, lot_id: str) -> Decimal:
        rows = self.db.query(InventoryLedger.balance_delta).filter(
            InventoryLedger.lot_id == lot_id, InventoryLedger.org_id == self.org_id
        ).all()
        total = ZERO
        for (delta,) in rows:
            total += dec(delta)
        return total

    def add_line(self, line: InventoryLedger) -> InventoryLedger:
        if not line.org_id:
            line.org_id = self.org_id
        self.db.add(line)
        self.db.flush()
        return line


class WasteRepository(ScopedRepository[WasteTank]):
    model = WasteTank

    def list(self) -> list[WasteTank]:
        return list(self.query().order_by(WasteTank.id).all())
