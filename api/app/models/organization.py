"""组织、实验室、项目与成员关系，以及集成用的服务身份。

访问范围的唯一权威在这里：业务对象带 org_id，服务端按当前成员关系裁决，
不看请求体里的 organization_id。
"""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from ..core.clock import now
from .base import Base, uid


class Organization(Base):
    __tablename__ = "organizations"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    code: Mapped[str] = mapped_column(String, unique=True)
    name: Mapped[str] = mapped_column(String)
    # 有效期与校准到期都按实验室时区解释，不依赖服务器本地时区
    timezone: Mapped[str] = mapped_column(String, default="Asia/Shanghai")
    state: Mapped[str] = mapped_column(String, default="active")  # active | suspended
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Lab(Base):
    __tablename__ = "labs"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    name: Mapped[str] = mapped_column(String)
    timezone: Mapped[str] = mapped_column(String, default="")
    state: Mapped[str] = mapped_column(String, default="active")


class Project(Base):
    """受限项目。restricted=True 时访问由项目成员关系控制，样本/结果/报告继承它。"""

    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    code: Mapped[str] = mapped_column(String)
    name: Mapped[str] = mapped_column(String)
    restricted: Mapped[bool] = mapped_column(Boolean, default=False)
    state: Mapped[str] = mapped_column(String, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    __table_args__ = (UniqueConstraint("org_id", "code", name="uq_project_org_code"),)


class Membership(Base):
    """组织成员关系。撤销后新请求立即失去该组织的访问范围。"""

    __tablename__ = "memberships"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    state: Mapped[str] = mapped_column(String, default="active")  # active | revoked
    default_lab_id: Mapped[str] = mapped_column(String, default="")
    granted_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    __table_args__ = (UniqueConstraint("org_id", "user_id", name="uq_membership_org_user"),)


class ProjectMember(Base):
    __tablename__ = "project_members"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    state: Mapped[str] = mapped_column(String, default="active")
    granted_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    __table_args__ = (UniqueConstraint("project_id", "user_id", name="uq_project_member"),)


class ServiceIdentity(Base):
    """集成服务凭据。回传来源由认证确定，不由请求体里的序列号确定。

    `scopes` 形如 {"stations": ["ST-..."], "analysis_tasks": "assigned"}；
    只保存凭据摘要，日志不记录原文。
    """

    __tablename__ = "service_identities"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    # 认证请求只有 source + secret，没有额外组织提示，因此 source 必须全局唯一。
    source: Mapped[str] = mapped_column(String, unique=True)
    name: Mapped[str] = mapped_column(String, default="")
    secret_hash: Mapped[str] = mapped_column(String)
    scopes: Mapped[dict] = mapped_column(JSON, default=dict)
    state: Mapped[str] = mapped_column(String, default="active")  # active | disabled
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    row_version: Mapped[int] = mapped_column(default=1)
    __table_args__ = (UniqueConstraint("org_id", "source", name="uq_service_identity_source"),)
