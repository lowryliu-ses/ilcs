"""方法的执行前仿真结论

Revision ID: 0021_recipe_simulation
Revises: 0020_webhooks

方法加一列记最近一次仿真的结论与内容摘要。已有方法为空：下次提交评审或批准时自动补跑。
已发布的方法不受影响（仿真只在提交评审与批准时作门槛）。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0021_recipe_simulation"
down_revision: Union[str, None] = "0020_webhooks"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("recipes", sa.Column("simulation", sa.JSON(), nullable=False, server_default=sa.text("'{}'")))


def downgrade() -> None:
    op.drop_column("recipes", "simulation")
