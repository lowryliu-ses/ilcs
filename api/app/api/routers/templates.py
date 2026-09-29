"""设备接入模板：一类设备怎么接。起草归工位维护，发布要另一个人签名；导出的文件就是设备模块的 profile.json。"""
from fastapi import APIRouter
from fastapi.responses import Response
import json

from ...schemas import DeviceTemplateImportIn, DeviceTemplateIn, DeviceTemplatePatchIn, DeviceTemplateReleaseIn, Versioned
from ...services.template_service import TemplateService
from ..deps import CurrentUser, DbSession, require, require_any

router = APIRouter(prefix="/device-templates", tags=["resource"])


# 模板里是设备的点表、命令与内部地址示例：只给维护工位的人（起草）与发布模板的人（审了才能发布）看
READ = ("station.edit", "template.release")


@router.get("")
def list_templates(db: DbSession, ctx=require_any(*READ), state: str | None = None, driver: str | None = None):
    return TemplateService(db, ctx).list(state, driver)


@router.post("", status_code=201)
def create_template(payload: DeviceTemplateIn, db: DbSession, user: CurrentUser, ctx=require("station.edit")):
    return TemplateService(db, ctx).create(payload.model_dump(), user)


@router.post("/import", status_code=201)
def import_template(payload: DeviceTemplateImportIn, db: DbSession, user: CurrentUser, ctx=require("station.edit")):
    """导入一律成草稿：别的部署发布过，不等于本部署审过。"""
    return TemplateService(db, ctx).import_file(payload.document, payload.filename, user)


@router.get("/{template_id}")
def get_template(template_id: str, db: DbSession, ctx=require_any(*READ)):
    return TemplateService(db, ctx).get(template_id)


@router.patch("/{template_id}")
def update_template(
    template_id: str, payload: DeviceTemplatePatchIn, db: DbSession, user: CurrentUser, ctx=require("station.edit"),
):
    changes = payload.model_dump(exclude_unset=True, exclude={"row_version"})
    return TemplateService(db, ctx).update(template_id, changes, payload.row_version, user)


@router.post("/{template_id}/release")
def release_template(
    template_id: str, payload: DeviceTemplateReleaseIn, db: DbSession, user: CurrentUser, ctx=require("template.release"),
):
    return TemplateService(db, ctx).release(template_id, payload.row_version, payload.signature_id, user)


@router.post("/{template_id}/revise", status_code=201)
def revise_template(template_id: str, db: DbSession, user: CurrentUser, ctx=require("station.edit")):
    return TemplateService(db, ctx).revise(template_id, user)


@router.post("/{template_id}/retire")
def retire_template(
    template_id: str, payload: Versioned, db: DbSession, user: CurrentUser, ctx=require("template.release"),
):
    return TemplateService(db, ctx).retire(template_id, payload.row_version, user)


@router.delete("/{template_id}")
def delete_template(template_id: str, db: DbSession, user: CurrentUser, ctx=require("station.edit")):
    return TemplateService(db, ctx).delete(template_id, user)


@router.get("/{template_id}/export")
def export_template(template_id: str, db: DbSession, ctx=require_any(*READ)):
    document = TemplateService(db, ctx).export(template_id)
    return Response(
        content=json.dumps(document, ensure_ascii=False, indent=2) + "\n", media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{document["code"]}-r{document["revision"]}.json"'},
    )
