from fastapi import APIRouter

from ...services.audit_service import AuditService
from ...services.batch_service import BatchService
from ...services.dashboard_service import DashboardService
from ..deps import Ctx, CurrentUser, DbSession, Paging

router = APIRouter(tags=["governance"])


@router.get("/dashboard")
def dashboard(db: DbSession, ctx: Ctx, user: CurrentUser):
    return DashboardService(db, ctx).overview(user)


@router.get("/dashboard/kpi")
def dashboard_kpi(db: DbSession, ctx: Ctx, window_hours: float = 24):
    """运行驾驶舱指标：实验运行、自动化成功率、异常与平均恢复时长、设备利用率（实际与计划）。"""
    return DashboardService(db, ctx).kpi(window_hours)


@router.get("/handover")
def handover(db: DbSession, ctx: Ctx):
    return BatchService(db, ctx).handover()


@router.get("/audit")
def audit_log(
    db: DbSession, ctx: Ctx, paging: Paging, limit: int = 200, target: str | None = None,
    action: str | None = None, paged: bool = False,
):
    """审计查询也在访问范围内：跨组织的记录读不到。"""
    service = AuditService(db, ctx)
    if paged or action:
        rows, total = service.page(paging.offset, paging.page_size, target, action)
        return paging.wrap([_out(event) for event in rows], total)
    events = service.for_target(target) if target else service.recent(limit)
    return [_out(event) for event in events]


def _out(event) -> dict:
    return {
        "id": event.id,
        "time": event.time.isoformat(timespec="seconds"),
        "user": event.user,
        "user_id": event.user_id,
        "role": event.role,
        "action": event.action,
        "target": event.target,
        "object_version": event.object_version,
        "sign": event.sign,
        "meaning": event.meaning,
        "before": event.before,
        "after": event.after,
        "detail": event.detail,
        "signature_id": event.signature_id,
        "command_id": event.command_id,
        "checkpoint_id": event.checkpoint_id,
        "request_id": event.request_id,
    }
