from fastapi import APIRouter

from ...schemas import AlarmClearIn, ShelveIn
from ...services.alarm_service import AlarmService
from ..deps import Ctx, CurrentUser, DbSession, ServiceCtx

router = APIRouter(prefix="/alarms", tags=["alarm"])


@router.get("")
def list_alarms(db: DbSession, ctx: Ctx):
    return AlarmService(db, ctx).list()


@router.post("/{alarm_id}/ack")
def ack(alarm_id: str, db: DbSession, user: CurrentUser, ctx: Ctx):
    return AlarmService(db, ctx).ack(alarm_id, user)


@router.post("/{alarm_id}/shelve")
def shelve(alarm_id: str, payload: ShelveIn, db: DbSession, user: CurrentUser, ctx: Ctx):
    return AlarmService(db, ctx).shelve(alarm_id, payload.until, user)


@router.post("/{alarm_id}/close")
def close(alarm_id: str, db: DbSession, user: CurrentUser, ctx: Ctx):
    return AlarmService(db, ctx).close(alarm_id, user)


@router.post("/{alarm_id}/condition-cleared")
def condition_cleared(alarm_id: str, db: DbSession, ctx: ServiceCtx):
    """设备侧条件恢复事件。走服务认证，不是人工按钮。"""
    return AlarmService(db, ctx).condition_cleared(alarm_id)


@router.post("/{alarm_id}/clear-condition")
def clear_condition(alarm_id: str, payload: AlarmClearIn, db: DbSession, user: CurrentUser, ctx: Ctx):
    """清除软件判定报警的异常条件：写明原因并签名。设备侧条件报警不走这里。"""
    return AlarmService(db, ctx).clear_condition(alarm_id, payload.reason, payload.signature_id, user)
