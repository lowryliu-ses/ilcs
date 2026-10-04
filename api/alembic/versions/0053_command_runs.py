"""设备指令按瓶拆开下发：commands.runs

Revision ID: 0053_command_runs
Revises: 0052_inprocess_drivers_retired

一次只能处理一瓶的设备（天平秤上一个位置、光谱仪单测量位），设备接入配置里填 `wells_per_command`。一步要做的瓶数
超过它时，ILCS 仍记一条指令，但按瓶拆成依次执行的设备指令 `<指令号>/<序号>`：每条的孔位、状态、回执记在这里，
执行器重启后按这里接着查、接着下发；每瓶做完就按它的实际量入账，续跑 / 重试跳过已经做完的瓶。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0053_command_runs"
down_revision: Union[str, None] = "0052_inprocess_drivers_retired"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("commands", sa.Column("runs", sa.JSON(), nullable=False, server_default=sa.text("'[]'")))


def downgrade() -> None:
    op.drop_column("commands", "runs")
