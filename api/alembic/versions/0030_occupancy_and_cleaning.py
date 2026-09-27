"""实际占用、逐设备停止确认、清洗确认与计划开始时刻

Revision ID: 0030_occupancy_and_cleaning
Revises: 0029_permission_keys

- commands.target_command_id：保持 / 终止 / 续跑针对的动作指令。一条控制指令只确认它自己的目标，
  不再用一台设备的回执结束整批其他设备上的动作；
- stations.dirty_batch_id：需要清洗的动作做完后工位转为待清洗，记下用过它的批次；清洗确认前
  别的批次不能投递到这台设备；
- batches.planned_start_at：排程时的计划开始时刻。不占工位的起点步骤（以及纯人工流程）据此推算
  完成时间，不再只看最后一个设备时间窗；
- 数据修正：资产容量不小于映射到它的工位的通道数。此前排程按工位整体排除同工位负载，
  8 通道充放电柜挂在容量 1 的资产上也能并行；现在资产容量按全部映射工位统一计数，
  这类配置要先对齐，否则通道会被资产容量压成 1。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0030_occupancy_and_cleaning"
down_revision: Union[str, None] = "0029_permission_keys"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("commands", sa.Column("target_command_id", sa.String(), nullable=False, server_default=""))
    op.add_column("stations", sa.Column("dirty_batch_id", sa.String(), nullable=False, server_default=""))
    op.add_column("batches", sa.Column("planned_start_at", sa.DateTime(), nullable=True))
    op.execute(
        """
        UPDATE assets SET capacity = mapped.channels
        FROM (
            SELECT asset_id, MAX(COALESCE(channels, 1)) AS channels
            FROM stations WHERE asset_id <> '' GROUP BY asset_id
        ) AS mapped
        WHERE assets.id = mapped.asset_id AND assets.capacity < mapped.channels
        """
    )


def downgrade() -> None:
    op.drop_column("batches", "planned_start_at")
    op.drop_column("stations", "dirty_batch_id")
    op.drop_column("commands", "target_command_id")
