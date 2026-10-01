"""方案指定的样本可以接着用上一步的产物：plans 加 sample_policy

Revision ID: 0047_plan_sample_policy
Revises: 0046_result_series

矩阵方案指定的瓶子一直按「一瓶一配方」处理：用过的瓶子不能再进新批次。多步合成要把上一批的产物
接着做下一步反应（换一组条件），这条规则就挡住了。方案多一个 sample_policy：fresh（缺省，原规则）/
continue（接着用：允许上一批已经跑完的样本，仍不允许正在别的批次里、或已处置用尽的样本）。已有方案都是 fresh。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0047_plan_sample_policy"
down_revision: Union[str, None] = "0046_result_series"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("plans", sa.Column("sample_policy", sa.String(), nullable=False, server_default="fresh"))


def downgrade() -> None:
    op.drop_column("plans", "sample_policy")
