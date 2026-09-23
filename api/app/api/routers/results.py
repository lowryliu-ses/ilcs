import csv
import io

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from ...schemas import FlagIn
from ...services.report_service import ReportService
from ...services.result_service import ResultService
from ..deps import Ctx, CurrentUser, DbSession, require

router = APIRouter(tags=["result"])


def csv_response(filename: str, rows: list[list]) -> StreamingResponse:
    buffer = io.StringIO()
    csv.writer(buffer).writerows(rows)
    return StreamingResponse(
        iter(["﻿" + buffer.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/results")
def list_result_batches(db: DbSession, ctx: Ctx):
    return ResultService(db, ctx).batches_with_results()


@router.get("/results/{batch_id}")
def analysis(
    db: DbSession, ctx: Ctx, batch_id: str, official: bool = True, metric_ids: str = ""
):
    """结果分析。

    `official=true` 是正式范围：审核通过 + 质量有效 + 选定版本。
    `official=false` 是探索性范围，响应里的 `scope_label` 会说清楚，导出也会标注。
    没有类型化结果的历史批次回落到旧的固定三指标视图，并带 legacy 标记。
    """
    service = ResultService(db, ctx)
    if not service.has_typed_results(batch_id):
        return service.legacy_analysis(batch_id)
    selected = [m for m in metric_ids.split(",") if m] or None
    return ReportService(db, ctx).analysis_view(batch_id, selected, official)


@router.get("/results/{batch_id}/export")
def export(db: DbSession, ctx: Ctx, batch_id: str, official: bool = True):
    scope = "official" if official else "exploratory"
    return csv_response(
        f"{batch_id}-results-{scope}.csv", ReportService(db, ctx).export_rows(batch_id, official)
    )


@router.get("/results/compare")
def compare(db: DbSession, ctx: Ctx, batch_ids: str, metric_id: str, official: bool = True):
    """跨批次比较。单位或方法版本不可比时返回 comparable=false 与原因。"""
    return ReportService(db, ctx).compare(
        [b for b in batch_ids.split(",") if b], metric_id, official
    )


@router.get("/samples/{sample_id}/raw")
def download_raw(sample_id: str, db: DbSession, ctx: Ctx, user: CurrentUser):
    """历史模拟曲线下载。标记为 simulation，不冒充真实采集原件。"""
    filename, rows = ResultService(db, ctx).raw_curve_rows(sample_id, user)
    return csv_response(filename, rows)


@router.post("/samples/{sample_id}/flag")
def flag_sample(
    sample_id: str, payload: FlagIn, db: DbSession, user: CurrentUser, ctx=require("result.flag")
):
    """过渡期的人工质量标记。它是历史质量判定，不等于结果审核通过。"""
    return ResultService(db, ctx).flag(sample_id, payload.quality, payload.note, user)
