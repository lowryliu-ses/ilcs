"""配液模板与实验表格导入：模板决定怎么把一张配方表生成流程草稿与方案草稿；导入后仍走流程评审与方案审批。"""
from fastapi import APIRouter, File, UploadFile

from .spreadsheet import MAX_BYTES
from ...schemas import Versioned
from .schemas import (
    FormulationCheckIn, FormulationImportIn, FormulationPreviewIn, FormulationSubmitIn, FormulationTemplateIn,
    FormulationTemplatePatchIn,
)
from .service import FormulationService
from ...api.deps import Ctx, CurrentUser, DbSession, IdempotencyGuard, ServiceCtx, require

router = APIRouter(prefix="/formulation-templates", tags=["recipe"])
# 上游系统（AI 配方预测、实验设计平台）用服务身份提交配方表、查进度：X-Service-Source + X-Service-Secret，
# 授权范围 formulation_imports
runtime_router = APIRouter(prefix="/runtime/formulation-templates", tags=["runtime"])


@router.get("")
def list_templates(db: DbSession, ctx: Ctx, state: str | None = None):
    return FormulationService(db, ctx).list(state)


@router.post("", status_code=201)
def create_template(payload: FormulationTemplateIn, db: DbSession, user: CurrentUser, ctx=require("recipe.edit")):
    return FormulationService(db, ctx).create(payload.model_dump(), user)


@router.post("/table")
def read_table(file: UploadFile = File(...), ctx=require("recipe.edit")):
    """模板编辑器试算用：只把上传的 .xlsx / .csv 读成表格（与导入同一个读取器），不涉及任何模板、不写库。"""
    data = file.file.read(MAX_BYTES + 1)
    return FormulationService.read_table(file.filename or "", data)


@router.post("/check")
def check_template(payload: FormulationCheckIn, db: DbSession, ctx=require("recipe.edit")):
    """模板编辑器：按还没保存的配置列出问题；带了表格就按它试算（与导入同一套规则，不写库）。"""
    return FormulationService(db, ctx).check(payload.model_dump())


@router.get("/{template_id}")
def get_template(template_id: str, db: DbSession, ctx: Ctx):
    return FormulationService(db, ctx).get(template_id)


@router.patch("/{template_id}")
def update_template(
    template_id: str, payload: FormulationTemplatePatchIn, db: DbSession, user: CurrentUser, ctx=require("recipe.edit"),
):
    changes = payload.model_dump(exclude_unset=True, exclude={"row_version"})
    return FormulationService(db, ctx).update(template_id, changes, payload.row_version, user)


@router.post("/{template_id}/retire")
def retire_template(
    template_id: str, payload: Versioned, db: DbSession, user: CurrentUser, ctx=require("recipe.edit"),
):
    return FormulationService(db, ctx).retire(template_id, payload.row_version, user)


@router.post("/{template_id}/parse")
def parse_table(template_id: str, db: DbSession, file: UploadFile = File(...), ctx=require("recipe.edit")):
    """上传配方表（.xlsx / .csv）→ 解析 + 按实验参数缺省值生成预览。不写库，文件也不存档。"""
    # 多读一个字节就能判出超限，不必把大文件整个读进内存
    data = file.file.read(MAX_BYTES + 1)
    return FormulationService(db, ctx).parse(template_id, file.filename or "", data)


@router.post("/{template_id}/preview")
def preview_table(template_id: str, payload: FormulationPreviewIn, db: DbSession, ctx=require("recipe.edit")):
    """改了实验参数后重新生成预览。不写库。"""
    return FormulationService(db, ctx).preview(template_id, payload.filename, payload.table, payload.params)


@router.post("/{template_id}/import", status_code=201)
def import_table(
    template_id: str, payload: FormulationImportIn, db: DbSession, guard: IdempotencyGuard, user: CurrentUser,
    ctx=require("recipe.edit"), _plan=require("plan.edit"), _sample=require("sample.register"),
):
    """一次导入登记瓶子、建流程草稿（同结构沿用已有流程）与方案草稿；不提交、不批准。"""
    body = payload.model_dump()
    guard.bind(ctx, body).required()
    replay = guard.replay()
    if replay is not None:
        return replay
    return guard.remember(FormulationService(db, ctx).import_table(template_id, body, user))


@runtime_router.post("/{code}/imports", status_code=201)
def submit_table(code: str, payload: FormulationSubmitIn, db: DbSession, ctx: ServiceCtx):
    """提交一张配方表：与界面导入同一套校验与生成，登记瓶子、建流程草稿（同结构沿用）与方案草稿。

    之后的评审、批准、建批次、签名下发照旧由人做。按 `request_id` 去重：同一编号同一内容回放首次结果
    （`replayed: true`），内容不同 409；表格有问题 422，`detail.problems` 列出全部问题，什么都不建。
    """
    return FormulationService(db, ctx).submit(code, payload.model_dump())


@runtime_router.get("/{code}/imports/{request_id}")
def submission_progress(code: str, request_id: str, db: DbSession, ctx: ServiceCtx):
    """按请求编号查进度：流程与方案的审批状态、实验任务、批次、每瓶的检测结果（标明是否进正式统计）。
    只看得到本服务身份自己提交的。"""
    return FormulationService(db, ctx).progress(code, request_id)


@runtime_router.get("/{code}/imports/{request_id}/results/{result_id}/series")
def submission_result_series(code: str, request_id: str, result_id: str, db: DbSession, ctx: ServiceCtx):
    """取一条曲线结果（拉曼谱等）的完整数据点；只限这次提交生成的批次里的结果。"""
    return FormulationService(db, ctx).result_series(code, request_id, result_id)
