"""设备侧结论与实体孔位

Revision ID: 0032_outcome_and_labware_wells
Revises: 0031_automation_extensions

- commands.outcome：设备对动作给出的结论，与投递事实（delivery_state）分开记。设备收到指令却回报
  「结论未知」时记 unknown：动作可能仍在进行，占用保留到现场核查；明确失败记 failed。已有指令为空串，
  行为不变；
- slot_occupancies.labware_well：绑定实体载具后样本在载具上的实体孔位（规范化，A01 记为 A1）。
  container_id + well 是批次布局的逻辑孔位，布局放不进板型时两者不同；物理唯一性按「载具 + 实体孔位」
  约束（部分唯一索引，只约束在途占用）。回填：母样取物理样本上的实体孔位，分装子样取登记的孔位；
  历史上同一实体孔位若已有多个在途占用，只给最早的一条回填，其余留空，由现场核对后处理。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0032_outcome_and_labware_wells"
down_revision: Union[str, None] = "0031_automation_extensions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("commands", sa.Column("outcome", sa.String(), nullable=False, server_default=""))
    op.add_column("slot_occupancies", sa.Column("labware_well", sa.String(), nullable=False, server_default=""))
    op.execute(
        r"""
        UPDATE slot_occupancies AS so
        SET labware_well = regexp_replace(
            upper(trim(CASE WHEN ps.labware_id = so.labware_id AND ps.well <> '' THEN ps.well ELSE so.well END)),
            '^([A-Z]+)0*([0-9]+)$', '\1\2'
        )
        FROM physical_samples AS ps
        WHERE ps.id = so.physical_sample_id AND so.labware_id IS NOT NULL AND so.released_at IS NULL
        """
    )
    op.execute(
        """
        UPDATE slot_occupancies SET labware_well = ''
        WHERE id IN (
            SELECT id FROM (
                SELECT id, ROW_NUMBER() OVER (
                    PARTITION BY labware_id, labware_well ORDER BY occupied_at, id
                ) AS rank
                FROM slot_occupancies
                WHERE released_at IS NULL AND labware_id IS NOT NULL AND labware_well <> ''
            ) AS ranked
            WHERE ranked.rank > 1
        )
        """
    )
    op.create_index(
        "uq_slot_labware_live", "slot_occupancies", ["labware_id", "labware_well"], unique=True,
        postgresql_where=sa.text("released_at IS NULL AND labware_id IS NOT NULL AND labware_well <> ''"),
    )


def downgrade() -> None:
    op.drop_index("uq_slot_labware_live", table_name="slot_occupancies")
    op.drop_column("slot_occupancies", "labware_well")
    op.drop_column("commands", "outcome")
