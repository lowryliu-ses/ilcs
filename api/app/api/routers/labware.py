"""载具、位置与现场总览。

位置只由设备回执或扫码确认写入；这里的写接口都是「人确认了现场」的动作，带幂等键。
"""
from fastapi import APIRouter

from ...schemas import LabwareBindIn, LabwareCreateIn, LabwareMoveIn, LocationActiveIn, LocationCreateIn
from ...services.transfer_service import TransferService
from ..deps import Ctx, CurrentUser, DbSession, IdempotencyGuard, require

router = APIRouter(tags=["labware"])


@router.get("/floor")
def floor(db: DbSession, ctx: Ctx):
    """现场总览：工位实时状态与在途指令、放置位与板库上的载具、在途转运、位置未知的载具。"""
    return TransferService(db, ctx).floor()


@router.get("/labware-types")
def labware_types(db: DbSession, ctx: Ctx):
    return TransferService(db, ctx).types()


@router.get("/locations")
def locations(db: DbSession, ctx: Ctx):
    return TransferService(db, ctx).locations()


@router.post("/locations", status_code=201)
def create_location(
    payload: LocationCreateIn, db: DbSession, guard: IdempotencyGuard, user: CurrentUser,
    ctx=require("location.edit"),
):
    body = payload.model_dump()
    guard.bind(ctx, body).required()
    replay = guard.replay()
    if replay is not None:
        return replay
    return guard.remember(TransferService(db, ctx).create_location(body, user))


@router.post("/locations/{location_id}/active")
def set_location_active(
    location_id: str, payload: LocationActiveIn, db: DbSession, user: CurrentUser,
    ctx=require("location.edit"),
):
    return TransferService(db, ctx).set_location_active(location_id, payload.active, user)


@router.get("/labware")
def list_labware(db: DbSession, ctx: Ctx, keyword: str = ""):
    return TransferService(db, ctx).list_labware(keyword)


@router.post("/labware", status_code=201)
def register_labware(
    payload: LabwareCreateIn, db: DbSession, guard: IdempotencyGuard, user: CurrentUser,
    ctx=require("labware.move"),
):
    body = payload.model_dump()
    guard.bind(ctx, body).required()
    replay = guard.replay()
    if replay is not None:
        return replay
    return guard.remember(TransferService(db, ctx).register(body, user))


@router.post("/labware/{labware_id}/move")
def move_labware(
    labware_id: str, payload: LabwareMoveIn, db: DbSession, guard: IdempotencyGuard, user: CurrentUser,
    ctx=require("labware.move"),
):
    body = payload.model_dump()
    guard.bind(ctx, {"labware_id": labware_id, **body}).required()
    replay = guard.replay()
    if replay is not None:
        return replay
    return guard.remember(TransferService(db, ctx).move(labware_id, body, user))


@router.get("/labware/{labware_id}/qr")
def labware_qr(labware_id: str, db: DbSession, ctx: Ctx):
    """标签二维码（SVG 文本）。内容是载具条码。"""
    return TransferService(db, ctx).labware_qr(labware_id)


@router.get("/labware/{labware_id}/samples")
def labware_samples(labware_id: str, db: DbSession, ctx: Ctx):
    """载具上现在的样本（按孔位）；按编号或条码都能查。"""
    return TransferService(db, ctx).labware_samples(labware_id)


@router.get("/labware/{labware_id}/moves")
def labware_moves(labware_id: str, db: DbSession, ctx: Ctx):
    return TransferService(db, ctx).moves(labware_id)


@router.post("/batches/{batch_id}/labware")
def bind_labware(
    batch_id: str, payload: LabwareBindIn, db: DbSession, user: CurrentUser, ctx=require("labware.move"),
):
    return TransferService(db, ctx).bind(batch_id, payload.labware_id, user)


@router.delete("/batches/{batch_id}/labware")
def unbind_labware(batch_id: str, db: DbSession, user: CurrentUser, ctx=require("labware.move")):
    return TransferService(db, ctx).unbind(batch_id, user)
