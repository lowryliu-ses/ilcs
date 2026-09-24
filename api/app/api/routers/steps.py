from fastapi import APIRouter

from ...schemas import BranchDecisionIn, GateDecisionIn, StepReviewIn, StepSubmitIn
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


@router.get("/branches")
def branch_queue(db: DbSession, ctx: Ctx):
    """待人工选择出口的条件分支。"""
    return WorkflowService(db, ctx).branch_todos()


@router.post("/{step_run_id}/branch-decision")
def decide_branch(
    step_run_id: str, payload: BranchDecisionIn, db: DbSession, guard: IdempotencyGuard,
    user: CurrentUser, ctx: Ctx,
):
    """人工选择分支出口。人工选择模式要 step.submit；判据缺失而保持的分支要 step.review 并签名。"""
    body = payload.model_dump()
    guard.bind(ctx, body).required()
    replay = guard.replay()
    if replay is not None:
        return replay
    return guard.remember(WorkflowService(db, ctx).decide_branch(step_run_id, body, user))


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


@router.post("/{step_run_id}/gate-decision")
def decide_gate(
    step_run_id: str, payload: GateDecisionIn, db: DbSession, guard: IdempotencyGuard,
    user: CurrentUser, ctx=require("step.review"),
):
    """保持中的质检关卡：QA 签名放行，或判不合格按报废处理。"""
    body = payload.model_dump()
    guard.bind(ctx, body).required()
    replay = guard.replay()
    if replay is not None:
        return replay
    return guard.remember(WorkflowService(db, ctx).decide_gate(step_run_id, body, user))
