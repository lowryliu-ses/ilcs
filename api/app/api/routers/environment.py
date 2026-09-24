"""环境读数。步骤声明的环境要求（温湿度、露点、手套箱水氧）按区域 × 指标的最新读数核对。"""
from fastapi import APIRouter

from ...core.errors import PermissionDenied
from ...schemas import EnvironmentBatchIn, EnvironmentReadingIn
from ...services.environment_service import EnvironmentService
from ..deps import Ctx, CurrentUser, DbSession, ServiceCtx, require

router = APIRouter(tags=["environment"])


@router.get("/environment/readings")
def latest_readings(db: DbSession, ctx: Ctx, zone: str = ""):
    """每个区域 × 指标的最新读数（带是否过期）。"""
    return EnvironmentService(db, ctx).latest(zone)


@router.get("/environment/history")
def reading_history(db: DbSession, ctx: Ctx, zone: str, metric: str):
    return EnvironmentService(db, ctx).history(zone, metric)


@router.post("/environment/readings", status_code=201)
def record_reading(payload: EnvironmentReadingIn, db: DbSession, user: CurrentUser, ctx=require("environment.record")):
    """人工抄录一条读数。"""
    return EnvironmentService(db, ctx).record(payload.model_dump(), user, source="manual")


@router.post("/runtime/environment", status_code=201)
def device_readings(payload: EnvironmentBatchIn, db: DbSession, ctx: ServiceCtx):
    """传感器 / 设备上报。服务身份须在 environment_zones 范围内授权区域（"all" 表示全部）。"""
    allowed = (ctx.scopes or {}).get("environment_zones")
    zones = {row.zone for row in payload.readings}
    if allowed != "all" and not (isinstance(allowed, list) and zones <= set(allowed)):
        raise PermissionDenied("该服务身份未被授权上报这些区域的环境读数", code="zone_not_authorized")
    return EnvironmentService(db, ctx).record({"readings": [row.model_dump() for row in payload.readings]}, None, source="device")
