import csv
import io

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from ...services.report_service import ReportService
from ...services.result_service import ResultService
from ..deps import Ctx, DbSession

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


# 固定路径要声明在 /results/{batch_id} 之前：否则「compare」会被当成批次号截走
@router.get("/results/compare")
def compare(db: DbSession, ctx: Ctx, batch_ids: str, metric_id: str, official: bool = True):
    """跨批次比较。单位或方法版本不可比时返回 comparable=false 与原因。"""
    return ReportService(db, ctx).compare(
        [b for b in batch_ids.split(",") if b], metric_id, official
    )


@router.get("/results/{batch_id}")
def analysis(
    db: DbSession, ctx: Ctx, batch_id: str, official: bool = True, metric_ids: str = ""
):
    """结果分析。

    `official=true` 是正式范围：审核通过 + 质量有效 + 选定版本。
    `official=false` 是探索性范围，响应里的 `scope_label` 会说清楚，导出也会标注。
    还没有回传结果的批次照样走这个视图，`metrics` 为空。
    """
    selected = [m for m in metric_ids.split(",") if m] or None
    return ReportService(db, ctx).analysis_view(batch_id, selected, official)


@router.get("/results/{batch_id}/series")
def series(db: DbSession, ctx: Ctx, batch_id: str, metric_id: str, official: bool = True):
    """曲线叠加：这个批次里选定曲线指标的样本曲线（抽稀），纳入口径与数值统计相同，没纳入的列出原因。"""
    return ReportService(db, ctx).batch_series_view(batch_id, metric_id, official)


@router.get("/results/{batch_id}/export")
def export(db: DbSession, ctx: Ctx, batch_id: str, official: bool = True):
    scope = "official" if official else "exploratory"
    return csv_response(
        f"{batch_id}-results-{scope}.csv", ReportService(db, ctx).export_rows(batch_id, official)
    )
