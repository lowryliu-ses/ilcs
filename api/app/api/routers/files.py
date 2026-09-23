from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import FileResponse

from ...schemas import FileAttachIn
from ...services.file_service import FileService
from ..deps import Ctx, CurrentUser, DbSession, Paging, require

router = APIRouter(prefix="/files", tags=["file"])


@router.get("")
def list_files(db: DbSession, ctx: Ctx, paging: Paging, ref_type: str = ""):
    items, total = FileService(db, ctx).page(paging.offset, paging.page_size, ref_type)
    return paging.wrap(items, total)


@router.get("/for/{ref_type}/{ref_id}")
def files_for(ref_type: str, ref_id: str, db: DbSession, ctx: Ctx):
    return FileService(db, ctx).list_for(ref_type, ref_id)


@router.post("", status_code=201)
def upload_file(
    db: DbSession,
    user: CurrentUser,
    file: UploadFile = File(...),
    ref_type: str = Form(""),
    ref_id: str = Form(""),
    note: str = Form(""),
    ctx=require("file.upload"),
):
    """真实文件上传。存储键由服务端生成，调用方不能拼物理路径。"""
    return FileService(db, ctx).upload(
        file.filename or "unnamed", file.content_type or "application/octet-stream",
        file.file, user, ref_type, ref_id, note,
    )


@router.post("/{file_id}/attach")
def attach_file(
    file_id: str, payload: FileAttachIn, db: DbSession, user: CurrentUser,
    ctx=require("file.upload"),
):
    return FileService(db, ctx).attach(file_id, payload.ref_type, payload.ref_id, user)


@router.get("/{file_id}/download")
def download_file(file_id: str, db: DbSession, user: CurrentUser, ctx: Ctx):
    """授权下载。摘要不一致或原件缺失都明确报错，不提供替代内容。"""
    record, path = FileService(db, ctx).open_for_download(file_id, user)
    return FileResponse(
        path, media_type=record.media_type, filename=record.filename,
        headers={"X-Checksum-Sha256": record.checksum},
    )
