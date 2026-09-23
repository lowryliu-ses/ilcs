"""人员档案与资质。账号是登录凭证，人员档案是资质与执行身份的载体。"""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..core.clock import now
from .base import Base, uid


class Person(Base):
    __tablename__ = "people"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    code: Mapped[str] = mapped_column(String)
    name: Mapped[str] = mapped_column(String)
    lab_id: Mapped[str] = mapped_column(String, default="")
    # on_duty | leave | left：离岗与停用账号都阻止新的受控操作
    employment_state: Mapped[str] = mapped_column(String, default="on_duty")
    contact: Mapped[str] = mapped_column(String, default="")
    title: Mapped[str] = mapped_column(String, default="")
    user_id: Mapped[str] = mapped_column(String, default="")
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    row_version: Mapped[int] = mapped_column(default=1)
    __table_args__ = (UniqueConstraint("org_id", "code", name="uq_person_org_code"),)


class Qualification(Base):
    """资质记录。scope_kind 决定 scope_ref 的含义：能力 ID、SOP 版本 ID 或安全类别。"""

    __tablename__ = "qualifications"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    person_id: Mapped[str] = mapped_column(ForeignKey("people.id"), index=True)
    # capability | sop | safety
    scope_kind: Mapped[str] = mapped_column(String)
    scope_ref: Mapped[str] = mapped_column(String)
    label: Mapped[str] = mapped_column(String, default="")
    evidence_file_id: Mapped[str] = mapped_column(String, default="")
    granted_by: Mapped[str] = mapped_column(String, default="")
    effective_from: Mapped[datetime] = mapped_column(DateTime, default=now)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoke_reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
