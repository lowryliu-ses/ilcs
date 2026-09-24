from fastapi import APIRouter

from ...schemas import DecisionIn, RetireIn, SopRecipeIn, SopRestoreIn, SopStepsIn, SopVersionCreateIn, SopVersionPatchIn
from ...services.sop_service import SopService
from ..deps import Ctx, CurrentUser, DbSession, Paging, require

router = APIRouter(prefix="/sops", tags=["sop"])


@router.get("")
def list_sops(db: DbSession, ctx: Ctx, paging: Paging, state: str | None = None):
    items, total = SopService(db, ctx).page(paging.offset, paging.page_size, state)
    return paging.wrap(items, total)


@router.get("/effective")
def effective(db: DbSession, ctx: Ctx, capability_id: str = ""):
    """给方法编辑器用：某能力当前生效的 SOP 版本。"""
    return SopService(db, ctx).effective_for_capability(capability_id)


@router.get("/{version_id}")
def get_version(version_id: str, db: DbSession, ctx: Ctx):
    return SopService(db, ctx).detail(version_id)


@router.post("", status_code=201)
def create_version(
    payload: SopVersionCreateIn, db: DbSession, user: CurrentUser, ctx=require("sop.edit")
):
    return SopService(db, ctx).create_version(payload.model_dump(), user)


@router.patch("/{version_id}")
def update_version(
    version_id: str, payload: SopVersionPatchIn, db: DbSession, user: CurrentUser,
    ctx=require("sop.edit"),
):
    """只改草稿。已发布内容不可原位编辑，请建立新版本。"""
    changes = payload.model_dump(exclude_unset=True, exclude_none=True, exclude={"row_version"})
    return SopService(db, ctx).update_version(version_id, changes, payload.row_version, user)


@router.put("/{version_id}/steps")
def update_steps(version_id: str, payload: SopStepsIn, db: DbSession, user: CurrentUser, ctx=require("sop.edit")):
    """数字 SOP：结构化步骤（设备 / 人工 / 等待 / 审核）。只有草稿能改。"""
    return SopService(db, ctx).update_steps(version_id, [row.model_dump() for row in payload.steps], payload.row_version, user)


@router.post("/{version_id}/generate-recipe", status_code=201)
def generate_recipe(version_id: str, payload: SopRecipeIn, db: DbSession, user: CurrentUser, ctx=require("recipe.edit")):
    """按结构化步骤一键生成方法草稿；已发布的 SOP 版本同时挂到方法上。"""
    return SopService(db, ctx).generate_recipe(version_id, payload.model_dump(), user)


@router.get("/{version_id}/diff")
def diff_versions(version_id: str, db: DbSession, ctx: Ctx, against: str):
    """与同一 SOP 的另一个版本对比：附件、适用范围、结构化步骤。"""
    return SopService(db, ctx).diff(against, version_id)


@router.post("/{version_id}/restore", status_code=201)
def restore_version(version_id: str, payload: SopRestoreIn, db: DbSession, user: CurrentUser, ctx=require("sop.edit")):
    """从这个历史版本恢复：产生新的草稿版本，历史版本不变。"""
    return SopService(db, ctx).restore(version_id, payload.version, user)


@router.post("/{version_id}/submit")
def submit_version(version_id: str, db: DbSession, user: CurrentUser, ctx=require("sop.edit")):
    return SopService(db, ctx).submit(version_id, user)


@router.post("/{version_id}/decision")
def decide_version(
    version_id: str, payload: DecisionIn, db: DbSession, user: CurrentUser,
    ctx=require("sop.approve"),
):
    """批准并发布，或驳回回草稿。作者不能批准自己写的版本。"""
    return SopService(db, ctx).decide(version_id, payload.model_dump(), user)


@router.post("/{version_id}/retire")
def retire_version(
    version_id: str, payload: RetireIn, db: DbSession, user: CurrentUser,
    ctx=require("sop.approve"),
):
    return SopService(db, ctx).retire(version_id, payload.reason, user)


@router.post("/{version_id}/acknowledge")
def acknowledge(version_id: str, db: DbSession, user: CurrentUser, ctx: Ctx):
    """阅读确认。要求培训确认的 SOP 靠它放行执行人。"""
    return SopService(db, ctx).acknowledge(version_id, user)
