"""报告模板：内置模板只读；组织自己的模板起草 → 发布（发布人不能是起草人）→ 修订出新版本 / 退役。"""
from fastapi import APIRouter

from ...schemas import ReportTemplateIn, ReportTemplatePatchIn, Versioned
from ...services.report_template_service import ReportTemplateService
from ..deps import Ctx, CurrentUser, DbSession, require

router = APIRouter(prefix="/report-templates", tags=["report"])


@router.get("")
def list_report_templates(db: DbSession, ctx: Ctx):
    """内置模板、组织模板的全部版本与可选的内置章节。"""
    return ReportTemplateService(db, ctx).listing()


@router.post("", status_code=201)
def create_report_template(payload: ReportTemplateIn, db: DbSession, user: CurrentUser, ctx=require("report.edit")):
    return ReportTemplateService(db, ctx).create(payload.model_dump(), user)


@router.patch("/{template_id}")
def update_report_template(
    template_id: str, payload: ReportTemplatePatchIn, db: DbSession, user: CurrentUser, ctx=require("report.edit"),
):
    changes = payload.model_dump(exclude_unset=True, exclude={"row_version"})
    return ReportTemplateService(db, ctx).update(template_id, changes, payload.row_version, user)


@router.post("/{template_id}/release")
def release_report_template(
    template_id: str, payload: Versioned, db: DbSession, user: CurrentUser, ctx=require("report.approve"),
):
    return ReportTemplateService(db, ctx).release(template_id, payload.row_version, user)


@router.post("/{template_id}/revise", status_code=201)
def revise_report_template(template_id: str, db: DbSession, user: CurrentUser, ctx=require("report.edit")):
    return ReportTemplateService(db, ctx).revise(template_id, user)


@router.post("/{template_id}/retire")
def retire_report_template(
    template_id: str, payload: Versioned, db: DbSession, user: CurrentUser, ctx=require("report.approve"),
):
    return ReportTemplateService(db, ctx).retire(template_id, payload.row_version, user)
