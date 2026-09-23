from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from ..core.clock import now
from .base import Base, uid


class Alarm(Base):
    """确认 ≠ 关闭。condition_active 由设备侧事件置位，关闭要求它已复位。"""

    __tablename__ = "alarms"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    org_id: Mapped[str] = mapped_column(String, default="", index=True)
    severity: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String, default="active")  # active|acked|shelved|closed
    condition_active: Mapped[bool] = mapped_column(Boolean, default=True)
    owner: Mapped[str] = mapped_column(String, default="")
    source_type: Mapped[str] = mapped_column(String)  # station|batch|material|person|asset
    source_id: Mapped[str] = mapped_column(String)
    message: Mapped[str] = mapped_column(Text)
    response: Mapped[str] = mapped_column(Text, default="")
    shelved_until: Mapped[str] = mapped_column(String, default="")
    raised_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    # device：设备侧条件，只能由设备上报恢复；system：软件判定生成，设备不知道它的存在，
    # 由操作员写明原因并签名后清除条件
    origin: Mapped[str] = mapped_column(String, default="device")
    # 同一异常条件的去重键（如 station:ST-05:heartbeat_stale）；条件持续期间不重复报
    condition_key: Mapped[str] = mapped_column(String, default="", index=True)


class AuditEvent(Base):
    """仅追加。生产库上对应用角色 REVOKE UPDATE, DELETE。"""

    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(String, default="", index=True)
    time: Mapped[datetime] = mapped_column(DateTime, default=now)
    user: Mapped[str] = mapped_column(String)
    user_id: Mapped[str] = mapped_column(String, default="")
    role: Mapped[str] = mapped_column(String)
    action: Mapped[str] = mapped_column(String)
    target: Mapped[str] = mapped_column(String, index=True)
    object_version: Mapped[int] = mapped_column(Integer, default=0)
    sign: Mapped[bool] = mapped_column(Boolean, default=False)
    meaning: Mapped[str] = mapped_column(String, default="")
    before: Mapped[str] = mapped_column(String, default="")
    after: Mapped[str] = mapped_column(String, default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    signature_id: Mapped[str] = mapped_column(String, default="")
    command_id: Mapped[str] = mapped_column(String, default="")
    checkpoint_id: Mapped[str] = mapped_column(String, default="")
    request_id: Mapped[str] = mapped_column(String, default="")


class AccessLog(Base):
    """被拒绝的访问与回传。拒绝事件不制造成功业务审计，但必须留痕。"""

    __tablename__ = "access_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    time: Mapped[datetime] = mapped_column(DateTime, default=now)
    org_id: Mapped[str] = mapped_column(String, default="")
    subject: Mapped[str] = mapped_column(String, default="")
    subject_kind: Mapped[str] = mapped_column(String, default="user")  # user | service | system
    method: Mapped[str] = mapped_column(String, default="")
    path: Mapped[str] = mapped_column(String, default="")
    outcome: Mapped[str] = mapped_column(String, default="denied")
    code: Mapped[str] = mapped_column(String, default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    request_id: Mapped[str] = mapped_column(String, default="")


class IdempotencyKey(Base):
    """幂等记录。作用域 = 组织 + 调用主体 + 动作 + 键值，并保存请求摘要。

    相同键且内容一致回放原响应；内容不同返回 409，不重复变更。
    """

    __tablename__ = "idempotency_keys"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, default="")
    subject: Mapped[str] = mapped_column(String, default="")
    action: Mapped[str] = mapped_column(String)
    key: Mapped[str] = mapped_column(String)
    method: Mapped[str] = mapped_column(String)
    path: Mapped[str] = mapped_column(String)
    request_digest: Mapped[str] = mapped_column(String, default="")
    status: Mapped[int] = mapped_column(Integer, default=200)
    body: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    __table_args__ = (
        UniqueConstraint("org_id", "subject", "action", "key", name="uq_idempotency_scope"),
    )


class PlanBatchLink(Base):
    """计划与批次的绑定。已绑定批次的计划不可解锁。"""

    __tablename__ = "plan_batches"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_id: Mapped[str] = mapped_column(String)
    batch_id: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    ref: Mapped[str] = mapped_column(String, default=uid)
