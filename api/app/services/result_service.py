"""科学数据主线的兼容层。

新数据走 `analysis_service`（类型化结果、回传事件、审核）与 `report_service`（正式统计）。
这里保留两件事：
1. 历史批次的固定三指标读取，让旧批次页仍然打得开；
2. 过渡期的人工质量标记——它是「历史质量判定」，明确不等于审核通过。

M0 修掉的两处：回传不再自动把样品判成 valid，检测任务只完成与本次回传匹配的那一条。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..core.context import AccessContext
from ..core.errors import NotFound, PermissionDenied, StateConflict
from ..domain import simulation, statistics
from ..models import AnalysisTask, Result, Sample, User
from ..repositories.batches import (
    AnalysisTaskRepository, BatchRepository, ResultRepository, SampleRepository,
)
from ..repositories.metrics import MetricRepository, ResultValueRepository
from ..repositories.recipes import RecipeRepository
from .audit_service import AuditService
from .identity_service import IdentityService

QUALITY_LABEL = {"valid": "有效", "suspect": "可疑", "invalid": "无效"}
DEFAULT_METHOD = "电性能测试 EC-02 v2 · 0.1C 首次充放电"
LEGACY_NOTE = "历史质量标记，不等于结果审核通过"


class ResultService:
    def __init__(self, db: Session, ctx: AccessContext):
        self.db = db
        self.ctx = ctx
        self.samples = SampleRepository(db, ctx)
        self.results = ResultRepository(db, ctx)
        self.tasks = AnalysisTaskRepository(db, ctx)
        self.batches = BatchRepository(db, ctx)
        self.recipes = RecipeRepository(db, ctx)
        self.metrics = MetricRepository(db, ctx)
        self.values = ResultValueRepository(db, ctx)
        self.audit = AuditService(db, ctx)
        self.identity = IdentityService(db, ctx)

    # ---------- 写 ----------

    def create_task(
        self, sample: Sample, method: str = DEFAULT_METHOD, external_ref: str = "",
        required_metrics: list[str] | None = None,
    ) -> AnalysisTask:
        task = AnalysisTask(
            org_id=self.ctx.org_id, sample_id=sample.id,
            physical_sample_id=sample.physical_sample_id or sample.id, method=method,
            method_version="EC-02 v2", required_metrics=required_metrics or [],
            round_no=self.tasks.max_round(sample.physical_sample_id or sample.id) + 1,
            state="pending", external_ref=external_ref,
        )
        self.tasks.add(task)
        return task

    def ingest_legacy(
        self, sample_id: str, metrics: dict, raw_uri: str, parser_version: str, task_id: str = "",
        commit: bool = True,
    ) -> dict:
        """历史固定三指标写入路径。

        两处 M0 修正：
        - 不再把样品质量置成 valid。采集成功只说明数据到了，质量要人复核。
        - 只把 task_id 对应的那条检测任务标成已采集；以前是把该样品下所有任务一起标完，
          于是第二个检测任务会凭第一个的回传「凭空完成」。
        """
        sample = self.samples.get(sample_id)
        if sample is None:
            raise NotFound(f"样品 {sample_id} 不存在")
        result = Result(
            org_id=self.ctx.org_id,
            sample_id=sample.id,
            task_id=task_id,
            areal_density=metrics.get("areal_density"),
            discharge_capacity=metrics.get("discharge_capacity"),
            retention=metrics.get("retention"),
            raw_uri=raw_uri,
            parser_version=parser_version,
        )
        self.results.add(result)

        matched = self.tasks.get(task_id) if task_id else None
        if matched is not None and matched.sample_id == sample.id:
            matched.state = "collected"
        elif task_id:
            raise StateConflict(
                f"检测任务 {task_id} 与样品 {sample.id} 不匹配，已拒绝入账",
                code="task_sample_mismatch",
            )

        if sample.state != "failed":
            sample.state = "done"
        # 质量与审核状态一律不在这里动
        self.audit.record(
            None, "检测结果回传", sample.id, before="待检测", after="已采集",
            detail=(
                f"{raw_uri or '无原始文件'}；解析版本 {parser_version or '—'}；"
                f"质量未判定、审核待复核，不进入正式统计"
            ),
        )
        if commit:
            self.db.commit()
        return {
            "sample_id": sample.id,
            "result_id": result.id,
            "quality": sample.quality,
            "task_id": matched.id if matched else "",
            "note": "采集完成不等于质量有效或审核通过",
        }

    def flag(self, sample_id: str, quality: str, note: str, user: User) -> dict:
        """过渡期的人工质量标记。

        这个标记是「历史质量判定」，不写审核状态、不参与正式统计的纳入判断。
        正式质量判定走 `/result-values/{id}/review`。
        """
        if not self.ctx.has("result.flag"):
            raise PermissionDenied("当前角色不能标记结果质量")
        if quality not in QUALITY_LABEL:
            raise StateConflict("质量标记无效")
        sample = self.samples.get(sample_id)
        if sample is None:
            raise NotFound("样品不存在")
        before = QUALITY_LABEL.get(sample.quality or "", "未标记")
        sample.quality = quality
        sample.flag_note = note
        self.audit.record(
            user, "标记样品质量（历史标记）", sample.id, before=before, after=QUALITY_LABEL[quality],
            detail=f"{note}；{LEGACY_NOTE}",
        )
        self.db.commit()
        return {
            "id": sample.id,
            "quality": sample.quality,
            "flag_note": sample.flag_note,
            "legacy_note": LEGACY_NOTE,
        }

    # ---------- 读 ----------

    def _sample_dicts(self, batch_id: str) -> list[dict]:
        samples = self.samples.for_batch(batch_id)
        results = self.results.for_samples([s.id for s in samples])
        rows = []
        for sample in samples:
            result = results.get(sample.id)
            rows.append(
                {
                    "id": sample.id, "well": sample.well, "repeat": sample.repeat,
                    "state": sample.state, "quality": sample.quality,
                    "flag_note": sample.flag_note, "levels": sample.levels,
                    "condition_group": sample.condition_group,
                    "condition_label": sample.condition_label,
                    "is_control": sample.is_control,
                    "raw_uri": result.raw_uri if result else "",
                    "metrics": {
                        "discharge_capacity": result.discharge_capacity if result else None,
                        "areal_density": result.areal_density if result else None,
                        "retention": result.retention if result else None,
                    },
                }
            )
        return rows

    def legacy_analysis(self, batch_id: str) -> dict:
        """历史固定三指标分析。

        它按样品上的历史质量标记聚合，因此结果里带 `legacy` 标注：
        这里的「有效」是旧标记，不是新模型下的审核通过 + 质量有效。
        """
        batch = self.batches.get(batch_id)
        if batch is None:
            raise NotFound("批次不存在")
        samples = self._sample_dicts(batch_id)
        recipe = self.recipes.get(batch.recipe_id)
        golden_id = recipe.golden_batch_id if recipe else ""
        golden_samples = self._sample_dicts(golden_id) if golden_id and golden_id != batch_id else []
        factors = (batch.plan_snapshot or {}).get("factors") or []
        groups = statistics.group_statistics(samples, golden_samples)
        return {
            "batch_id": batch.id,
            "legacy": True,
            "legacy_note": LEGACY_NOTE,
            "recipe_id": batch.recipe_id,
            "recipe_name": batch.recipe_snapshot.get("name"),
            "plan_id": batch.plan_id,
            "plan_name": (batch.plan_snapshot or {}).get("name"),
            "state": batch.state,
            "golden_batch_id": golden_id,
            "is_golden": golden_id == batch.id,
            "groups": groups,
            "effects": statistics.factor_effects(samples, factors),
            "summary": statistics.summary(groups, (batch.plan_snapshot or {}).get("repeats")),
            "samples": samples,
        }

    def has_typed_results(self, batch_id: str) -> bool:
        tasks = self.tasks.for_batch(batch_id)
        if not tasks:
            return False
        return bool(self.values.for_tasks([task.id for task in tasks]))

    def raw_curve_rows(self, sample_id: str, user: User) -> tuple[str, list[list]]:
        """历史模拟曲线下载。

        这是模拟数据，不是真实采集原件，所以文件名与表头都带 simulated 标记。
        真实原始文件走 `/files/{id}/download`。
        """
        sample = self.samples.get(sample_id)
        if sample is None:
            raise NotFound("样品不存在")
        result = self.results.for_samples([sample.id]).get(sample.id)
        if not result or result.discharge_capacity is None:
            raise StateConflict("该样品还没有回传结果，没有原始曲线")
        rows: list[list] = [
            ["# 数据来源", "模拟曲线（simulation），非真实采集原件"],
            ["# 原始引用", result.raw_uri or "—"],
            ["step", "capacity_mAh_g", "voltage_V"],
        ]
        rows.extend(
            [list(row) for row in simulation.raw_curve(sample.id, float(result.discharge_capacity))]
        )
        self.audit.record(
            user, "下载历史模拟曲线", sample.id,
            detail=f"{result.raw_uri or '模拟曲线'}；解析版本 {result.parser_version or '—'}；标记为模拟数据",
        )
        self.db.commit()
        return f"{sample.id}-raw-simulated.csv", rows

    def batches_with_results(self) -> list[dict]:
        rows = []
        for batch in self.batches.list():
            samples = self.samples.for_batch(batch.id)
            done = [s for s in samples if s.state == "done"]
            typed = self.has_typed_results(batch.id)
            if not done and not typed:
                continue
            recipe = self.recipes.get(batch.recipe_id)
            tasks = self.tasks.for_batch(batch.id)
            values = self.values.for_tasks([task.id for task in tasks])
            live = [row for row in values if not row.superseded_by_id]
            rows.append(
                {
                    "batch_id": batch.id,
                    "recipe_id": batch.recipe_id,
                    "recipe_name": batch.recipe_snapshot.get("name"),
                    "state": batch.state,
                    "plan_id": batch.plan_id,
                    "sample_done": len(done),
                    "sample_count": len(samples),
                    "typed_results": typed,
                    "result_count": len(live),
                    "pending_review": len([row for row in live if row.review_state == "pending"]),
                    "official_count": len(
                        [
                            row for row in live
                            if row.review_state == "approved" and row.quality == "valid"
                        ]
                    ),
                    "is_golden": bool(recipe and recipe.golden_batch_id == batch.id),
                }
            )
        return rows
