"""SOP 与受控版本。已发布内容不可原位编辑，修订生成新版本。"""
from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from ..core.clock import now
from .base import Base, uid


class Sop(Base):
    __tablename__ = "sops"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    code: Mapped[str] = mapped_column(String)
    title: Mapped[str] = mapped_column(String)
    # 受控文件的分类与负责人：属于文件本身，不随版本变
    category: Mapped[str] = mapped_column(String, default="")
    owner_id: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    __table_args__ = (UniqueConstraint("org_id", "code", name="uq_sop_org_code"),)


class SopVersion(Base):
    __tablename__ = "sop_versions"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    sop_id: Mapped[str] = mapped_column(ForeignKey("sops.id"), index=True)
    version: Mapped[str] = mapped_column(String)
    # draft | review | published | retired
    state: Mapped[str] = mapped_column(String, default="draft")
    file_id: Mapped[str] = mapped_column(String, default="")
    file_checksum: Mapped[str] = mapped_column(String, default="")
    capability_scope: Mapped[list] = mapped_column(JSON, default=list)
    sample_types: Mapped[list] = mapped_column(JSON, default=list)
    requires_training_ack: Mapped[bool] = mapped_column(default=False)
    effective_from: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 失效时间：到点后不再是生效版本。被同编号新版本取代时取新版本的生效时间
    effective_to: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 下次复审日期：过期只提醒，不自动失效
    review_due: Mapped[date | None] = mapped_column(Date, nullable=True)
    superseded_by: Mapped[str] = mapped_column(String, default="")
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    author_id: Mapped[str] = mapped_column(String, default="")
    approver_id: Mapped[str] = mapped_column(String, default="")
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reject_reason: Mapped[str] = mapped_column(Text, default="")
    # 数字 SOP：结构化步骤 [{title, kind, capability, params, duration_min, instructions, checks}]
    steps: Mapped[list] = mapped_column(JSON, default=list)
    # 从哪个历史版本恢复而来（恢复产生新版本，不改写历史版本）
    restored_from: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    row_version: Mapped[int] = mapped_column(Integer, default=1)
    __table_args__ = (UniqueConstraint("sop_id", "version", name="uq_sop_version"),)


class SopAck(Base):
    """阅读确认。SOP 要求培训确认时，人员须有对应版本的确认记录。"""

    __tablename__ = "sop_acks"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    sop_version_id: Mapped[str] = mapped_column(ForeignKey("sop_versions.id"), index=True)
    person_id: Mapped[str] = mapped_column(String)
    user_id: Mapped[str] = mapped_column(String, default="")
    acked_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    __table_args__ = (UniqueConstraint("sop_version_id", "person_id", name="uq_sop_ack"),)
