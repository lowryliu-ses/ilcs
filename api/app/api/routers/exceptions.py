"""异常事件中心与策略库。"""
from fastapi import APIRouter

from ...schemas import ExceptionHandleIn, ExceptionRuleIn
from ...services.exception_service import ExceptionService
from ..deps import Ctx, CurrentUser, DbSession

router = APIRouter(tags=["exception"])


@router.get("/exceptions")
def list_exceptions(db: DbSession, ctx: Ctx, state: str = "", category: str = "", batch_id: str = "", limit: int = 200):
    """统一异常事件：类别、来源、影响面、自动处理与结果、人工处理与最终结果。state=open 取待处理与处理中。"""
    return ExceptionService(db, ctx).list(state, category, batch_id, min(max(limit, 1), 500))


@router.get("/exceptions/summary")
def exception_summary(db: DbSession, ctx: Ctx):
    return ExceptionService(db, ctx).summary()


@router.get("/exceptions/{event_id}")
def get_exception(event_id: str, db: DbSession, ctx: Ctx):
    service = ExceptionService(db, ctx)
    return service.out(service.get(event_id))


@router.post("/exceptions/{event_id}/handle")
def handle_exception(event_id: str, payload: ExceptionHandleIn, db: DbSession, user: CurrentUser, ctx: Ctx):
    """人工处理：认领、写明结果后标为已恢复、或关闭。"""
    return ExceptionService(db, ctx).handle(event_id, payload.action, payload.note, user)


@router.get("/exception-rules")
def list_rules(db: DbSession, ctx: Ctx):
    return ExceptionService(db, ctx).rules_out()


@router.post("/exception-rules", status_code=201)
def create_rule(payload: ExceptionRuleIn, db: DbSession, user: CurrentUser, ctx: Ctx):
    """新增策略。安全联锁只能转人工；会重新驱动设备的动作只在指令从未送达设备时生效。"""
    return ExceptionService(db, ctx).save_rule(payload.model_dump(), user)


@router.put("/exception-rules/{rule_id}")
def update_rule(rule_id: str, payload: ExceptionRuleIn, db: DbSession, user: CurrentUser, ctx: Ctx):
    return ExceptionService(db, ctx).save_rule(payload.model_dump(), user, rule_id)
