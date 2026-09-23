from __future__ import annotations

from ..models import Report, ReportVersion
from .base import ScopedRepository


class ReportRepository(ScopedRepository[Report]):
    model = Report

    def list(self) -> list[Report]:
        return list(self.query().order_by(Report.created_at.desc()).all())

    def by_code(self, code: str) -> Report | None:
        return self.query().filter(Report.code == code).first()

    def next_code(self) -> str:
        count = self.db.query(Report).count()
        return f"RPT-{count + 1:05d}"


class ReportVersionRepository(ScopedRepository[ReportVersion]):
    model = ReportVersion

    def for_report(self, report_id: str) -> list[ReportVersion]:
        return list(
            self.query()
            .filter(ReportVersion.report_id == report_id)
            .order_by(ReportVersion.version)
            .all()
        )

    def latest(self, report_id: str) -> ReportVersion | None:
        return (
            self.query()
            .filter(ReportVersion.report_id == report_id)
            .order_by(ReportVersion.version.desc())
            .first()
        )

    def published(self, report_id: str) -> ReportVersion | None:
        return (
            self.query()
            .filter(ReportVersion.report_id == report_id, ReportVersion.state == "published")
            .order_by(ReportVersion.version.desc())
            .first()
        )

    def page(self, offset: int, limit: int, state: str | None = None):
        query = self.query()
        if state:
            query = query.filter(ReportVersion.state == state)
        total = query.count()
        rows = query.order_by(ReportVersion.created_at.desc()).offset(offset).limit(limit).all()
        return list(rows), total
