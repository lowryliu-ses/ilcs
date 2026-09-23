from fastapi import APIRouter

from ...schemas import MaintenanceCancelIn, MaintenanceCompleteIn, MaintenanceOrderIn
from ...services.maintenance_service import MaintenanceService
from ..deps import Ctx, CurrentUser, DbSession, IdempotencyGuard, require

router = APIRouter(tags=["resource"])


@router.get("/maintenance-orders")
def list_orders(db: DbSession, ctx: Ctx, asset_id: str = ""):
    """某台资产的全部工单；不指定资产时列出未结束的工单。"""
    return MaintenanceService(db, ctx).list(asset_id)


@router.post("/maintenance-orders", status_code=201)
def create_order(
    payload: MaintenanceOrderIn, db: DbSession, guard: IdempotencyGuard, user: CurrentUser,
    ctx=require("maintenance.edit"),
):
    """建单同时登记维护占用；工单与占用在同一个事务里提交。"""
    body = payload.model_dump(mode="json")
    guard.bind(ctx, body).required()
    replay = guard.replay()
    if replay is not None:
        return replay
    return guard.remember(MaintenanceService(db, ctx).create(payload.model_dump(), user))


@router.post("/maintenance-orders/{order_id}/start")
def start_order(order_id: str, db: DbSession, user: CurrentUser, ctx=require("maintenance.edit")):
    return MaintenanceService(db, ctx).start(order_id, user)


@router.post("/maintenance-orders/{order_id}/complete")
def complete_order(
    order_id: str, payload: MaintenanceCompleteIn, db: DbSession, user: CurrentUser,
    ctx=require("maintenance.edit"),
):
    return MaintenanceService(db, ctx).complete(
        order_id, payload.result, payload.record, payload.signature_id, user,
    )


@router.post("/maintenance-orders/{order_id}/cancel")
def cancel_order(
    order_id: str, payload: MaintenanceCancelIn, db: DbSession, user: CurrentUser,
    ctx=require("maintenance.edit"),
):
    return MaintenanceService(db, ctx).cancel(order_id, payload.reason, user)
