"""结果分析页的批次清单。

结果的写入、审核与统计都在别处：设备回报走 `device_result_service`，人工录入与外部回传走
`analysis_service`（类型化结果、回传事件、审核），正式统计走 `report_service`。
早期固定三指标（放电比容量、面密度、保持率）的历史结果表与样本上的人工质量标记已在迁移 0055 退役。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..core.context import AccessContext
from ..repositories.batches import AnalysisTaskRepository, BatchRepository, SampleRepository
from ..repositories.metrics import ResultValueRepository
from ..repositories.recipes import RecipeRepository


class ResultService:
    def __init__(self, db: Session, ctx: AccessContext):
        self.db = db
        self.ctx = ctx
        self.samples = SampleRepository(db, ctx)
        self.tasks = AnalysisTaskRepository(db, ctx)
        self.batches = BatchRepository(db, ctx)
        self.recipes = RecipeRepository(db, ctx)
        self.values = ResultValueRepository(db, ctx)

    def has_typed_results(self, batch_id: str) -> bool:
        tasks = self.tasks.for_batch(batch_id)
        if not tasks:
            return False
        return bool(self.values.for_tasks([task.id for task in tasks]))

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
