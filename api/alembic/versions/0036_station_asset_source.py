"""工位的实物属性以资产为准：删校准到期与样品位两列，型号回填到资产

Revision ID: 0036_station_asset_source
Revises: 0035_sop_step_keys

- 删 stations.cal_due：它是资产校准的副本，只在登记合格校准时同步——复校不合格、新关联资产、到期都不会
  更新，工位台账还能直接改它；开跑检查却拿它当执行许可，和按资产校准记录查的那一项各说各话。
  校准许可只看资产校准记录（按每个设备步骤的执行区间），删列不丢任何许可依据。
- 删 stations.positions：样品位从来不参与排程、校验或投递；载具的物理位置由放置位（locations）管。
- 型号：关联了资产的工位以资产登记的型号为准，设备方法按它匹配工位。资产型号为空时用工位上原来登记的
  型号回填；与资产一致的工位型号清空（同一件事只留资产上一份）；两边都有值但不一致的不改，界面提示人
  去核对——资产档案是登记数据，迁移不替人判断哪一边对。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0036_station_asset_source"
down_revision: Union[str, None] = "0035_sop_step_keys"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE assets SET model = picked.model
        FROM (
            SELECT asset_id, MIN(model) AS model
            FROM stations WHERE asset_id <> '' AND model <> ''
            GROUP BY asset_id
        ) AS picked
        WHERE assets.id = picked.asset_id AND COALESCE(assets.model, '') = ''
        """
    )
    op.execute(
        """
        UPDATE stations SET model = ''
        FROM assets
        WHERE stations.asset_id = assets.id AND stations.model = assets.model
        """
    )
    op.drop_column("stations", "cal_due")
    op.drop_column("stations", "positions")


def downgrade() -> None:
    # 只恢复列结构。样品位原值不可恢复（按 1）；校准到期按资产最近一条合格校准的到期日回填，
    # 与旧代码「登记合格校准时同步」的口径一致；旧代码按工位型号匹配设备方法，型号从资产抄回工位
    op.execute(
        """
        UPDATE stations SET model = assets.model
        FROM assets
        WHERE stations.asset_id = assets.id AND stations.model = ''
        """
    )
    op.add_column("stations", sa.Column("positions", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("stations", sa.Column("cal_due", sa.String(), nullable=False, server_default=""))
    op.execute(
        """
        UPDATE stations SET cal_due = to_char(latest.expires_at, 'YYYY-MM-DD')
        FROM (
            SELECT asset_id, MAX(expires_at) AS expires_at
            FROM calibration_records WHERE result = 'pass' AND expires_at IS NOT NULL
            GROUP BY asset_id
        ) AS latest
        WHERE stations.asset_id = latest.asset_id
        """
    )
