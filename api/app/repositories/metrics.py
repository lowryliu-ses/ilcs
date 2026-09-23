from __future__ import annotations

from sqlalchemy import or_

from ..models import IngestEvent, MetricDefinition, ResultReview, ResultValue
from .base import ScopedRepository


class MetricRepository(ScopedRepository[MetricDefinition]):
    model = MetricDefinition

    def list(self) -> list[MetricDefinition]:
        return list(
            self.query().order_by(MetricDefinition.code, MetricDefinition.version).all()
        )

    def active(self) -> list[MetricDefinition]:
        return list(self.query().filter(MetricDefinition.state == "active").all())

    def by_code(self, code: str, version: str | None = None) -> MetricDefinition | None:
        query = self.query().filter(MetricDefinition.code == code)
        if version:
            query = query.filter(MetricDefinition.version == version)
        return query.order_by(MetricDefinition.version.desc()).first()

    def many(self, ids: list[str]) -> dict[str, MetricDefinition]:
        if not ids:
            return {}
        rows = self.query().filter(MetricDefinition.id.in_(list(set(ids)))).all()
        return {row.id: row for row in rows}

    def referenced(self, metric_id: str) -> int:
        return self.db.query(ResultValue).filter(
            ResultValue.metric_definition_id == metric_id
        ).count()


class IngestEventRepository(ScopedRepository[IngestEvent]):
    model = IngestEvent

    def find(self, source: str, analysis_task_id: str, event_id: str) -> IngestEvent | None:
        return (
            self.query()
            .filter(
                IngestEvent.source == source,
                IngestEvent.analysis_task_id == analysis_task_id,
                IngestEvent.event_id == event_id,
            )
            .first()
        )

    def for_task(self, analysis_task_id: str) -> list[IngestEvent]:
        return list(
            self.query()
            .filter(IngestEvent.analysis_task_id == analysis_task_id)
            .order_by(IngestEvent.received_at)
            .all()
        )


class ResultValueRepository(ScopedRepository[ResultValue]):
    model = ResultValue

    def for_task(self, analysis_task_id: str) -> list[ResultValue]:
        return list(
            self.query()
            .filter(ResultValue.analysis_task_id == analysis_task_id)
            .order_by(ResultValue.metric_definition_id, ResultValue.result_version)
            .all()
        )

    def current_for_task(self, analysis_task_id: str) -> dict[str, ResultValue]:
        """每个指标的当前版本 = 最高版本且未被取代。"""
        current: dict[str, ResultValue] = {}
        for row in self.for_task(analysis_task_id):
            if row.superseded_by_id:
                continue
            existing = current.get(row.metric_definition_id)
            if not existing or row.result_version > existing.result_version:
                current[row.metric_definition_id] = row
        return current

    def find_version(
        self, analysis_task_id: str, metric_id: str, version: int
    ) -> ResultValue | None:
        return (
            self.query()
            .filter(
                ResultValue.analysis_task_id == analysis_task_id,
                ResultValue.metric_definition_id == metric_id,
                ResultValue.result_version == version,
            )
            .first()
        )

    def max_version(self, analysis_task_id: str, metric_id: str) -> int:
        rows = (
            self.query()
            .filter(
                ResultValue.analysis_task_id == analysis_task_id,
                ResultValue.metric_definition_id == metric_id,
            )
            .all()
        )
        return max((row.result_version for row in rows), default=0)

    def for_tasks(self, task_ids: list[str]) -> list[ResultValue]:
        if not task_ids:
            return []
        return list(
            self.query().filter(ResultValue.analysis_task_id.in_(list(set(task_ids)))).all()
        )

    def pending_review(self, limit: int = 200) -> list[ResultValue]:
        return list(
            self.query()
            .filter(ResultValue.review_state == "pending", ResultValue.superseded_by_id == "")
            .order_by(ResultValue.created_at)
            .limit(limit)
            .all()
        )

    def page(self, offset: int, limit: int, review_state: str = "", quality: str = ""):
        query = self.query().filter(ResultValue.superseded_by_id == "")
        if review_state:
            query = query.filter(ResultValue.review_state == review_state)
        if quality:
            query = query.filter(ResultValue.quality == quality)
        total = query.count()
        rows = query.order_by(ResultValue.created_at.desc()).offset(offset).limit(limit).all()
        return list(rows), total


class ResultReviewRepository(ScopedRepository[ResultReview]):
    model = ResultReview

    def for_value(self, result_value_id: str) -> list[ResultReview]:
        return list(
            self.query()
            .filter(ResultReview.result_value_id == result_value_id)
            .order_by(ResultReview.decided_at)
            .all()
        )
