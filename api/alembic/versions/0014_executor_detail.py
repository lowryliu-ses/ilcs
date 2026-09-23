"""执行器每轮运行情况

Revision ID: 0014_executor_detail
Revises: 0013_queue_indexes

并发执行器每轮记录耗时、线程数、仍在跑与疑似卡住的工位，界面据此显示执行器健康度。
已有存活记录记为空对象。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0014_executor_detail"
down_revision: Union[str, None] = "0013_queue_indexes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "executor_heartbeats",
        sa.Column("detail", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )


def downgrade() -> None:
    op.drop_column("executor_heartbeats", "detail")
