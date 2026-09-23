from fastapi import APIRouter

from fastapi.responses import Response

from ...schemas import DecisionIn, PlanCreateIn, PlanPatchIn, ProposalIn
from ...services.plan_service import PlanService
from ...services.proposal_service import ProposalService
from ..deps import Ctx, CurrentUser, DbSession, Paging, require

router = APIRouter(prefix="/plans", tags=["plan"])


@router.get("")
def list_plans(
    db: DbSession, ctx: Ctx, paging: Paging, plan_type: str | None = None, keyword: str = "",
    paged: bool = False,
):
    service = PlanService(db, ctx)
    if not paged and not plan_type and not keyword:
        return service.list()
    items, total = service.page(paging.offset, paging.page_size, plan_type, keyword)
    return paging.wrap(items, total)


@router.get("/{plan_id}")
def get_plan(plan_id: str, db: DbSession, ctx: Ctx):
    return PlanService(db, ctx).get(plan_id)


@router.post("", status_code=201)
def create_plan(payload: PlanCreateIn, db: DbSession, user: CurrentUser, ctx=require("plan.edit")):
    return PlanService(db, ctx).create(payload.model_dump(), user)


@router.patch("/{plan_id}")
def patch_plan(
    plan_id: str, payload: PlanPatchIn, db: DbSession, user: CurrentUser, ctx=require("plan.edit")
):
    changes = payload.model_dump(exclude_unset=True, exclude_none=True)
    return PlanService(db, ctx).patch(plan_id, changes, user)


@router.delete("/{plan_id}")
def delete_plan(plan_id: str, db: DbSession, user: CurrentUser, ctx=require("plan.edit")):
    """只删草稿。已锁定先解锁；已批准、已绑定批次或已有任务的不能删。"""
    return PlanService(db, ctx).delete(plan_id, user)


@router.post("/{plan_id}/lock")
def lock_plan(plan_id: str, db: DbSession, user: CurrentUser, ctx=require("plan.edit")):
    """锁定结构。这是结构冻结，不代表已审批。"""
    return PlanService(db, ctx).lock(plan_id, user)


@router.post("/{plan_id}/unlock")
def unlock_plan(plan_id: str, db: DbSession, user: CurrentUser, ctx=require("plan.edit")):
    return PlanService(db, ctx).unlock(plan_id, user)


@router.post("/{plan_id}/submit")
def submit_plan(plan_id: str, db: DbSession, user: CurrentUser, ctx=require("plan.submit")):
    return PlanService(db, ctx).submit(plan_id, user)


@router.post("/{plan_id}/decision")
def decide_plan(
    plan_id: str, payload: DecisionIn, db: DbSession, user: CurrentUser, ctx=require("plan.approve")
):
    """批准或驳回。批准后版本冻结；作者不能批准自己写的方案。"""
    return PlanService(db, ctx).decide(plan_id, payload.model_dump(), user)


@router.post("/{plan_id}/revisions", status_code=201)
def revise_plan(plan_id: str, db: DbSession, user: CurrentUser, ctx=require("plan.edit")):
    """修订已批准方案，生成新版本。原批准版本快照保留。"""
    return PlanService(db, ctx).revise(plan_id, user)


@router.get("/{plan_id}/proposals")
def list_proposals(plan_id: str, db: DbSession, ctx: Ctx):
    """这个方案收到过的提案，含被拒绝的与拒绝原因。"""
    return ProposalService(db, ctx).list(plan_id)


@router.post("/{plan_id}/proposals", status_code=201)
def submit_proposal(
    plan_id: str, payload: ProposalIn, db: DbSession, user: CurrentUser, ctx=require("plan.edit"),
):
    """研究员提交下一轮提案。校验通过只生成方案草稿，仍需锁定、提交并由 QA 批准。"""
    return ProposalService(db, ctx).submit(plan_id, payload.model_dump(), user)


@router.get("/{plan_id}/dataset.csv")
def export_dataset(plan_id: str, db: DbSession, ctx: Ctx):
    """整个实验活动（各轮方案）的训练数据：只含复核通过、质量有效的当前结果版本。"""
    content = ProposalService(db, ctx).dataset_csv(plan_id)
    return Response(
        content="\ufeff" + content, media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{plan_id}-dataset.csv"'},
    )
