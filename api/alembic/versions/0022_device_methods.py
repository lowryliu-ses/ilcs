"""设备方法目录与驱动自报

Revision ID: 0022_device_methods
Revises: 0021_recipe_simulation

- 新表 device_methods：能力 + 适用型号 + 设备端程序 + 参数范围 + 输出规则，按版本管理；
- 资产加厂商、固件；适配器加驱动自报的厂商 / 固件 / 型号、方法目录与指令类型；
- 指令加 method：步骤引用的方法快照。已有数据全部为空，行为不变。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0022_device_methods"
down_revision: Union[str, None] = "0021_recipe_simulation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

EMPTY_LIST = sa.text("'[]'")
EMPTY_DICT = sa.text("'{}'")


def upgrade() -> None:
    op.create_table(
        "device_methods",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("org_id", sa.String(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("code", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("capability_id", sa.String(), nullable=False),
        sa.Column("instrument_models", sa.JSON(), nullable=False, server_default=EMPTY_LIST),
        sa.Column("program", sa.String(), nullable=False, server_default=""),
        sa.Column("params", sa.JSON(), nullable=False, server_default=EMPTY_DICT),
        sa.Column("outputs", sa.JSON(), nullable=False, server_default=EMPTY_LIST),
        sa.Column("dur_min", sa.Float(), nullable=False, server_default="0"),
        sa.Column("state", sa.String(), nullable=False, server_default="draft"),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_by", sa.String(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("released_by", sa.String(), nullable=False, server_default=""),
        sa.Column("released_at", sa.DateTime(), nullable=True),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
        sa.UniqueConstraint("org_id", "code", "version", name="uq_device_method_version"),
    )
    op.create_index("ix_device_methods_org_id", "device_methods", ["org_id"])
    for column in ("vendor", "firmware"):
        op.add_column("assets", sa.Column(column, sa.String(), nullable=False, server_default=""))
    for column in ("vendor", "firmware", "reported_model", "described_from"):
        op.add_column("adapters", sa.Column(column, sa.String(), nullable=False, server_default=""))
    op.add_column("adapters", sa.Column("methods", sa.JSON(), nullable=False, server_default=EMPTY_LIST))
    op.add_column("adapters", sa.Column("commands", sa.JSON(), nullable=False, server_default=EMPTY_LIST))
    op.add_column("adapters", sa.Column("described_at", sa.DateTime(), nullable=True))
    op.add_column("commands", sa.Column("method", sa.JSON(), nullable=False, server_default=EMPTY_DICT))


def downgrade() -> None:
    op.drop_column("commands", "method")
    for column in ("described_at", "commands", "methods", "described_from", "reported_model", "firmware", "vendor"):
        op.drop_column("adapters", column)
    for column in ("firmware", "vendor"):
        op.drop_column("assets", column)
    op.drop_index("ix_device_methods_org_id", table_name="device_methods")
    op.drop_table("device_methods")
