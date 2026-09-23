from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.types import JSON
from sqlalchemy.orm import Mapped, mapped_column

from ..core.clock import now
from .base import Base, uid


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    username: Mapped[str] = mapped_column(String, unique=True)
    display_name: Mapped[str] = mapped_column(String)
    # 主角色：令牌、审计与界面显示用。权限按 `roles`（含主角色）计算
    role: Mapped[str] = mapped_column(String)
    roles: Mapped[list] = mapped_column(JSON, default=list)
    password_hash: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String, default="active")  # active | disabled
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    row_version: Mapped[int] = mapped_column(default=1)


class ESignature(Base):
    """签名 ticket：一次性、限时，且绑定动作 + 对象 ID + 对象版本 + 含义。

    为一个对象签发的票据不能用于另一个对象——object_ref / object_version
    在消费时必须与被操作对象一致。
    """

    __tablename__ = "esignatures"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    meaning: Mapped[str] = mapped_column(String)
    note: Mapped[str] = mapped_column(Text, default="")
    action: Mapped[str] = mapped_column(String, default="")
    object_ref: Mapped[str] = mapped_column(String, default="")
    object_version: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


def roles_of(user: User) -> list[str]:
    """账号的全部角色，主角色在前。旧数据没有 roles 时只有主角色。"""
    ordered = [user.role, *(user.roles or [])]
    return list(dict.fromkeys(role for role in ordered if role))


class RolePermissionSet(Base):
    """组织的角色权限矩阵。没有这一行的组织沿用出厂默认值；系统管理员不在矩阵里。"""

    __tablename__ = "role_permission_sets"
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), primary_key=True)
    matrix: Mapped[dict] = mapped_column(JSON, default=dict)
    row_version: Mapped[int] = mapped_column(default=1)
    updated_by: Mapped[str] = mapped_column(String, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)
