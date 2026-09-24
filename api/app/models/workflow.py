"""步骤的执行实例、推进事件与批次业务信号。

设备动作与流程推进分离：设备回执只完成对应 StepRun，下一节点由推进器决定。
"""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from ..core.clock import now
from .base import Base, uid

STEP_KINDS = ("device", "manual", "wait", "review", "gate", "split", "branch")


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
    # | superseded | skipped | not_taken
    state: Mapped[str] = mapped_column(String, default="pending")
    step_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    assignee_user_id: Mapped[str] = mapped_column(String, default="")
    station_id: Mapped[str] = mapped_column(String, default="")
    due_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 步骤级超时：开出时按 timeout.minutes 算出；到点由推进器处理一次（timed_out_at 记处理时刻）
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    timed_out_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
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
    # device_ack | manual_submit | review_decision | wait_due | signal | timeout | cancel
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


class BatchSignal(Base):
    """批次业务信号：外部系统或现场人员告诉流程「某件事发生了」，唤醒等着它的业务事件等待节点。

    早到的信号先登记、不丢：等待节点开出时直接消费。一条信号只唤醒一个等待实例
    （consumed_by_run_id），同名的下一个等待节点要等下一条信号。`event_key` 是发送方给的
    幂等键——重发同一条信号不会唤醒两次。
    """

    __tablename__ = "batch_signals"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    batch_id: Mapped[str] = mapped_column(ForeignKey("batches.id"), index=True)
    name: Mapped[str] = mapped_column(String)
    event_key: Mapped[str] = mapped_column(String)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    # 发送方：user:<id> 或 service:<服务身份 id>
    source: Mapped[str] = mapped_column(String, default="")
    source_label: Mapped[str] = mapped_column(String, default="")
    received_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    consumed_by_run_id: Mapped[str] = mapped_column(String, default="")
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    __table_args__ = (UniqueConstraint("org_id", "event_key", name="uq_batch_signal_key"),)
