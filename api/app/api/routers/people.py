from fastapi import APIRouter

from ...schemas import PersonBookingIn, PersonCreateIn, PersonPatchIn, QualificationIn, RevokeIn
from ...services.people_service import PeopleService
from ..deps import Ctx, CurrentUser, DbSession, Paging, require

router = APIRouter(prefix="/people", tags=["people"])


@router.get("")
def list_people(
    db: DbSession, ctx: Ctx, paging: Paging, keyword: str = "", state: str | None = None
):
    items, total = PeopleService(db, ctx).page(paging.offset, paging.page_size, keyword, state)
    return paging.wrap(items, total)


@router.get("/expiring-qualifications")
def expiring(db: DbSession, ctx: Ctx):
    """到期与即将到期的资质。工作台与任务分配提示都读它。"""
    return PeopleService(db, ctx).expiring()


@router.get("/{person_id}")
def get_person(person_id: str, db: DbSession, ctx: Ctx):
    return PeopleService(db, ctx).detail(person_id)


@router.post("", status_code=201)
def create_person(payload: PersonCreateIn, db: DbSession, user: CurrentUser, ctx=require("person.edit")):
    return PeopleService(db, ctx).create(payload.model_dump(), user)


@router.patch("/{person_id}")
def update_person(
    person_id: str, payload: PersonPatchIn, db: DbSession, user: CurrentUser,
    ctx=require("person.edit"),
):
    changes = payload.model_dump(exclude_unset=True, exclude_none=True, exclude={"row_version"})
    return PeopleService(db, ctx).update(person_id, changes, payload.row_version, user)


@router.get("/{person_id}/qualifications")
def list_qualifications(person_id: str, db: DbSession, ctx: Ctx):
    service = PeopleService(db, ctx)
    return [service.qualification_out(row) for row in service.qualifications.for_person(person_id)]


@router.post("/{person_id}/qualifications", status_code=201)
def grant_qualification(
    person_id: str, payload: QualificationIn, db: DbSession, user: CurrentUser,
    ctx=require("qualification.edit"),
):
    return PeopleService(db, ctx).grant(person_id, payload.model_dump(), user)


@router.post("/qualifications/{qualification_id}/revoke")
def revoke_qualification(
    qualification_id: str, payload: RevokeIn, db: DbSession, user: CurrentUser,
    ctx=require("qualification.edit"),
):
    """撤销资质。运行中的设备不自动急停，返回里说明既定策略。"""
    return PeopleService(db, ctx).revoke(qualification_id, payload.reason, user)


@router.get("/{person_id}/bookings")
def person_bookings(person_id: str, db: DbSession, ctx: Ctx, include_past: bool = False):
    """人员预占：批次人工步骤（排程生成）、请假、培训、值守。"""
    from ...services.staffing_service import StaffingService

    return StaffingService(db, ctx).list_for(person_id, include_past)


@router.post("/{person_id}/bookings", status_code=201)
def create_person_booking(
    person_id: str, payload: PersonBookingIn, db: DbSession, user: CurrentUser, ctx=require("booking.edit"),
):
    """登记请假、培训或值守；开跑检查会据此发现执行人时间冲突。"""
    from ...services.staffing_service import StaffingService

    return StaffingService(db, ctx).create(person_id, payload.model_dump(), user)


@router.post("/bookings/{booking_id}/cancel")
def cancel_person_booking(booking_id: str, db: DbSession, user: CurrentUser, ctx=require("booking.edit")):
    from ...services.staffing_service import StaffingService

    return StaffingService(db, ctx).cancel(booking_id, user)
