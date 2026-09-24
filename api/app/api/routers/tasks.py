from fastapi import APIRouter

from ...schemas import CancelIn, TaskAssignIn, TaskCreateIn, TaskDecomposeIn, TaskDependenciesIn
from ...services.task_service import TaskService
from ..deps import Ctx, CurrentUser, DbSession, IdempotencyGuard, Paging, require

router = APIRouter(prefix="/experiment-tasks", tags=["task"])


@router.get("")
def list_tasks(
    db: DbSession, ctx: Ctx, paging: Paging, state: str | None = None, assignee: str = "",
    plan_id: str = "",
):
    items, total = TaskService(db, ctx).page(
        paging.offset, paging.page_size, state, assignee, plan_id
    )
    return paging.wrap(items, total)


@router.get("/mine")
def my_tasks(db: DbSession, ctx: Ctx, user: CurrentUser):
    return TaskService(db, ctx).my_tasks(user.id)


@router.get("/{task_id}")
def get_task(task_id: str, db: DbSession, ctx: Ctx):
    return TaskService(db, ctx).detail(task_id)


@router.post("", status_code=201)
def create_task(
    payload: TaskCreateIn, db: DbSession, guard: IdempotencyGuard, user: CurrentUser,
    ctx=require("task.create"),
):
    body = payload.model_dump()
    guard.bind(ctx, body).required()
    replay = guard.replay()
    if replay is not None:
        return replay
    return guard.remember(TaskService(db, ctx).create(body, user))


@router.post("/{task_id}/assign")
def assign_task(
    task_id: str, payload: TaskAssignIn, db: DbSession, user: CurrentUser,
    ctx=require("task.assign"),
):
    """分配或转派。按预计执行时间校验资质；转派必须写原因。"""
    return TaskService(db, ctx).assign(task_id, payload.model_dump(), user)


@router.post("/{task_id}/accept")
def accept_task(task_id: str, db: DbSession, user: CurrentUser, ctx=require("task.accept")):
    return TaskService(db, ctx).accept(task_id, user)


@router.post("/{task_id}/cancel")
def cancel_task(
    task_id: str, payload: CancelIn, db: DbSession, user: CurrentUser, ctx=require("task.cancel")
):
    return TaskService(db, ctx).cancel(task_id, payload.reason, user)


@router.post("/{task_id}/decompose")
def decompose_task(
    task_id: str, payload: TaskDecomposeIn, db: DbSession, guard: IdempotencyGuard, user: CurrentUser,
    ctx=require("task.create"),
):
    """拆成子任务。父任务不绑定批次，状态由子任务汇总；每个子任务各建一个批次。"""
    body = payload.model_dump()
    guard.bind(ctx, body).required()
    replay = guard.replay()
    if replay is not None:
        return replay
    return guard.remember(TaskService(db, ctx).decompose(task_id, body, user))


@router.put("/{task_id}/dependencies")
def set_task_dependencies(
    task_id: str, payload: TaskDependenciesIn, db: DbSession, user: CurrentUser, ctx=require("task.assign"),
):
    """设置上游任务（完成—开始）。成环、依赖自己的子任务、已下发批次再加上游都拒绝。"""
    return TaskService(db, ctx).set_dependencies(task_id, payload.depends_on, user)
