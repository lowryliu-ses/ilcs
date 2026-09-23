from fastapi import APIRouter

from ...schemas import ApplyOptimizedIn, OptimizeIn
from ...services.gate_service import GateService
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
    return ScheduleService(db, ctx).optimize_preview(payload.batch_ids, payload.start_from)


@router.post("/schedule/optimize/apply")
def apply_optimized(payload: ApplyOptimizedIn, db: DbSession, user: CurrentUser, ctx=require("batch.schedule")):
    return ScheduleService(db, ctx).apply_optimized(payload.order, user, payload.start_from)
