"""A 批加固：方法职责分离与乐观锁、工位乐观锁、指令超时、报警来源与去重、执行器存活

Revision ID: 0009_operations_hardening
Revises: 0008_event_retry
"""
import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009_operations_hardening"
down_revision: Union[str, None] = "0008_event_retry"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("recipes") as batch_op:
        # 历史方法没有作者 ID：留空表示「未知」，不伪造成某个真人
        batch_op.add_column(sa.Column("author_user_id", sa.String(), nullable=False, server_default=""))
        batch_op.add_column(sa.Column("submitted_by", sa.String(), nullable=False, server_default=""))
        batch_op.add_column(
            sa.Column("used_step_ids", sa.JSON(), nullable=False, server_default=sa.text("'[]'"))
        )
        batch_op.add_column(sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"))
    with op.batch_alter_table("stations") as batch_op:
        batch_op.add_column(sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"))
    with op.batch_alter_table("commands") as batch_op:
        batch_op.add_column(sa.Column("started_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("overdue_at", sa.DateTime(), nullable=True))
    with op.batch_alter_table("alarms") as batch_op:
        batch_op.add_column(sa.Column("origin", sa.String(), nullable=False, server_default="device"))
        batch_op.add_column(sa.Column("condition_key", sa.String(), nullable=False, server_default=""))
        batch_op.create_index("ix_alarms_condition_key", ["condition_key"])
    op.create_table(
        "executor_heartbeats",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("host", sa.String(), nullable=False, server_default=""),
        sa.Column("pid", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen", sa.DateTime(), nullable=False),
    )

    connection = op.get_bind()
    # 批次上的报警都是软件在执行异常时生成的，设备无从知道它们的 ID
    connection.execute(sa.text("UPDATE alarms SET origin = 'system' WHERE source_type = 'batch'"))
    # 已有步骤 ID 进入「用过的 ID」集合；已删除步骤的 ID 在历史里找不回，只能从现在开始记
    rows = connection.execute(sa.text("SELECT id, steps FROM recipes")).fetchall()
    for recipe_id, raw in rows:
        steps = raw if isinstance(raw, list) else json.loads(raw or "[]")
        used = sorted({
            str(step.get("step_id")) for step in steps or []
            if isinstance(step, dict) and step.get("step_id")
        })
        connection.execute(
            sa.text("UPDATE recipes SET used_step_ids = :used WHERE id = :id"),
            {"used": json.dumps(used), "id": recipe_id},
        )


def downgrade() -> None:
    op.drop_table("executor_heartbeats")
    with op.batch_alter_table("alarms") as batch_op:
        batch_op.drop_index("ix_alarms_condition_key")
        batch_op.drop_column("condition_key")
        batch_op.drop_column("origin")
    with op.batch_alter_table("commands") as batch_op:
        batch_op.drop_column("overdue_at")
        batch_op.drop_column("started_at")
    with op.batch_alter_table("stations") as batch_op:
        batch_op.drop_column("row_version")
    with op.batch_alter_table("recipes") as batch_op:
        batch_op.drop_column("row_version")
        batch_op.drop_column("used_step_ids")
        batch_op.drop_column("submitted_by")
        batch_op.drop_column("author_user_id")
