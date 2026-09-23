"""适配器受控配置

Revision ID: 0004_adapter_config
Revises: 0003_history_mapping
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004_adapter_config"
down_revision: Union[str, None] = "0003_history_mapping"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("adapters") as batch_op:
        batch_op.add_column(sa.Column("driver", sa.String(), nullable=False, server_default="simulation"))
        batch_op.add_column(sa.Column("config", sa.JSON(), nullable=False, server_default=sa.text("'{}'")))
        batch_op.add_column(sa.Column("credential_ref", sa.String(), nullable=False, server_default=""))
        batch_op.add_column(sa.Column("config_version", sa.Integer(), nullable=False, server_default="1"))
        batch_op.add_column(sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()))
        batch_op.add_column(sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"))
        batch_op.add_column(
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp())
        )

    # 现有记录全是模拟器。协议名称保留用于界面展示，执行注册键明确写 simulation。
    op.execute("UPDATE adapters SET driver = 'simulation' WHERE kind = 'simulation'")


def downgrade() -> None:
    with op.batch_alter_table("adapters") as batch_op:
        batch_op.drop_column("updated_at")
        batch_op.drop_column("row_version")
        batch_op.drop_column("enabled")
        batch_op.drop_column("config_version")
        batch_op.drop_column("credential_ref")
        batch_op.drop_column("config")
        batch_op.drop_column("driver")
