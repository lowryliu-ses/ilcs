from fastapi import APIRouter

from ...schemas import StepReviewIn, StepSubmitIn
from ...services.workflow_service import WorkflowService
from ..deps import Ctx, CurrentUser, DbSession, IdempotencyGuard, require

router = APIRouter(prefix="/step-runs", tags=["workflow"])


@router.get("/mine")
def my_steps(db: DbSession, ctx: Ctx, user: CurrentUser):
    """我的人工待办。"""
    return WorkflowService(db, ctx).my_manual_todos(user.id)


@router.get("/reviews")
def review_queue(db: DbSession, ctx: Ctx):
    return WorkflowService(db, ctx).review_todos()


@router.post("/{step_run_id}/submit")
def submit_step(
    step_run_id: str, payload: StepSubmitIn, db: DbSession, guard: IdempotencyGuard,
    user: CurrentUser, ctx=require("step.submit"),
):
    """人工步骤提交。缺必填项、缺样本或物料核对一律不推进。"""
    body = payload.model_dump()
    guard.bind(ctx, body).required()
    replay = guard.replay()
    if replay is not None:
        return replay
    return guard.remember(WorkflowService(db, ctx).submit_manual(step_run_id, body, user))


@router.post("/{step_run_id}/review")
def review_step(
    step_run_id: str, payload: StepReviewIn, db: DbSession, guard: IdempotencyGuard,
    user: CurrentUser, ctx=require("step.review"),
):
    """审核节点批准或退回。不接受任意目标状态。"""
    body = payload.model_dump()
    guard.bind(ctx, body).required()
    replay = guard.replay()
    if replay is not None:
        return replay
    return guard.remember(WorkflowService(db, ctx).decide_review(step_run_id, body, user))
