from fastapi import APIRouter

from ...schemas import (
    AssetCreateIn, AssetPatchIn, BookingCancelIn, BookingIn, CalibrationIn, StationLinkIn,
)
from ...services.asset_service import AssetService
from ..deps import Ctx, CurrentUser, DbSession, Paging, require

router = APIRouter(tags=["asset"])


@router.get("/assets")
def list_assets(db: DbSession, ctx: Ctx, paging: Paging, keyword: str = "", state: str | None = None):
    items, total = AssetService(db, ctx).page(paging.offset, paging.page_size, keyword, state)
    return paging.wrap(items, total)


@router.get("/assets/{asset_id}")
def get_asset(asset_id: str, db: DbSession, ctx: Ctx):
    return AssetService(db, ctx).detail(asset_id)


@router.post("/assets", status_code=201)
def create_asset(payload: AssetCreateIn, db: DbSession, user: CurrentUser, ctx=require("asset.edit")):
    return AssetService(db, ctx).create_asset(payload.model_dump(), user)


@router.patch("/assets/{asset_id}")
def update_asset(
    asset_id: str, payload: AssetPatchIn, db: DbSession, user: CurrentUser, ctx=require("asset.edit")
):
    changes = payload.model_dump(exclude_unset=True, exclude_none=True, exclude={"row_version"})
    return AssetService(db, ctx).update_asset(asset_id, changes, payload.row_version, user)


@router.post("/assets/{asset_id}/stations")
def link_station(
    asset_id: str, payload: StationLinkIn, db: DbSession, user: CurrentUser,
    ctx=require("asset.edit"),
):
    """把工位挂到资产上。多个工位共享同一资产的容量与校准许可。"""
    return AssetService(db, ctx).link_station(asset_id, payload.station_id, user)


@router.get("/assets/{asset_id}/calibrations")
def list_calibrations(asset_id: str, db: DbSession, ctx: Ctx):
    service = AssetService(db, ctx)
    return [service.calibration_out(row) for row in service.calibrations.for_asset(asset_id)]


@router.post("/assets/{asset_id}/calibrations", status_code=201)
def add_calibration(
    asset_id: str, payload: CalibrationIn, db: DbSession, user: CurrentUser,
    ctx=require("asset.edit"),
):
    return AssetService(db, ctx).add_calibration(asset_id, payload.model_dump(), user)


@router.get("/resource-bookings")
def list_bookings(db: DbSession, ctx: Ctx, asset_id: str = ""):
    return AssetService(db, ctx).list_bookings(asset_id)


@router.post("/resource-bookings", status_code=201)
def create_booking(payload: BookingIn, db: DbSession, user: CurrentUser, ctx=require("booking.edit")):
    """创建占用。冲突在事务里重算；返回受影响的已排程工步清单。"""
    return AssetService(db, ctx).create_booking(payload.model_dump(), user)


@router.post("/resource-bookings/{booking_id}/cancel")
def cancel_booking(
    booking_id: str, payload: BookingCancelIn, db: DbSession, user: CurrentUser,
    ctx=require("booking.edit"),
):
    return AssetService(db, ctx).cancel_booking(booking_id, payload.reason, user)
