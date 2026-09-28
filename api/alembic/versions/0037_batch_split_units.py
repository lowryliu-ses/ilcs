"""一个方案分多批执行；按样本占通道

Revision ID: 0037_batch_split_units
Revises: 0036_station_asset_source

- experiment_tasks.portion：子任务在父任务里负责的那一份（按数量拆出的样本数、矩阵按重复拆出的重复次数、
  补测的条件组与个数、第一个样本 / 重复的全局序号偏移）。已有任务为空：按样本清单或方案整体执行，行为不变；
- experiment_tasks.split_mode：父任务怎么拆的（并行 / 首批验证后放行 / 逐批顺序 / 整体重复）。已有父任务留空；
- experiment_tasks.purpose：retest 表示补测子任务，它补的是别的子任务的短缺，不算父任务计划量；
- experiment_tasks.shortfall_decisions：父任务上「按现有结果结束、不再补测」的签名记录；
- stations.channel_unit：通道按批次计（batch，一个设备步骤占 1 个）还是按样本计（sample，一个样本占 1 个，
  如一颗电芯占一个通道的充放电柜）。已有工位记为 batch，行为不变；
- allocations.units / commands.units：这段时间窗、这条动作在主工位上占几份通道。已有记录记为 1，行为不变。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0037_batch_split_units"
down_revision: Union[str, None] = "0036_station_asset_source"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "experiment_tasks", sa.Column("portion", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )
    op.add_column("experiment_tasks", sa.Column("split_mode", sa.String(), nullable=False, server_default=""))
    op.add_column("experiment_tasks", sa.Column("purpose", sa.String(), nullable=False, server_default=""))
    op.add_column(
        "experiment_tasks",
        sa.Column("shortfall_decisions", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )
    op.add_column("stations", sa.Column("channel_unit", sa.String(), nullable=False, server_default="batch"))
    op.add_column("allocations", sa.Column("units", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("commands", sa.Column("units", sa.Integer(), nullable=False, server_default="1"))


def downgrade() -> None:
    op.drop_column("commands", "units")
    op.drop_column("allocations", "units")
    op.drop_column("stations", "channel_unit")
    op.drop_column("experiment_tasks", "shortfall_decisions")
    op.drop_column("experiment_tasks", "purpose")
    op.drop_column("experiment_tasks", "split_mode")
    op.drop_column("experiment_tasks", "portion")
