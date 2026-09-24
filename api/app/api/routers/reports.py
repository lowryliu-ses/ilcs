from fastapi import APIRouter
from fastapi.responses import FileResponse

from ...schemas import DecisionIn, PublishIn, ReportCreateIn, ReportPatchIn
from ...services.report_service import ReportService
from ..deps import Ctx, CurrentUser, DbSession, IdempotencyGuard, Paging, require

router = APIRouter(prefix="/reports", tags=["report"])


@router.get("")
def list_reports(db: DbSession, ctx: Ctx, paging: Paging, state: str | None = None):
    items, total = ReportService(db, ctx).page(paging.offset, paging.page_size, state)
    return paging.wrap(items, total)


@router.get("/templates")
def report_templates(ctx: Ctx):
    """可选报告模板与各自包含的章节。取数只有一套，模板只决定章节与顺序。"""
    from ...domain.report_templates import catalog

    return catalog()


@router.get("/{version_id}")
def get_report(version_id: str, db: DbSession, ctx: Ctx):
    return ReportService(db, ctx).detail(version_id)


@router.get("/{version_id}/publish-check")
def publish_check(version_id: str, db: DbSession, ctx: Ctx):
    """发布前校验：引用结果是否全部通过审核。"""
    service = ReportService(db, ctx)
    version = service.versions.get(version_id)
    blockers = service.publish_blockers(version) if version else ["报告版本不存在"]
    return {"ok": not blockers, "blocked": [{"key": "result", "label": row} for row in blockers]}


@router.post("", status_code=201)
def create_report(
    payload: ReportCreateIn, db: DbSession, guard: IdempotencyGuard, user: CurrentUser,
    ctx=require("report.edit"),
):
    body = payload.model_dump()
    guard.bind(ctx, body).required()
    replay = guard.replay()
    if replay is not None:
        return replay
    return guard.remember(ReportService(db, ctx).create(body, user))


@router.patch("/{version_id}")
def update_report(
    version_id: str, payload: ReportPatchIn, db: DbSession, user: CurrentUser,
    ctx=require("report.edit"),
):
    return ReportService(db, ctx).update(version_id, payload.model_dump(exclude_unset=True), user)


@router.post("/{version_id}/submit")
def submit_report(version_id: str, db: DbSession, user: CurrentUser, ctx=require("report.submit")):
    return ReportService(db, ctx).submit(version_id, user)


@router.post("/{version_id}/approve")
def approve_report(
    version_id: str, payload: DecisionIn, db: DbSession, user: CurrentUser,
    ctx=require("report.approve"),
):
    """批准或退回。作者不能批准自己的报告。"""
    return ReportService(db, ctx).approve(version_id, payload.model_dump(), user)


@router.post("/{version_id}/publish")
def publish_report(
    version_id: str, payload: PublishIn, db: DbSession, guard: IdempotencyGuard,
    user: CurrentUser, ctx=require("report.publish"),
):
    """发布。校验引用结果全部审核通过，并固化结果版本、算法、模板、摘要与签名。"""
    body = payload.model_dump()
    guard.bind(ctx, body).required()
    replay = guard.replay()
    if replay is not None:
        return replay
    return guard.remember(ReportService(db, ctx).publish(version_id, body, user))


@router.post("/{version_id}/revisions", status_code=201)
def revise_report(version_id: str, db: DbSession, user: CurrentUser, ctx=require("report.edit")):
    """源结果修订后发布新报告。原 PDF 与引用不变，新版本标明替代关系。"""
    return ReportService(db, ctx).revise(version_id, user)


@router.get("/{version_id}/download")
def download_report(version_id: str, db: DbSession, user: CurrentUser, ctx: Ctx):
    record, path = ReportService(db, ctx).pdf_file(version_id, user)
    return FileResponse(
        path, media_type="application/pdf", filename=record.filename,
        headers={"X-Checksum-Sha256": record.checksum},
    )
