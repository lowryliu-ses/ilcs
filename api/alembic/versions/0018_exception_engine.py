"""异常引擎：统一异常事件、策略库、报警类别

Revision ID: 0018_exception_engine
Revises: 0017_task_tree

- 异常事件表：类别、来源、影响面、自动处理（策略 / 动作 / 结果）、人工处理、最终结果。
- 策略库表：按类别（可按能力 / 工位 / 步骤类型 / 方法 / 步骤细分）选择重试、改派、跳过、重排建议或转人工。
  不预置任何策略：没有策略时一切异常照旧转人工，行为与之前一致。
- 报警加类别列；已有报警按去重键回填（station:*:heartbeat_stale → 通信异常 这类），键认不出的留空。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0018_exception_engine"
down_revision: Union[str, None] = "0017_task_tree"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("alarms", sa.Column("category", sa.String(), nullable=False, server_default=""))
    # LIKE 模式里的冒号要转义，否则会被当成绑定参数
    op.execute(sa.text(r"""
        UPDATE alarms SET category = CASE
            WHEN condition_key LIKE '%\:interlock' THEN 'safety'
            WHEN condition_key LIKE '%\:disconnected' OR condition_key LIKE '%\:heartbeat_stale' THEN 'communication'
            WHEN condition_key LIKE '%\:calibration%' THEN 'device_fault'
            WHEN condition_key LIKE '%\:overdue' OR condition_key LIKE '%\:timeout' THEN 'timeout'
            WHEN condition_key LIKE '%\:schedule_conflict' THEN 'schedule'
            WHEN condition_key LIKE 'gate\:%' THEN 'sample'
            ELSE '' END
    """))
    op.create_table(
        "exception_events",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("org_id", sa.String(), nullable=False, server_default=""),
        sa.Column("category", sa.String(), nullable=False, server_default="system"),
        sa.Column("severity", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("source_type", sa.String(), nullable=False, server_default="batch"),
        sa.Column("source_id", sa.String(), nullable=False, server_default=""),
        sa.Column("batch_id", sa.String(), nullable=False, server_default=""),
        sa.Column("step_id", sa.String(), nullable=False, server_default=""),
        sa.Column("step_index", sa.Integer(), nullable=False, server_default="-1"),
        sa.Column("station_id", sa.String(), nullable=False, server_default=""),
        sa.Column("command_id", sa.String(), nullable=False, server_default=""),
        sa.Column("alarm_id", sa.String(), nullable=False, server_default=""),
        sa.Column("message", sa.Text(), nullable=False, server_default=""),
        sa.Column("impact", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("never_sent", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("state", sa.String(), nullable=False, server_default="open"),
        sa.Column("rule_id", sa.String(), nullable=False, server_default=""),
        sa.Column("decision", sa.Text(), nullable=False, server_default=""),
        sa.Column("auto_action", sa.String(), nullable=False, server_default=""),
        sa.Column("auto_result", sa.Text(), nullable=False, server_default=""),
        sa.Column("manual_action", sa.String(), nullable=False, server_default=""),
        sa.Column("manual_note", sa.Text(), nullable=False, server_default=""),
        sa.Column("manual_by", sa.String(), nullable=False, server_default=""),
        sa.Column("final_result", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
    )
    for column in ("org_id", "batch_id", "station_id", "state"):
        op.create_index(f"ix_exception_events_{column}", "exception_events", [column])
    op.create_table(
        "exception_rules",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("org_id", sa.String(), nullable=False, server_default=""),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("category", sa.String(), nullable=False),
        sa.Column("match", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("params", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_by", sa.String(), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.create_index("ix_exception_rules_org_id", "exception_rules", ["org_id"])


def downgrade() -> None:
    op.drop_index("ix_exception_rules_org_id", table_name="exception_rules")
    op.drop_table("exception_rules")
    for column in ("org_id", "batch_id", "station_id", "state"):
        op.drop_index(f"ix_exception_events_{column}", table_name="exception_events")
    op.drop_table("exception_events")
    op.drop_column("alarms", "category")
