"""能力参数的类型与单位；指令上的前馈参数记录

Revision ID: 0038_param_specs_bindings
Revises: 0037_batch_split_units

- capabilities.param_specs：参数键 → {type, unit, required}。已有能力为空：按「数值、单位未登记、必填」解释，
  与之前的行为一致。不从参数显示名称里拆单位——名称是给人看的文字，猜出来的单位不能当换算依据，
  由负责人在能力字典里登记；
- commands.bindings：取自上游结果的参数（前馈）在下发时的求值记录。已有指令为空列表：它们都没有前馈参数。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0038_param_specs_bindings"
down_revision: Union[str, None] = "0037_batch_split_units"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "capabilities", sa.Column("param_specs", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )
    op.add_column("commands", sa.Column("bindings", sa.JSON(), nullable=False, server_default=sa.text("'[]'")))


def downgrade() -> None:
    op.drop_column("commands", "bindings")
    op.drop_column("capabilities", "param_specs")
