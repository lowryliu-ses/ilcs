from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..core.clock import now
from .base import Base, uid


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    username: Mapped[str] = mapped_column(String, unique=True)
    display_name: Mapped[str] = mapped_column(String)
    role: Mapped[str] = mapped_column(String)
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
