from fastapi import APIRouter

from ...schemas import (
    AbortIn, BatchCreateIn, DispatchIn, HoldIn, RecoverIn, RescheduleIn, ScheduleIn,
)
from ...services.batch_service import BatchService
from ..deps import Ctx, CurrentUser, DbSession, IdempotencyGuard, Paging, require

router = APIRouter(prefix="/batches", tags=["batch"])


@router.get("")
def list_batches(
    db: DbSession, ctx: Ctx, paging: Paging, state: str | None = None, keyword: str = "",
    paged: bool = False,
):
    """默认返回数组，兼容既有前端；`paged=true` 返回分页对象。

    同一路径不会随机返回两种形状——由调用方显式选择。
    """
    service = BatchService(db, ctx)
    if not paged and not state and not keyword:
        return service.list()
    items, total = service.page(paging.offset, paging.page_size, state, keyword)
    return paging.wrap(items, total)


@router.get("/{batch_id}")
def get_batch(batch_id: str, db: DbSession, ctx: Ctx, user: CurrentUser):
    return BatchService(db, ctx).detail(batch_id, user)


@router.get("/{batch_id}/telemetry")
def batch_telemetry(batch_id: str, db: DbSession, ctx: Ctx):
    return BatchService(db, ctx).telemetry_series(batch_id)


@router.post("", status_code=201)
def create_batch(
    payload: BatchCreateIn, db: DbSession, guard: IdempotencyGuard, user: CurrentUser,
    ctx=require("batch.create"),
):
    body = payload.model_dump()
    guard.bind(ctx, body).required()
    replay = guard.replay()
    if replay is not None:
        return replay
    return guard.remember(
        BatchService(db, ctx).create(
            payload.plan_id, payload.priority, payload.note, user, payload.task_id
        )
    )


@router.post("/{batch_id}/schedule")
def schedule_batch(
    batch_id: str, payload: ScheduleIn, db: DbSession, guard: IdempotencyGuard, user: CurrentUser,
    ctx=require("batch.schedule"),
):
    body = payload.model_dump(mode="json")
    guard.bind(ctx, body).required()
    replay = guard.replay()
    if replay is not None:
        return replay
    return guard.remember(
        BatchService(db, ctx).schedule_batch(
            batch_id, payload.start_from, payload.prefer_station_id, user
        )
    )


@router.post("/{batch_id}/reschedule")
def reschedule_batch(
    batch_id: str, payload: RescheduleIn, db: DbSession, user: CurrentUser,
    ctx=require("batch.schedule"),
):
    """加急与重排。只改未执行部分，已执行与正在执行的步骤保持占用。"""
    return BatchService(db, ctx).reschedule(batch_id, payload.from_step, payload.start_from, user)


@router.get("/{batch_id}/preflight")
def preflight(
    batch_id: str, db: DbSession, ctx: Ctx, user: CurrentUser, manual_review: bool = False
):
    service = BatchService(db, ctx)
    batch = service._require(batch_id)
    return service.preflight(batch, user, manual_review)


@router.post("/{batch_id}/dispatch")
def dispatch(
    batch_id: str, payload: DispatchIn, db: DbSession, guard: IdempotencyGuard, user: CurrentUser,
    ctx=require("batch.control"),
):
    body = payload.model_dump()
    guard.bind(ctx, body).required()
    replay = guard.replay()
    if replay is not None:
        return replay
    return guard.remember(
        BatchService(db, ctx).dispatch(
            batch_id, payload.manual_review, payload.reason, payload.signature_id, user
        )
    )


@router.post("/{batch_id}/unschedule")
def unschedule_batch(batch_id: str, db: DbSession, user: CurrentUser, ctx=require("batch.schedule")):
    """取消排程：退回待排程并归还工位时间窗，物料预留保持不变。"""
    return BatchService(db, ctx).unschedule(batch_id, user)


@router.delete("/{batch_id}")
def delete_batch(batch_id: str, db: DbSession, user: CurrentUser, ctx=require("batch.control")):
    """只删未下发的批次。独立登记或已流转的物理样本不会被级联删除。"""
    return BatchService(db, ctx).delete(batch_id, user)


@router.post("/{batch_id}/hold")
def hold(
    batch_id: str, payload: HoldIn, db: DbSession, guard: IdempotencyGuard, user: CurrentUser,
    ctx=require("batch.control"),
):
    body = payload.model_dump()
    guard.bind(ctx, body).required()
    replay = guard.replay()
    if replay is not None:
        return replay
    return guard.remember(BatchService(db, ctx).hold(batch_id, payload.reason, user))


@router.get("/{batch_id}/recovery-options")
def recovery_options(batch_id: str, db: DbSession, ctx: Ctx):
    return BatchService(db, ctx).recovery_options(batch_id)


@router.post("/{batch_id}/recover")
def recover(
    batch_id: str, payload: RecoverIn, db: DbSession, guard: IdempotencyGuard, user: CurrentUser,
    ctx=require("batch.recover"),
):
    body = payload.model_dump()
    guard.bind(ctx, body).required()
    replay = guard.replay()
    if replay is not None:
        return replay
    return guard.remember(
        BatchService(db, ctx).recover(
            batch_id, payload.strategy, payload.verified, payload.signature_id, user
        )
    )


@router.post("/{batch_id}/abort")
def abort(
    batch_id: str, payload: AbortIn, db: DbSession, guard: IdempotencyGuard, user: CurrentUser,
    ctx=require("batch.control"),
):
    body = payload.model_dump()
    guard.bind(ctx, body).required()
    replay = guard.replay()
    if replay is not None:
        return replay
    return guard.remember(
        BatchService(db, ctx).abort(batch_id, payload.reason, payload.signature_id, user)
    )
