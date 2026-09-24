"""重排建议

Revision ID: 0019_schedule_proposals
Revises: 0018_exception_engine

设备故障 / 失联、紧急插单、指令故障、人工请求时生成的重排建议：重排前后的时间窗、每个批次的
完成时间变化，调度确认后才写进时间线；时间线在此期间被改过则建议作废。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0019_schedule_proposals"
down_revision: Union[str, None] = "0018_exception_engine"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "schedule_proposals",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("org_id", sa.String(), nullable=False, server_default=""),
        sa.Column("trigger", sa.String(), nullable=False, server_default="manual"),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("station_id", sa.String(), nullable=False, server_default=""),
        sa.Column("state", sa.String(), nullable=False, server_default="pending"),
        sa.Column("batch_ids", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("before", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("after", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("impact", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("unplanned", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("auto_applied", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("decided_by", sa.String(), nullable=False, server_default=""),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
    )
    op.create_index("ix_schedule_proposals_org_id", "schedule_proposals", ["org_id"])
    op.create_index("ix_schedule_proposals_state", "schedule_proposals", ["state"])


def downgrade() -> None:
    op.drop_index("ix_schedule_proposals_state", table_name="schedule_proposals")
    op.drop_index("ix_schedule_proposals_org_id", table_name="schedule_proposals")
    op.drop_table("schedule_proposals")
