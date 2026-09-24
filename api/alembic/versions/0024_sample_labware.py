"""样本 ↔ 载具真实关联与结构化位置

Revision ID: 0024_sample_labware
Revises: 0023_data_quality

- slot_occupancies 加 labware_id：批次绑定实体载具后，孔位占用指向那块载具；
- physical_samples 加 labware_id / well / location_id：样本在哪块载具的哪个孔位，或放在哪个库位；
- sample_transfers 加 to_location_id：去向是登记过的位置时记编号。
历史数据只有文本位置，结构化字段为空，界面照常显示文本位置。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0024_sample_labware"
down_revision: Union[str, None] = "0023_data_quality"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("slot_occupancies", sa.Column("labware_id", sa.String(), sa.ForeignKey("labware.id"), nullable=True))
    op.add_column("physical_samples", sa.Column("labware_id", sa.String(), sa.ForeignKey("labware.id"), nullable=True))
    op.add_column("physical_samples", sa.Column("well", sa.String(), nullable=False, server_default=""))
    op.add_column("physical_samples", sa.Column("location_id", sa.String(), sa.ForeignKey("locations.id"), nullable=True))
    op.create_index("ix_physical_samples_labware_id", "physical_samples", ["labware_id"])
    op.add_column("sample_transfers", sa.Column("to_location_id", sa.String(), nullable=False, server_default=""))


def downgrade() -> None:
    op.drop_column("sample_transfers", "to_location_id")
    op.drop_index("ix_physical_samples_labware_id", table_name="physical_samples")
    for column in ("location_id", "well", "labware_id"):
        op.drop_column("physical_samples", column)
    op.drop_column("slot_occupancies", "labware_id")
