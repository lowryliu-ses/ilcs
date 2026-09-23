"""报告与发布快照。发布后固化结果版本、算法版本、模板版本、文件摘要与签名。"""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from ..core.clock import now
from .base import Base, uid


class Report(Base):
    __tablename__ = "reports"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    code: Mapped[str] = mapped_column(String)
    title: Mapped[str] = mapped_column(String)
    task_id: Mapped[str] = mapped_column(String, default="")
    plan_id: Mapped[str] = mapped_column(String, default="")
    batch_id: Mapped[str] = mapped_column(String, default="")
    created_by: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    __table_args__ = (UniqueConstraint("org_id", "code", name="uq_report_org_code"),)


class ReportVersion(Base):
    __tablename__ = "report_versions"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    report_id: Mapped[str] = mapped_column(ForeignKey("reports.id"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    # draft | review | approved | published | superseded
    state: Mapped[str] = mapped_column(String, default="draft")
    template_version: Mapped[str] = mapped_column(String, default="fixed-1.0")
    algorithm_version: Mapped[str] = mapped_column(String, default="")
    author_id: Mapped[str] = mapped_column(String, default="")
    approver_id: Mapped[str] = mapped_column(String, default="")
    content: Mapped[dict] = mapped_column(JSON, default=dict)
    publish_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    pdf_file_id: Mapped[str] = mapped_column(String, default="")
    signature_id: Mapped[str] = mapped_column(String, default="")
    supersedes_id: Mapped[str] = mapped_column(String, default="")
    reject_reason: Mapped[str] = mapped_column(Text, default="")
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    row_version: Mapped[int] = mapped_column(Integer, default=1)
    __table_args__ = (UniqueConstraint("report_id", "version", name="uq_report_version"),)
