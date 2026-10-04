"""设备回传的实际物料消耗入账。

消耗必须由实际投料事件写入，不按步骤比例推算——但真实投料量本来就在设备回执里。这里把
回执 `delivered.materials` 里的每一项按指令号去重后写成库存消耗事件：
- 带批号的按那个批号的预留入账；只写物料名的（内置模拟与多数真实设备都这样报）按本批次这种料仍有余量的
  预留依次分摊（有效期早的先扣），一个事件多条明细——预留跨了两个批号时单条预留装不下整步的量。
- 找不到预留、预留合计装不下或单位换算不了就不入账，报警转人工。一项回报整体入账或整体不入账（保存点），
  不会留下没有明细的空事件，也不会只扣了一部分批号。
- 称量加料总有上下浮动：实际量比剩余预留多出的部分不超过偏差阈值（计划量的 `consumption_deviation_pct`，取不到
  计划量时按回报量算），且同一批号还有没被占用的可用量，就先记一笔系统来源的「追加预留」（事件 `<指令>#<序号>:topup`，
  进流水与审计）再按实际量入账；多出更多、或批号没有余量可补，照旧整项拒绝、报警转人工。
- 与计划量偏差超过阈值照常入账，但报警并标记待复核：计划量只是对照基准，账上记的永远是设备称出来的量。
  方案给出用量的物料（快照 BOM 里 `source: plan`），计划量就是这条指令下发的量（各孔用量参数之和）：
  中途剔除的瓶子、同一种料分几步投都不会误报。流程 BOM 列的物料照旧按「BOM 量 ÷ 声明投这种料的消耗
  步骤数」分摊；没有步骤声明它时退回「÷ 没声明物料的消耗步骤数」（旧流程的均分口径）。
- 内置模拟的回执同样入账（来源仍是 device），流水备注写明「模拟设备回执」以便区分。
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from ..core.config import settings
from ..core.context import AccessContext
from ..core.errors import DomainError
from ..domain import params as units
from ..domain.dosing import QUANTUM, commanded_quantity, dosing_param, param_unit
from ..domain.inventory import QuantityError, convert
from ..domain.steps import consumes_materials, normalize, step_material
from ..models import Batch, Command, Lot
from ..repositories.materials import MaterialRepository, ReservationRepository
from ..repositories.resources import CapabilityRepository
from .alarm_service import AlarmService
from .inventory_service import InventoryService


class Refused(ValueError):
    """预留装不下这项回报：和库存校验拒绝同一口径报「消耗被拒」。

    带上差多少（物料基础单位）、可以往哪条预留上补：差得少时由调用方按偏差阈值决定是否自动追加预留。"""

    def __init__(self, message: str, *, shortfall: Decimal = Decimal(0), reservation=None, lot: Lot | None = None,
                 amount: Decimal = Decimal(0), base_unit: str = ""):
        super().__init__(message)
        self.shortfall, self.reservation, self.lot = shortfall, reservation, lot
        self.amount, self.base_unit = amount, base_unit


class ConsumptionService:
    def __init__(self, db: Session, ctx: AccessContext):
        self.db = db
        self.ctx = ctx
        self.reservations = ReservationRepository(db, ctx)
        self.materials = MaterialRepository(db, ctx)
        self.alarms = AlarmService(db, ctx)

    def book(
        self, batch: Batch, command: Command, delivered: dict, step_run_id: str = "", origin: str = "",
    ) -> dict:
        usages = (delivered or {}).get("materials") or []
        if not isinstance(usages, list) or not usages:
            return {"booked": 0, "rejected": 0, "deviations": 0}
        inventory = InventoryService(self.db, self.ctx)
        booked = rejected = deviations = 0
        for index, usage in enumerate(usages, start=1):
            key = f"command:{command.id}:material:{index}"
            event_id = f"{command.id}#{index}"
            if inventory.events.find_event("device", event_id) is not None:
                # 重复回执：已经入过账。不再按现在的余量重新分摊——预留已被第一次扣掉，重分只会误报「装不下」
                booked += 1
                continue
            overdraw: Refused | None = None
            try:
                lines, lot, actual, base_unit = self._allocate(batch, usage)
            except Refused as exc:
                if not self._tolerated(batch, command, exc):
                    self._alarm(
                        batch, key,
                        f"第 {command.step_index + 1} 步设备回报的 {self._label(usage)} 消耗被拒：{exc}", 2,
                    )
                    rejected += 1
                    continue
                overdraw, lot = exc, exc.lot
            except ValueError as exc:
                self._alarm(batch, key, f"第 {command.step_index + 1} 步设备回报的消耗无法入账：{exc}", 2)
                rejected += 1
                continue
            note = f"{'模拟设备回执' if origin == 'simulation' else '设备回执'} {command.id}"
            try:
                # 保存点：任一条明细被拒，事件行与前面几条明细对批号余额、预留的改动一起撤销（连同自动追加的预留）
                with self.db.begin_nested():
                    if overdraw is not None:
                        inventory.post(
                            source="system", event_id=f"{event_id}:topup", event_type="reserve",
                            items=[{"lot_id": overdraw.lot.id, "reservation_id": overdraw.reservation.id,
                                    "quantity": str(overdraw.shortfall), "unit": overdraw.base_unit, "note": note}],
                            batch_id=batch.id, step_run_id=step_run_id, command_id=command.id,
                            reason=(f"第 {command.step_index + 1} 步设备实际用量比剩余预留多 "
                                    f"{overdraw.shortfall:f}{overdraw.base_unit}（在 "
                                    f"{settings.consumption_deviation_pct:g}% 偏差阈值内），自动追加预留后按实际量入账"),
                            commit=False,
                        )
                        lines, lot, actual, base_unit = self._allocate(batch, usage)
                    inventory.post(
                        source="device", event_id=event_id, event_type="consume",
                        items=[
                            {"lot_id": row_lot.id, "reservation_id": reservation.id,
                             "quantity": str(quantity), "unit": base_unit, "note": note}
                            for reservation, row_lot, quantity in lines
                        ],
                        batch_id=batch.id, step_run_id=step_run_id, command_id=command.id,
                        reason=f"第 {command.step_index + 1} 步设备回报实际消耗", commit=False,
                    )
            except (DomainError, Refused) as exc:
                detail = getattr(exc, "detail", None)
                blocked = detail.get("blocked") if isinstance(detail, dict) else None
                reason = "；".join(row.get("label", "") for row in blocked or []) or str(exc)
                self._alarm(
                    batch, key, f"第 {command.step_index + 1} 步设备回报的 {self._label(usage, lot)} 消耗被拒：{reason}", 2,
                )
                rejected += 1
                continue
            booked += 1
            # 分摊到几条预留也只按这一步的总量比一次，不拿每一块去和整步的计划量比
            planned = self._planned(batch, command, lot, base_unit)
            if planned is not None:
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

    def _tolerated(self, batch: Batch, command: Command, refused: Refused) -> bool:
        """预留差的这点量能不能自动补：差额不超过计划量（取不到就按回报量）的偏差阈值，且有预留可以往上加。
        批号还有没有可用量由追加预留那一笔的库存校验把关。"""
        if refused.reservation is None or refused.lot is None or refused.shortfall <= 0:
            return False
        planned = self._planned(batch, command, refused.lot, refused.base_unit)
        basis = planned if planned is not None and planned > 0 else refused.amount
        return refused.shortfall <= basis * Decimal(str(settings.consumption_deviation_pct)) / 100

    @staticmethod
    def _label(usage, lot: Lot | None = None) -> str:
        usage = usage if isinstance(usage, dict) else {}
        name = lot.material if lot is not None else str(usage.get("material") or usage.get("lot_id") or "")
        return f"{name} {usage.get('quantity')}{usage.get('unit') or ''}"

    def _allocate(self, batch: Batch, usage: dict):
        """一项回报落到哪些预留上：([(预留, 批号, 基础单位数量)], 首个批号, 总量, 基础单位)。

        带批号的照旧整笔记到该批号的预留（优先仍有余量的那条）。只写物料名的按这种料仍有余量的预留
        依次分摊：有效期早的先扣，同一批号按建预留的先后——和 reserve_for_batch 选批号的顺序一致。
        合计余量装不下就整项拒绝（`Refused` 带上差额和可以往上补的预留）：差得多要先以明确动作追加预留，
        不在消耗里默默放大占用；差额在偏差阈值内的由 `book` 记一笔追加预留再入账。
        """
        if not isinstance(usage, dict):
            raise ValueError("回执 materials 的每一项必须是对象")
        try:
            quantity = Decimal(str(usage.get("quantity")))
        except (InvalidOperation, TypeError) as exc:
            raise ValueError(f"数量 {usage.get('quantity')!r} 不是数值") from exc
        if not quantity.is_finite() or quantity <= 0:
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
        # 同一物料多个批号时优先仍有未消耗余量的那条预留；其次有效期早的、先建的
        candidates.sort(key=lambda pair: (
            pair[0].state != "reserved", not pair[1].expiry, pair[1].expiry or "", pair[0].id,
        ))
        first = candidates[0][1]
        base_unit, amount = self._to_base(quantity, unit or first.unit, first)
        if lot_id:
            reservation, lot = candidates[0]
            return [(reservation, lot, amount)], lot, amount, base_unit
        lines: list[tuple] = []
        remaining = amount
        available = Decimal(0)
        for reservation, lot in candidates:
            if reservation.state != "reserved":
                continue
            outstanding = InventoryService.state_of(reservation).outstanding
            if outstanding <= 0:
                continue
            available += outstanding
            if remaining > 0:
                take = min(outstanding, remaining)
                lines.append((reservation, lot, take))
                remaining -= take
        if remaining > 0:
            # 差的量补到正在扣的那条预留（同一批号）上；一条都没扣到时补到排在最前的那条
            reservation, lot = lines[-1][:2] if lines else candidates[0]
            raise Refused(
                f"实际用量 {amount:f}{base_unit} 超出本批次 {material_name} 的剩余预留合计 {available:f}{base_unit}，"
                f"请先追加预留并校验可用量",
                shortfall=remaining, reservation=reservation, lot=lot, amount=amount, base_unit=base_unit,
            )
        return lines, first, amount, base_unit

    def _to_base(self, quantity: Decimal, unit: str, lot: Lot) -> tuple[str, Decimal]:
        """换到物料基础单位：先用物料登记的精确换算；没登记的同量纲公制换算（μL→mL、mg→g）比例是精确的，
        也照换——用量参数常用比 BOM 小一级的单位。跨量纲（体积 ↔ 质量）没登记就拒绝，不猜密度。"""
        material = self.materials.get(lot.material_id) if lot.material_id else None
        base_unit = material.base_unit if material else lot.unit
        conversions = (material.conversions if material else {}) or {}
        try:
            if units.canonical_unit(unit) == units.canonical_unit(base_unit):
                return base_unit, quantity.quantize(QUANTUM)
            try:
                return base_unit, convert(quantity, unit, base_unit, conversions)
            except QuantityError as exc:
                scaled = units.convert(quantity, unit, base_unit)
                if scaled is None:
                    raise ValueError(str(exc)) from exc
                return base_unit, scaled.quantize(QUANTUM)
        except (InvalidOperation, ArithmeticError) as exc:
            # 设备回报的量大到超出十进制精度（如 1e30）：按回报无法入账报警，不让整个设备回执事务失败
            raise ValueError(f"回报的用量 {quantity} 超出可记账的范围") from exc

    def _planned(self, batch: Batch, command: Command, lot: Lot, base_unit: str) -> Decimal | None:
        """这一步这种料的计划量（物料基础单位）。只作对照，不入账；BOM 里没有这项物料或单位换算不了就不比。

        方案给出用量的物料（`source: plan`）：计划量 = 这条指令对用量参数下发的量（`dosing.commanded_quantity`，
        只含下发时仍在用的瓶子、只含这一步），单位是用量参数登记的单位——与执行器报给驱动的同一条规则。
        取不到用量参数时退回下面的分摊口径。

        流程 BOM 列的物料（每批一份，没有逐步的量）：BOM 量 ÷ 声明投这种料（`material`）的消耗步骤数；
        多种料各自一步投时，每一步只和自己那种料比。没有步骤声明这种料（旧流程）时照旧均分到没声明物料的
        消耗步骤上，都声明了别的料才退回全部消耗步骤。
        """
        snapshot = batch.recipe_snapshot or {}
        entry = next((row for row in snapshot.get("bom") or [] if row.get("material") == lot.material), None)
        if entry is None:
            return None
        steps = normalize(snapshot.get("steps") or [])
        planned: tuple[Decimal, str] | None = None
        step = steps[command.step_index] if command.step_index < len(steps) else {}
        # 只有这一步声明投的就是这种料，才拿这条指令的用量参数当计划量；多种料的设备在别的料的步骤上回报它时，
        # 拿别的料的下发量来比只会误报偏差
        if entry.get("source") == "plan" and step_material(step) == lot.material:
            row = CapabilityRepository(self.db).get(command.capability or step.get("cap") or "")
            capability = {"params": row.params or {}, "param_specs": row.param_specs or {}} if row else {}
            param = dosing_param(step, capability, str(entry.get("unit") or ""))
            if param:
                planned = (
                    commanded_quantity(command.params or {}, param),
                    param_unit(capability, param) or str(entry.get("unit") or base_unit),
                )
        if planned is None:
            consuming = [step for step in steps if consumes_materials(step)]
            declared = [step for step in consuming if step_material(step) == lot.material]
            undeclared = [step for step in consuming if not step_material(step)]
            sharing = declared or undeclared or consuming
            planned = (
                Decimal(str(entry.get("qty") or 0)) / max(1, len(sharing)),
                str(entry.get("unit") or base_unit),
            )
        try:
            _, value = self._to_base(planned[0], planned[1], lot)
        except ValueError:
            return None
        return value if value > 0 else None

    def _alarm(self, batch: Batch, key: str, message: str, severity: int) -> None:
        self.alarms.raise_alarm(
            severity=severity, source_type="batch", source_id=batch.id, message=message[:500],
            response="核对设备回报与现场称量；需要时在物料页人工补录消耗或冲正。",
            owner="物料管理员", origin="system", condition_key=key,
        )
