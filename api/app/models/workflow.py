"""四类步骤的执行实例与推进事件。

设备动作与流程推进分离：设备回执只完成对应 StepRun，下一节点由推进器决定。
"""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from ..core.clock import now
from .base import Base, uid

STEP_KINDS = ("device", "manual", "wait", "review")


class StepRun(Base):
    """一次步骤执行。同一步的重试通过 attempt 区分，旧记录保留。"""

    __tablename__ = "step_runs"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    batch_id: Mapped[str] = mapped_column(ForeignKey("batches.id"), index=True)
    step_id: Mapped[str] = mapped_column(String)
    step_index: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String, default="device")
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    # pending | ready | running | waiting | completed | failed | unknown | cancelled
    state: Mapped[str] = mapped_column(String, default="pending")
    step_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    assignee_user_id: Mapped[str] = mapped_column(String, default="")
    station_id: Mapped[str] = mapped_column(String, default="")
    due_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    form_data: Mapped[dict] = mapped_column(JSON, default=dict)
    conclusion: Mapped[str] = mapped_column(String, default="")
    reason: Mapped[Text] = mapped_column(Text, default="")
    submitted_by: Mapped[str] = mapped_column(String, default="")
    reviewed_by: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    row_version: Mapped[int] = mapped_column(Integer, default=1)
    __table_args__ = (
        UniqueConstraint("batch_id", "step_id", "attempt", name="uq_step_run_attempt"),
    )


class WorkflowEvent(Base):
    """持久化推进事件。同一 event_key 只处理一次，后台进程无浏览器请求也能推进。"""

    __tablename__ = "workflow_events"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    batch_id: Mapped[str] = mapped_column(String, default="")
    step_run_id: Mapped[str] = mapped_column(String, default="")
    event_key: Mapped[str] = mapped_column(String)
    # device_ack | manual_submit | review_decision | wait_due | cancel
    event_type: Mapped[str] = mapped_column(String)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    # pending | processing | processed | rejected
    state: Mapped[str] = mapped_column(String, default="pending")
    available_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    claimed_by: Mapped[str] = mapped_column(String, default="")
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")
    # 非业务性失败的重试次数；退避时间写在 available_at
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    __table_args__ = (UniqueConstraint("org_id", "event_key", name="uq_workflow_event_key"),)


class StepAdvance(Base):
    """下一节点创建的唯一约束。防止两个事件并发把同一步推进两次。"""

    __tablename__ = "step_advances"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    batch_id: Mapped[str] = mapped_column(String)
    from_step_id: Mapped[str] = mapped_column(String)
    from_attempt: Mapped[int] = mapped_column(Integer, default=1)
    to_step_id: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    __table_args__ = (
        UniqueConstraint("batch_id", "from_step_id", "from_attempt", name="uq_step_advance"),
    )
