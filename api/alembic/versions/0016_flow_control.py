"""流程控制：条件分支 / 回环、业务事件等待、步骤级超时与跳过

Revision ID: 0016_flow_control
Revises: 0015_labware_locations

- 步骤实例加截止时刻与超时处理时刻（步骤级超时）。已有实例两列为空：历史步骤没有超时配置。
- 批次业务信号表：外部系统或现场人员发出的事件，唤醒业务事件等待节点。早到的信号先登记，
  等待节点开出时消费；(组织, 事件键) 唯一，重发不重复唤醒。
- 分支、跳过、子流程不需要新列：分支出口记在步骤实例的 conclusion，子流程在建批次时展开进快照。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0016_flow_control"
down_revision: Union[str, None] = "0015_labware_locations"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("step_runs", sa.Column("deadline_at", sa.DateTime(), nullable=True))
    op.add_column("step_runs", sa.Column("timed_out_at", sa.DateTime(), nullable=True))
    # 推进器每轮扫「开着且到了截止时刻、还没处理过」的实例：部分索引只覆盖这一小部分行
    op.create_index(
        "ix_step_runs_deadline_open", "step_runs", ["deadline_at"],
        postgresql_where=sa.text(
            "deadline_at IS NOT NULL AND timed_out_at IS NULL "
            "AND state IN ('pending', 'ready', 'running', 'waiting')"
        ),
    )
    op.create_table(
        "batch_signals",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("org_id", sa.String(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("batch_id", sa.String(), sa.ForeignKey("batches.id"), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("event_key", sa.String(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("source", sa.String(), nullable=False, server_default=""),
        sa.Column("source_label", sa.String(), nullable=False, server_default=""),
        sa.Column("received_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("consumed_by_run_id", sa.String(), nullable=False, server_default=""),
        sa.Column("consumed_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("org_id", "event_key", name="uq_batch_signal_key"),
    )
    op.create_index("ix_batch_signals_org_id", "batch_signals", ["org_id"])
    op.create_index("ix_batch_signals_batch_id", "batch_signals", ["batch_id"])
    op.create_index(
        "ix_batch_signals_unconsumed", "batch_signals", ["batch_id", "name"],
        postgresql_where=sa.text("consumed_by_run_id = ''"),
    )


def downgrade() -> None:
    op.drop_index("ix_batch_signals_unconsumed", table_name="batch_signals")
    op.drop_index("ix_batch_signals_batch_id", table_name="batch_signals")
    op.drop_index("ix_batch_signals_org_id", table_name="batch_signals")
    op.drop_table("batch_signals")
    op.drop_index("ix_step_runs_deadline_open", table_name="step_runs")
    op.drop_column("step_runs", "timed_out_at")
    op.drop_column("step_runs", "deadline_at")
