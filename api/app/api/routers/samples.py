from fastapi import APIRouter

from ...schemas import CancelIn, SampleCreateIn, SampleReceiveIn, SampleSplitIn, SampleTransferIn
from ...services.sample_service import SampleService
from ..deps import Ctx, CurrentUser, DbSession, IdempotencyGuard, Paging, require

router = APIRouter(prefix="/samples", tags=["sample"])


@router.get("")
def list_samples(
    db: DbSession, ctx: Ctx, paging: Paging, keyword: str = "", state: str | None = None,
    project_id: str = "",
):
    items, total = SampleService(db, ctx).page(
        paging.offset, paging.page_size, keyword, state, project_id
    )
    return paging.wrap(items, total)


@router.get("/{sample_id}")
def get_sample(sample_id: str, db: DbSession, ctx: Ctx):
    """样本详情：来源谱系、运行分配、流转、检测任务与附件分开呈现。"""
    return SampleService(db, ctx).detail(sample_id)


@router.post("", status_code=201)
def register_sample(
    payload: SampleCreateIn, db: DbSession, guard: IdempotencyGuard, user: CurrentUser,
    ctx=require("sample.register"),
):
    body = payload.model_dump()
    guard.bind(ctx, body).required()
    replay = guard.replay()
    if replay is not None:
        return replay
    return guard.remember(SampleService(db, ctx).register(body, user))


@router.post("/{sample_id}/receive")
def receive_sample(
    sample_id: str, payload: SampleReceiveIn, db: DbSession, user: CurrentUser,
    ctx=require("sample.transfer"),
):
    """扫码接收。重复扫描由 event_key 去重，返回 replayed 标记。"""
    return SampleService(db, ctx).receive(sample_id, payload.model_dump(), user)


@router.post("/{sample_id}/transfers")
def transfer_sample(
    sample_id: str, payload: SampleTransferIn, db: DbSession, user: CurrentUser,
    ctx=require("sample.transfer"),
):
    return SampleService(db, ctx).transfer(sample_id, payload.model_dump(), user)


@router.get("/{sample_id}/transfers")
def list_transfers(sample_id: str, db: DbSession, ctx: Ctx):
    service = SampleService(db, ctx)
    return [service.transfer_out(row) for row in service.transfers.for_sample(sample_id)]


@router.post("/{sample_id}/split", status_code=201)
def split_sample(
    sample_id: str, payload: SampleSplitIn, db: DbSession, guard: IdempotencyGuard,
    user: CurrentUser, ctx=require("sample.register"),
):
    """分样。校验母样剩余量、子样数量与明确记录的损耗，不允许超量。"""
    body = payload.model_dump()
    guard.bind(ctx, body).required()
    replay = guard.replay()
    if replay is not None:
        return replay
    return guard.remember(SampleService(db, ctx).split(sample_id, body, user))


@router.post("/{sample_id}/dispose")
def dispose_sample(
    sample_id: str, payload: CancelIn, db: DbSession, user: CurrentUser,
    ctx=require("sample.dispose"),
):
    return SampleService(db, ctx).dispose(sample_id, payload.reason, user)
