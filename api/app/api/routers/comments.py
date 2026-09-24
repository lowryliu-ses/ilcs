"""批注：方案、SOP 版本、方法、报告版本上的评审意见。"""
from typing import Literal

from fastapi import APIRouter

from ...schemas import CommentIn
from ...services.comment_service import CommentService
from ..deps import Ctx, CurrentUser, DbSession

router = APIRouter(prefix="/comments", tags=["comment"])


@router.get("")
def list_comments(
    target_type: Literal["plan", "sop_version", "recipe", "report_version"], target_id: str, db: DbSession, ctx: Ctx,
):
    return CommentService(db, ctx).list(target_type, target_id)


@router.post("", status_code=201)
def create_comment(payload: CommentIn, db: DbSession, user: CurrentUser, ctx: Ctx):
    return CommentService(db, ctx).create(payload.model_dump(), user)


@router.post("/{comment_id}/resolve")
def resolve_comment(comment_id: str, db: DbSession, user: CurrentUser, ctx: Ctx):
    return CommentService(db, ctx).resolve(comment_id, user)
