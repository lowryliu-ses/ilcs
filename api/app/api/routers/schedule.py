from fastapi import APIRouter

from ...schemas import ApplyOptimizedIn, OptimizeIn, ProposalDismissIn, ProposalRequestIn
from ...services.gate_service import GateService
from ...services.reschedule_service import RescheduleService
from ...services.schedule_service import ScheduleService
from ..deps import Ctx, CurrentUser, DbSession, require

router = APIRouter(tags=["schedule"])


@router.get("/gate")
def gate(db: DbSession):
    return GateService(db).status()


@router.get("/schedule/board")
def board(db: DbSession, ctx: Ctx):
    return ScheduleService(db, ctx).board()


@router.get("/schedule/queue")
def queue(db: DbSession, ctx: Ctx):
    return ScheduleService(db, ctx).queue()


@router.post("/schedule/optimize")
def optimize(payload: OptimizeIn, db: DbSession, user: CurrentUser, ctx=require("batch.schedule")):
    return ScheduleService(db, ctx).optimize_preview(payload.batch_ids, payload.start_from, payload.mode)


@router.post("/schedule/optimize/apply")
def apply_optimized(payload: ApplyOptimizedIn, db: DbSession, user: CurrentUser, ctx=require("batch.schedule")):
    return ScheduleService(db, ctx).apply_optimized(payload.order, user, payload.start_from)


@router.get("/schedule/proposals")
def list_proposals(db: DbSession, ctx: Ctx, state: str = ""):
    """重排建议：工位不可用、指令故障、紧急插单、人工请求时生成，调度确认后才写入时间线。"""
    return RescheduleService(db, ctx).list(state)


@router.post("/schedule/proposals", status_code=201)
def request_proposal(payload: ProposalRequestIn, db: DbSession, user: CurrentUser, ctx=require("batch.schedule")):
    return RescheduleService(db, ctx).request(payload.batch_ids, payload.station_id, payload.reason, user)


@router.post("/schedule/proposals/{proposal_id}/apply")
def apply_proposal(proposal_id: str, db: DbSession, user: CurrentUser, ctx=require("batch.schedule")):
    """应用重排建议。建议生成后时间线被改过、或又有步骤开出，建议作废（409 proposal_stale）。"""
    return RescheduleService(db, ctx).apply(proposal_id, user)


@router.post("/schedule/proposals/{proposal_id}/dismiss")
def dismiss_proposal(
    proposal_id: str, payload: ProposalDismissIn, db: DbSession, user: CurrentUser, ctx=require("batch.schedule"),
):
    return RescheduleService(db, ctx).dismiss(proposal_id, payload.note, user)
