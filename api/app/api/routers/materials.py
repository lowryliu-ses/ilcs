from fastapi import APIRouter

from ...schemas import (
    InventoryEventIn, LotAdjustIn, LotCreateIn, LotOpenIn, LotPatchIn, LotScrapIn,
    MaterialCreateIn, Signed, WasteCreateIn, WastePatchIn,
)
from ...services.inventory_service import InventoryService
from ...services.material_service import MaterialService
from ..deps import Ctx, CurrentUser, DbSession, IdempotencyGuard, Paging, require

router = APIRouter(tags=["material"])


@router.get("/materials")
def list_materials(db: DbSession, ctx: Ctx):
    return MaterialService(db, ctx).list_materials()


@router.post("/materials", status_code=201)
def create_material(
    payload: MaterialCreateIn, db: DbSession, user: CurrentUser, ctx=require("material.edit")
):
    return MaterialService(db, ctx).create_material(payload.model_dump(), user)


@router.get("/lots")
def list_lots(
    db: DbSession, ctx: Ctx, paging: Paging, keyword: str = "", state: str | None = None,
    paged: bool = False,
):
    service = MaterialService(db, ctx)
    if not paged and not keyword and not state:
        return service.list_lots()
    items, total = service.page_lots(paging.offset, paging.page_size, keyword, state)
    return paging.wrap(items, total)


@router.get("/lots/{lot_id}/ledger")
def lot_ledger(lot_id: str, db: DbSession, ctx: Ctx):
    """批号流水与对账。账面、未耗用占用、已领用未消耗、可用量分列。"""
    return InventoryService(db, ctx).ledger_for_lot(lot_id)


@router.get("/reservations")
def list_reservations(db: DbSession, ctx: Ctx, batch_id: str | None = None):
    return MaterialService(db, ctx).list_reservations(batch_id)


@router.get("/waste")
def list_waste(db: DbSession, ctx: Ctx):
    return MaterialService(db, ctx).list_waste()


@router.post("/lots", status_code=201)
def receive_lot(payload: LotCreateIn, db: DbSession, user: CurrentUser, ctx=require("material.edit")):
    return MaterialService(db, ctx).receive_lot(payload.model_dump(), user)


@router.post("/lots/{lot_id}/release")
def release_lot(
    lot_id: str, payload: Signed, db: DbSession, user: CurrentUser, ctx=require("material.release")
):
    return MaterialService(db, ctx).release_lot(lot_id, payload.signature_id, user)


@router.post("/lots/{lot_id}/opening")
def record_opening(
    lot_id: str, payload: LotOpenIn, db: DbSession, user: CurrentUser, ctx=require("material.edit")
):
    """登记开封。有效截止时间取生产有效期与开封有效期中的较早者。"""
    return MaterialService(db, ctx).record_opening(lot_id, payload.model_dump(), user)


@router.patch("/lots/{lot_id}")
def update_lot(
    lot_id: str, payload: LotPatchIn, db: DbSession, user: CurrentUser, ctx=require("material.edit")
):
    """改批号信息。数量不能在这里改——变化必须通过库存事件或盘点调整入账。"""
    return MaterialService(db, ctx).update_lot(
        lot_id, payload.model_dump(exclude_unset=True, exclude_none=True), user
    )


@router.delete("/lots/{lot_id}")
def delete_lot(lot_id: str, db: DbSession, user: CurrentUser, ctx=require("material.edit")):
    return MaterialService(db, ctx).delete_lot(lot_id, user)


@router.post("/lots/{lot_id}/scrap")
def scrap_lot(
    lot_id: str, payload: LotScrapIn, db: DbSession, user: CurrentUser,
    ctx=require("material.release"),
):
    return MaterialService(db, ctx).scrap_lot(lot_id, payload.reason, payload.signature_id, user)


@router.post("/lots/{lot_id}/adjust")
def adjust_lot(
    lot_id: str, payload: LotAdjustIn, db: DbSession, user: CurrentUser,
    ctx=require("material.edit"),
):
    """盘点调整。差额与理由写审计与流水；不能调到低于未耗用占用。"""
    return MaterialService(db, ctx).adjust_lot(lot_id, payload.qty, payload.reason, user)


@router.post("/inventory/events", status_code=201)
def post_inventory_event(
    payload: InventoryEventIn, db: DbSession, guard: IdempotencyGuard, user: CurrentUser,
    ctx=require("inventory.post"),
):
    """库存事件入账。

    业务级去重由 (组织, 来源, event_id) 唯一约束负责：同一事件重试只入账一次，
    同一命令下的不同部分投料用不同 event_id，各自入账。
    """
    body = payload.model_dump()
    guard.bind(ctx, body)
    replay = guard.replay()
    if replay is not None:
        return replay
    result = InventoryService(db, ctx).post(
        payload.source, payload.event_id, payload.event_type,
        [item.model_dump() for item in payload.items], user=user, batch_id=payload.batch_id,
        step_run_id=payload.step_run_id, command_id=payload.command_id, reason=payload.reason,
    )
    return guard.remember(result)


@router.get("/inventory/events")
def list_inventory_events(db: DbSession, ctx: Ctx, batch_id: str = ""):
    service = InventoryService(db, ctx)
    if batch_id:
        return service.ledger_for_batch(batch_id)
    return [
        {
            "id": row.id, "source": row.source, "event_id": row.event_id,
            "event_type": row.event_type, "batch_id": row.batch_id, "reason": row.reason,
            "created_at": row.created_at.isoformat(timespec="seconds"),
            "lines": [service.line_out(line) for line in service.events.lines_for_event(row.id)],
        }
        for row in service.events.query().order_by(
            service.events.model.created_at.desc()
        ).limit(100).all()
    ]


@router.post("/waste", status_code=201)
def create_tank(payload: WasteCreateIn, db: DbSession, user: CurrentUser, ctx=require("material.edit")):
    return MaterialService(db, ctx).create_tank(payload.model_dump(), user)


@router.patch("/waste/{tank_id}")
def update_tank(
    tank_id: str, payload: WastePatchIn, db: DbSession, user: CurrentUser,
    ctx=require("material.edit"),
):
    return MaterialService(db, ctx).update_tank(
        tank_id, payload.model_dump(exclude_unset=True, exclude_none=True), user
    )


@router.delete("/waste/{tank_id}")
def delete_tank(tank_id: str, db: DbSession, user: CurrentUser, ctx=require("material.edit")):
    return MaterialService(db, ctx).delete_tank(tank_id, user)


@router.post("/waste/{tank_id}/swap")
def swap_tank(tank_id: str, db: DbSession, user: CurrentUser, ctx=require("material.edit")):
    return MaterialService(db, ctx).swap_tank(tank_id, user)
