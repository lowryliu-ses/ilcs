"""配液模板与实验表格导入：模板决定怎么把一张配方表生成流程草稿与方案草稿；导入后仍走流程评审与方案审批。"""
from fastapi import APIRouter, File, UploadFile

from ...core.spreadsheet import MAX_BYTES
from ...schemas import (
    FormulationCheckIn, FormulationImportIn, FormulationPreviewIn, FormulationTemplateIn, FormulationTemplatePatchIn,
    Versioned,
)
from ...services.formulation_service import FormulationService
from ..deps import Ctx, CurrentUser, DbSession, IdempotencyGuard, require

router = APIRouter(prefix="/formulation-templates", tags=["recipe"])


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
