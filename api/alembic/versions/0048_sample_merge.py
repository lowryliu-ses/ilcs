"""样本合并：物理样本可以有几个母样（parent_ids）

Revision ID: 0048_sample_merge
Revises: 0047_plan_sample_policy

流程多一种节点「样本合并」：同一条件组（或全部）的在用样本合成一个新样本（合并洗涤液、收集馏分、拼批）。
新样本的谱系要指回全部母样，physical_samples 加 parent_ids（JSON 列表）；parent_id 仍记第一个母样，
只看单亲的地方照旧能用。已有样本 parent_ids 为空。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0048_sample_merge"
down_revision: Union[str, None] = "0047_plan_sample_policy"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("physical_samples", sa.Column("parent_ids", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("physical_samples", "parent_ids")
