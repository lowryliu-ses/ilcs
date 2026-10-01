from fastapi import APIRouter

from ...schemas import (
    CancelIn, ShortfallAcceptIn, TaskAssignIn, TaskBatchesIn, TaskCreateIn, TaskDecomposeIn, TaskDependenciesIn,
    TaskMigrateIn, TaskRetestIn, TaskSplitPreviewIn,
)
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


@router.post("/split-preview")
def split_preview(payload: TaskSplitPreviewIn, db: DbSession, ctx: Ctx):
    """拆分预览（不写库）：按方案的批准版本（建任务前）或任务锁定的版本（拆分前）算出每一份。

    总数超过流程每批样品位时 needs_split 为真，建任务时会按这个分法自动拆成子任务。
    """
    return TaskService(db, ctx).split_preview(payload.model_dump())


@router.get("/{task_id}")
def get_task(task_id: str, db: DbSession, ctx: Ctx):
    return TaskService(db, ctx).detail(task_id)


@router.get("/{task_id}/results")
def task_results(task_id: str, db: DbSession, ctx: Ctx, official: bool = True, metric_ids: str = ""):
    """父任务的合并结果：各子任务批次的观测合在一起，给出合并统计、分批明细与批次差异。"""
    from ...services.report_service import ReportService

    selected = [m for m in metric_ids.split(",") if m] or None
    return ReportService(db, ctx).task_analysis_view(task_id, selected, official)


@router.get("/{task_id}/results/series")
def task_result_series(task_id: str, metric_id: str, db: DbSession, ctx: Ctx, official: bool = True):
    """父任务的曲线叠加：各子任务批次里这个曲线指标的样本曲线（终止的批次不画，与合并统计同一口径）。"""
    from ...services.report_service import ReportService

    service = ReportService(db, ctx)
    task, leaves = service.task_batches(task_id)
    batch_ids = [batch.id for _, batch in leaves if batch.state != "aborted"] or [batch.id for _, batch in leaves]
    return {"task_id": task.id, **service.series_view(batch_ids, metric_id, official)}


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
    """设置上游任务与放行条件。成环（含从父任务继承的依赖）、依赖自己的子任务、已下发批次再加上游都拒绝。"""
    return TaskService(db, ctx).set_dependencies(task_id, payload.depends_on, user, payload.gate)


@router.post("/{task_id}/migrate-version")
def migrate_task_version(
    task_id: str, payload: TaskMigrateIn, db: DbSession, user: CurrentUser, ctx=require("task.assign"),
):
    """把任务显式迁移到方案当前的批准版本（连同还没建批次的子任务）。已建批次的任务不能迁移。"""
    return TaskService(db, ctx).migrate_version(task_id, payload.reason, user)


@router.post("/{task_id}/batches", status_code=201)
def create_child_batches(
    task_id: str, payload: TaskBatchesIn, db: DbSession, guard: IdempotencyGuard, user: CurrentUser,
    ctx=require("batch.create"),
):
    """为父任务下还没建批次的子任务各建一个批次（一个事务：任何一个建不成整体回滚）。"""
    from ...services.batch_service import BatchService

    body = payload.model_dump()
    guard.bind(ctx, body).required()
    replay = guard.replay()
    if replay is not None:
        return replay
    return guard.remember(BatchService(db, ctx).create_for_children(task_id, payload.priority, payload.note, user))


@router.post("/{task_id}/retest")
def retest_task(
    task_id: str, payload: TaskRetestIn, db: DbSession, guard: IdempotencyGuard, user: CurrentUser,
    ctx=require("task.create"),
):
    """补测：在父任务下新建补测子任务，补现有的样本短缺。"""
    body = payload.model_dump()
    guard.bind(ctx, body).required()
    replay = guard.replay()
    if replay is not None:
        return replay
    return guard.remember(TaskService(db, ctx).retest(task_id, body, user))


@router.post("/{task_id}/accept-shortfall")
def accept_shortfall(
    task_id: str, payload: ShortfallAcceptIn, db: DbSession, user: CurrentUser, ctx=require("task.cancel"),
):
    """按现有结果结束、不再补测：写明原因并电子签名。"""
    return TaskService(db, ctx).accept_shortfall(task_id, payload.model_dump(), user)
