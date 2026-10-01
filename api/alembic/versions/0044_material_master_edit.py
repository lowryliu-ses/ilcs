"""物料主数据可维护：乐观锁与修改时间

Revision ID: 0044_material_master_edit
Revises: 0043_backfill_sample_done

以前物料主数据只能登记（或在入库批号时按名称顺带建），建了就改不了：类别、单位换算、CAS 只能删库重来。
现在在「试剂耗材 → 物料主数据」里改，改要带 row_version（乐观锁）并留审计；不再使用的停用、不删除——
批号、预留与投料流水都指回它。已有批号的物料不能改名称与基础单位：批号、预留与设备回报的消耗按名称与单位对账。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0044_material_master_edit"
down_revision: Union[str, None] = "0043_backfill_sample_done"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("materials", sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("materials", sa.Column("updated_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("materials", "updated_at")
    op.drop_column("materials", "row_version")
