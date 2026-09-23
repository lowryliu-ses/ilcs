"""推进事件重试计数

Revision ID: 0008_event_retry
Revises: 0007_account_lifecycle
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008_event_retry"
down_revision: Union[str, None] = "0007_account_lifecycle"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("workflow_events") as batch_op:
        # 已有事件视为从未重试；退避时间沿用 available_at
        batch_op.add_column(
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0")
        )


def downgrade() -> None:
    with op.batch_alter_table("workflow_events") as batch_op:
        batch_op.drop_column("attempts")
