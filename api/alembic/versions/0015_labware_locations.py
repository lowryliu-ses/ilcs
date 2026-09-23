"""耗材载具、位置与可执行的转运

Revision ID: 0015_labware_locations
Revises: 0014_executor_detail

- 载具类型 / 位置 / 载具 / 移位记录四张表。位置只由设备回执或扫码确认写入，不按计划推算。
- 指令加前置指令（设备步骤等它的转运把板送到位）与所搬载具。已有指令两列记为空串。
- 不预置任何位置：没有登记位置的部署不启用位置追踪，行为与之前一致。位置由
  `scripts/configure-locations.py` 或界面按现场布局登记。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0015_labware_locations"
down_revision: Union[str, None] = "0014_executor_detail"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "labware_types",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False, server_default="plate"),
        sa.Column("rows", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("cols", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.create_table(
        "locations",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False, server_default="nest"),
        sa.Column("station_id", sa.String(), nullable=False, server_default=""),
        sa.Column("group", sa.String(), nullable=False, server_default=""),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("accepts", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.create_index("ix_locations_station_id", "locations", ["station_id"])
    op.create_table(
        "labware",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("org_id", sa.String(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("barcode", sa.String(), nullable=False),
        sa.Column("type_id", sa.String(), sa.ForeignKey("labware_types.id"), nullable=False),
        sa.Column("batch_id", sa.String(), nullable=False, server_default=""),
        sa.Column("location_id", sa.String(), sa.ForeignKey("locations.id"), nullable=True),
        sa.Column("state", sa.String(), nullable=False, server_default="idle"),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
        sa.UniqueConstraint("org_id", "barcode", name="uq_labware_org_barcode"),
    )
    op.create_index("ix_labware_org_id", "labware", ["org_id"])
    op.create_index("ix_labware_batch_id", "labware", ["batch_id"])
    op.create_index(
        "uq_labware_location_live", "labware", ["location_id"], unique=True,
        postgresql_where=sa.text("location_id IS NOT NULL AND state <> 'retired'"),
    )
    op.create_table(
        "labware_moves",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("org_id", sa.String(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("labware_id", sa.String(), sa.ForeignKey("labware.id"), nullable=False),
        sa.Column("from_location_id", sa.String(), nullable=False, server_default=""),
        sa.Column("to_location_id", sa.String(), nullable=False, server_default=""),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("command_id", sa.String(), nullable=False, server_default=""),
        sa.Column("batch_id", sa.String(), nullable=False, server_default=""),
        sa.Column("barcode_confirmed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("by", sa.String(), nullable=False, server_default=""),
        sa.Column("at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_labware_moves_org_id", "labware_moves", ["org_id"])
    op.create_index("ix_labware_moves_labware_id", "labware_moves", ["labware_id"])
    op.add_column("commands", sa.Column("after_command_id", sa.String(), nullable=False, server_default=""))
    op.add_column("commands", sa.Column("labware_id", sa.String(), nullable=False, server_default=""))
    op.create_index("ix_commands_after_command_id", "commands", ["after_command_id"])


def downgrade() -> None:
    op.drop_index("ix_commands_after_command_id", table_name="commands")
    op.drop_column("commands", "labware_id")
    op.drop_column("commands", "after_command_id")
    op.drop_table("labware_moves")
    op.drop_table("labware")
    op.drop_table("locations")
    op.drop_table("labware_types")
