from fastapi import APIRouter

from ...schemas import MetricCreateIn, MetricPatchIn
from ...services.metric_service import MetricService
from ..deps import Ctx, CurrentUser, DbSession, require

router = APIRouter(prefix="/metrics", tags=["metric"])


@router.get("")
def list_metrics(db: DbSession, ctx: Ctx, only_active: bool = False):
    return MetricService(db, ctx).list(only_active)


@router.post("", status_code=201)
def create_metric(payload: MetricCreateIn, db: DbSession, user: CurrentUser, ctx=require("metric.edit")):
    return MetricService(db, ctx).create(payload.model_dump(), user)


@router.patch("/{metric_id}")
def update_metric(
    metric_id: str, payload: MetricPatchIn, db: DbSession, user: CurrentUser,
    ctx=require("metric.edit"),
):
    """只改没被结果引用过的版本。已引用的必须走修订。"""
    changes = payload.model_dump(exclude_unset=True, exclude_none=True)
    return MetricService(db, ctx).update(metric_id, changes, user)


@router.post("/{metric_id}/revisions", status_code=201)
def revise_metric(
    metric_id: str, payload: MetricCreateIn, db: DbSession, user: CurrentUser,
    ctx=require("metric.edit"),
):
    return MetricService(db, ctx).revise(metric_id, payload.model_dump(), user)


@router.post("/{metric_id}/retire")
def retire_metric(metric_id: str, db: DbSession, user: CurrentUser, ctx=require("metric.edit")):
    return MetricService(db, ctx).retire(metric_id, user)
