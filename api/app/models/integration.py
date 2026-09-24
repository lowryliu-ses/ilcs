"""出向事件：Webhook 订阅与投递。

投递表是发件箱（outbox）：业务事务提交时，同一事务里写一条待投递记录；执行器在事务外发 HTTP、
按指数退避重试。业务写入与「要通知外部」要么一起提交、要么一起回滚，不会出现「库里改了、外部没收到」
或「外部收到了、库里其实回滚了」。
"""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from ..core.clock import now
from .base import Base, uid


class WebhookSubscription(Base):
    """外部系统订阅哪些主题。签名密钥用于给每次投递算 HMAC，只在创建 / 轮换时显示一次。"""

    __tablename__ = "webhook_subscriptions"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, default="", index=True)
    name: Mapped[str] = mapped_column(String)
    url: Mapped[str] = mapped_column(Text)
    topics: Mapped[list] = mapped_column(JSON, default=list)
    # HMAC-SHA256 签名密钥。投递时要用原文，所以不能只存摘要；接口只在签发时返回它
    secret: Mapped[str] = mapped_column(String)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    row_version: Mapped[int] = mapped_column(Integer, default=1)


class WebhookDelivery(Base):
    """一次投递。event_id 对同一业务事件稳定：接收方按它去重，重投不会让对方处理两遍。"""

    __tablename__ = "webhook_deliveries"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, default="", index=True)
    subscription_id: Mapped[str] = mapped_column(String, index=True)
    event_id: Mapped[str] = mapped_column(String)
    topic: Mapped[str] = mapped_column(String)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    # pending | delivered | dead
    state: Mapped[str] = mapped_column(String, default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    last_error: Mapped[str] = mapped_column(Text, default="")
    response_status: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
