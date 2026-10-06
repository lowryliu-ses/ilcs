from __future__ import annotations

from ..models import AuditEvent, Report, ReportTemplate, ReportVersion
from .base import ScopedRepository


class ReportRepository(ScopedRepository[Report]):
    model = Report

    def list(self) -> list[Report]:
        return list(self.query().order_by(Report.created_at.desc()).all())

    def by_code(self, code: str) -> Report | None:
        return self.query().filter(Report.code == code).first()

    def next_code(self) -> str:
        """组织内用过的最大编号 + 1。按行数算的话，报告被删（测试环境的强制删除）以后会撞上还在的编号（唯一约束，
        建报告失败），或重发删掉的编号——审计里同一个编号就指了两份不同的报告。报告删了，审计里「生成报告草稿」的
        说明仍以编号开头，一并算进来：编号不回头。"""
        used = [row[0] for row in self.query().with_entities(Report.code).all()]
        drafts = self.db.query(AuditEvent.detail).filter(AuditEvent.action == "生成报告草稿")
        if self.ctx is not None:
            drafts = drafts.filter(AuditEvent.org_id == self.ctx.org_id)
        used += [str(row[0] or "").split("；", 1)[0] for row in drafts.all()]
        numbers = [int(code[4:]) for code in used if code.startswith("RPT-") and code[4:].isdigit()]
        return f"RPT-{max(numbers, default=0) + 1:05d}"


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


class ReportTemplateRepository(ScopedRepository[ReportTemplate]):
    model = ReportTemplate

    def list(self) -> list[ReportTemplate]:
        return list(self.query().order_by(ReportTemplate.key, ReportTemplate.version).all())

    def for_key(self, key: str) -> list[ReportTemplate]:
        return list(self.query().filter(ReportTemplate.key == key).order_by(ReportTemplate.version).all())
