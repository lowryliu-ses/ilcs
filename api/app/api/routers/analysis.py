from fastapi import APIRouter

from ...schemas import (
    AnalysisTaskCreateIn, CancelIn, ManualResultIn, ResultIngestIn, ResultReviewIn,
    ResultRevisionIn, RetestIn,
)
from ...services.analysis_service import AnalysisService
from ..deps import Ctx, CurrentUser, DbSession, IdempotencyGuard, Paging, ServiceCtx, require

router = APIRouter(tags=["analysis"])


@router.get("/analysis-tasks")
def list_analysis_tasks(
    db: DbSession, ctx: Ctx, paging: Paging, state: str | None = None, sample_id: str = ""
):
    items, total = AnalysisService(db, ctx).page_tasks(
        paging.offset, paging.page_size, state, sample_id
    )
    return paging.wrap(items, total)


@router.get("/analysis-tasks/{task_id}")
def get_analysis_task(task_id: str, db: DbSession, ctx: Ctx):
    return AnalysisService(db, ctx).task_detail(task_id)


@router.post("/analysis-tasks", status_code=201)
def create_analysis_task(
    payload: AnalysisTaskCreateIn, db: DbSession, user: CurrentUser, ctx=require("analysis.create")
):
    """建任务时冻结要求指标集合。"""
    return AnalysisService(db, ctx).create_task_api(payload.model_dump(), user)


@router.post("/analysis-tasks/{task_id}/retests", status_code=201)
def retest(
    task_id: str, payload: RetestIn, db: DbSession, user: CurrentUser,
    ctx=require("analysis.create"),
):
    """重测：新任务 + 新轮次，不覆盖原任务与原结果。"""
    return AnalysisService(db, ctx).retest(task_id, payload.model_dump(), user)


@router.post("/analysis-tasks/{task_id}/cancel")
def cancel_analysis_task(
    task_id: str, payload: CancelIn, db: DbSession, user: CurrentUser,
    ctx=require("analysis.create"),
):
    return AnalysisService(db, ctx).cancel_task(task_id, payload.reason, user)


@router.post("/analysis-tasks/{task_id}/results")
def enter_manual_result(
    task_id: str, payload: ManualResultIn, db: DbSession, user: CurrentUser,
    ctx=require("result.enter"),
):
    """人工录入。记录录入人，之后本人不能审核这条记录。"""
    return AnalysisService(db, ctx).enter_manual(task_id, payload.model_dump(), user)


@router.post("/integrations/results")
def ingest_result(payload: ResultIngestIn, db: DbSession, ctx: ServiceCtx):
    """结果回传入口。

    来源由服务认证确定；事件编号必填；一次多指标原子入账，任一项非法整次拒绝。
    相同事件重传返回原事件与原结果并带 `replayed` 标记。
    """
    return AnalysisService(db, ctx).ingest(payload.model_dump())


@router.get("/result-values")
def list_result_values(
    db: DbSession, ctx: Ctx, paging: Paging, review_state: str = "", quality: str = ""
):
    items, total = AnalysisService(db, ctx).page_values(
        paging.offset, paging.page_size, review_state, quality
    )
    return paging.wrap(items, total)


@router.get("/result-values/pending")
def pending_reviews(db: DbSession, ctx: Ctx):
    return AnalysisService(db, ctx).review_queue()


@router.post("/result-values/{value_id}/review")
def review_result(
    value_id: str, payload: ResultReviewIn, db: DbSession, guard: IdempotencyGuard,
    user: CurrentUser, ctx=require("result.review"),
):
    """复核。绑定确切结果版本；本人不能审核本人录入的记录。"""
    body = payload.model_dump()
    guard.bind(ctx, body).required()
    replay = guard.replay()
    if replay is not None:
        return replay
    return guard.remember(AnalysisService(db, ctx).review(value_id, body, user))


@router.post("/result-values/{value_id}/revisions", status_code=201)
def revise_result(
    value_id: str, payload: ResultRevisionIn, db: DbSession, guard: IdempotencyGuard,
    user: CurrentUser, ctx=require("result.enter"),
):
    """更正产生新版本，显式引用原版本并说明原因；旧记录不被覆盖。"""
    body = payload.model_dump()
    guard.bind(ctx, body).required()
    replay = guard.replay()
    if replay is not None:
        return replay
    return guard.remember(AnalysisService(db, ctx).revise(value_id, body, user))
