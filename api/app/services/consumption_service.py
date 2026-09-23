"""设备回传的实际物料消耗入账。

消耗必须由实际投料事件写入，不按步骤比例推算——但真实投料量本来就在设备回执里。这里把
回执 `delivered.materials` 里的每一项按指令号去重后写成库存消耗事件：
- 按批号或物料名找到本批次的预留；找不到、超出预留或单位换算不了就不入账，报警转人工。
- 与计划量（BOM 按消耗步骤均分）偏差超过阈值照常入账，但报警并标记待复核：
  计划量只是对照基准，账上记的永远是设备称出来的量。
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from ..core.config import settings
from ..core.context import AccessContext
from ..core.errors import DomainError
from ..domain.inventory import QuantityError, convert
from ..domain.steps import consumes_materials, normalize
from ..models import Batch, Command, Lot
from ..repositories.materials import MaterialRepository, ReservationRepository
from .alarm_service import AlarmService
from .inventory_service import InventoryService


class ConsumptionService:
    def __init__(self, db: Session, ctx: AccessContext):
        self.db = db
        self.ctx = ctx
        self.reservations = ReservationRepository(db, ctx)
        self.materials = MaterialRepository(db, ctx)
        self.alarms = AlarmService(db, ctx)

    def book(self, batch: Batch, command: Command, delivered: dict, step_run_id: str = "") -> dict:
        usages = (delivered or {}).get("materials") or []
        if not isinstance(usages, list) or not usages:
            return {"booked": 0, "rejected": 0, "deviations": 0}
        booked = rejected = deviations = 0
        for index, usage in enumerate(usages, start=1):
            key = f"command:{command.id}:material:{index}"
            try:
                reservation, lot, quantity, unit = self._resolve(batch, usage)
            except ValueError as exc:
                self._alarm(batch, key, f"第 {command.step_index + 1} 步设备回报的消耗无法入账：{exc}", 2)
                rejected += 1
                continue
            try:
                InventoryService(self.db, self.ctx).post(
                    source="device", event_id=f"{command.id}#{index}", event_type="consume",
                    items=[{
                        "lot_id": lot.id, "reservation_id": reservation.id,
                        "quantity": str(quantity), "unit": unit,
                        "note": f"设备回执 {command.id}",
                    }],
                    batch_id=batch.id, step_run_id=step_run_id, command_id=command.id,
                    reason=f"第 {command.step_index + 1} 步设备回报实际消耗", commit=False,
                )
            except DomainError as exc:
                blocked = (exc.detail or {}).get("blocked") if isinstance(exc.detail, dict) else None
                reason = "；".join(row.get("label", "") for row in blocked or []) or str(exc)
                self._alarm(
                    batch, key,
                    f"第 {command.step_index + 1} 步设备回报的 {lot.material} {quantity:f}{unit} 消耗被拒：{reason}",
                    2,
                )
                rejected += 1
                continue
            booked += 1
            compared = self._against_plan(batch, lot, quantity, unit)
            if compared is not None:
                actual, planned, base_unit = compared
                deviation = abs(actual - planned) / planned * 100
                if deviation > Decimal(str(settings.consumption_deviation_pct)):
                    self._alarm(
                        batch, f"{key}:deviation",
                        f"第 {command.step_index + 1} 步 {lot.material} 实际消耗 {actual:f}{base_unit}，"
                        f"计划 {planned:f}{base_unit}，偏差 {deviation:.1f}% 超过 "
                        f"{settings.consumption_deviation_pct:g}%；已按实际量入账，待复核",
                        3,
                    )
                    deviations += 1
        return {"booked": booked, "rejected": rejected, "deviations": deviations}

    def _resolve(self, batch: Batch, usage: dict):
        if not isinstance(usage, dict):
            raise ValueError("回执 materials 的每一项必须是对象")
        try:
            quantity = Decimal(str(usage.get("quantity")))
        except (InvalidOperation, TypeError) as exc:
            raise ValueError(f"数量 {usage.get('quantity')!r} 不是数值") from exc
        if quantity <= 0:
            raise ValueError("消耗数量必须大于 0")
        unit = str(usage.get("unit") or "")
        lot_id = str(usage.get("lot_id") or "")
        material_name = str(usage.get("material") or "")
        candidates = []
        for reservation in self.reservations.for_batch(batch.id):
            lot = self.db.get(Lot, reservation.lot_id)
            if lot is None:
                continue
            if (lot_id and lot.id == lot_id) or (not lot_id and material_name and lot.material == material_name):
                candidates.append((reservation, lot))
        if not candidates:
            raise ValueError(f"本批次没有 {lot_id or material_name or '未指明物料'} 的预留")
        # 同一物料多个批号时优先仍有未消耗余量的那条预留
        candidates.sort(key=lambda pair: pair[0].state != "reserved")
        reservation, lot = candidates[0]
        return reservation, lot, quantity, unit or lot.unit

    def _against_plan(self, batch: Batch, lot: Lot, quantity: Decimal, unit: str):
        """(实际量, 计划量, 基础单位)，都换算到物料基础单位。计划量 = BOM 用量按消耗步骤均分。

        计划量只作对照，不入账；BOM 里没有这项物料或单位换算不了就不比。
        """
        snapshot = batch.recipe_snapshot or {}
        entry = next((row for row in snapshot.get("bom") or [] if row.get("material") == lot.material), None)
        if entry is None:
            return None
        consuming = [step for step in normalize(snapshot.get("steps") or []) if consumes_materials(step)]
        share = Decimal(str(entry.get("qty") or 0)) / max(1, len(consuming))
        material = self.materials.get(lot.material_id) if lot.material_id else None
        base_unit = material.base_unit if material else lot.unit
        conversions = (material.conversions if material else {}) or {}
        try:
            actual = convert(quantity, unit, base_unit, conversions)
            planned = convert(share, entry.get("unit") or base_unit, base_unit, conversions)
        except QuantityError:
            return None
        return (actual, planned, base_unit) if planned > 0 else None

    def _alarm(self, batch: Batch, key: str, message: str, severity: int) -> None:
        self.alarms.raise_alarm(
            severity=severity, source_type="batch", source_id=batch.id, message=message[:500],
            response="核对设备回报与现场称量；需要时在物料页人工补录消耗或冲正。",
            owner="物料管理员", origin="system", condition_key=key,
        )
