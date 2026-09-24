from fastapi import APIRouter

from fastapi.responses import Response

from ...core.errors import ValidationFailed
from ...schemas import CancelIn, DecisionIn, PlanCreateIn, PlanPatchIn, PlanRestoreIn, PlanSubmitIn, PlanTemplateIn, ProposalIn
from ...services.plan_service import PlanService
from ...services.proposal_service import ProposalService
from ..deps import Ctx, CurrentUser, DbSession, Paging, require

router = APIRouter(prefix="/plans", tags=["plan"])


@router.get("/approvers")
def plan_approvers(db: DbSession, ctx: Ctx):
    """可被指定为方案审批人的成员（有批准实验方案权限）。"""
    return PlanService(db, ctx).approvers()


@router.get("/templates")
def list_plan_templates(db: DbSession, ctx: Ctx, include_retired: bool = False):
    """方案模板库。新建方案时带 template_id 套用。"""
    return PlanService(db, ctx).templates(include_retired)


@router.post("/templates", status_code=201)
def create_plan_template(payload: PlanTemplateIn, db: DbSession, user: CurrentUser, ctx=require("plan.edit")):
    return PlanService(db, ctx).create_template(payload.model_dump(exclude_none=True), user)


@router.post("/templates/{template_id}/retire")
def retire_plan_template(template_id: str, db: DbSession, user: CurrentUser, ctx=require("plan.edit")):
    return PlanService(db, ctx).retire_template(template_id, user)


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
    # 套用模板时只取请求里显式给的字段，其余用模板的
    body = payload.model_dump(exclude_unset=True) if payload.template_id else payload.model_dump()
    if not body.get("recipe_id") and not payload.template_id:
        raise ValidationFailed("方案必须选择方法")
    return PlanService(db, ctx).create(body, user)


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
def submit_plan(
    plan_id: str, db: DbSession, user: CurrentUser, payload: PlanSubmitIn | None = None, ctx=require("plan.submit"),
):
    """提交评审。可带多级审批（每级可指定审批人），逐级审，最后一级通过才算批准。"""
    approvers = [row.model_dump() for row in payload.approvers] if payload else []
    return PlanService(db, ctx).submit(plan_id, user, approvers)


@router.post("/{plan_id}/withdraw")
def withdraw_plan(plan_id: str, payload: CancelIn, db: DbSession, user: CurrentUser, ctx: Ctx):
    """撤回评审回到草稿（提交人本人或有编辑权限的人）。指定的审批人审不了时用它解开。"""
    return PlanService(db, ctx).withdraw(plan_id, payload.reason, user)


@router.get("/{plan_id}/diff")
def diff_plan(plan_id: str, db: DbSession, ctx: Ctx, from_version: int, to_version: int | None = None):
    """两个版本（或历史版本与当前内容）的字段级差异。"""
    return PlanService(db, ctx).diff(plan_id, from_version, to_version)


@router.post("/{plan_id}/restore")
def restore_plan(plan_id: str, payload: PlanRestoreIn, db: DbSession, user: CurrentUser, ctx=require("plan.edit")):
    """把历史版本的内容恢复到当前草稿；历史版本快照不变，恢复后照常评审。"""
    return PlanService(db, ctx).restore(plan_id, payload.from_version, user, payload.row_version)


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
