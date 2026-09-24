"""数据质量：越界打标、前后逻辑规则、结果的设备列、遥测归属、步骤输出打标

Revision ID: 0023_data_quality
Revises: 0022_device_methods

- result_values 加 flags（自动打标）、station_id / instrument（测出这个值的设备）；
- 新表 data_rules：同一检测任务内指标之间的逻辑约束；
- telemetry 加 command_id / step_id / step_index / operator / sample_id；
- step_runs 加 flags：设备回报对照方法输出规则的打标。
已有数据全部为空：历史结果不补打标，历史遥测不回填归属。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0023_data_quality"
down_revision: Union[str, None] = "0022_device_methods"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

EMPTY_LIST = sa.text("'[]'")


def upgrade() -> None:
    op.add_column("result_values", sa.Column("flags", sa.JSON(), nullable=False, server_default=EMPTY_LIST))
    op.add_column("result_values", sa.Column("station_id", sa.String(), nullable=False, server_default=""))
    op.add_column("result_values", sa.Column("instrument", sa.String(), nullable=False, server_default=""))
    op.create_table(
        "data_rules",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("org_id", sa.String(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("left_metric", sa.String(), nullable=False),
        sa.Column("op", sa.String(), nullable=False, server_default="<="),
        sa.Column("right_metric", sa.String(), nullable=False, server_default=""),
        sa.Column("right_value", sa.Float(), nullable=True),
        sa.Column("factor", sa.Float(), nullable=False, server_default="1"),
        sa.Column("offset", sa.Float(), nullable=False, server_default="0"),
        sa.Column("severity", sa.String(), nullable=False, server_default="flag"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_by", sa.String(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.create_index("ix_data_rules_org_id", "data_rules", ["org_id"])
    for column in ("command_id", "step_id", "operator", "sample_id"):
        op.add_column("telemetry", sa.Column(column, sa.String(), nullable=False, server_default=""))
    op.add_column("telemetry", sa.Column("step_index", sa.Integer(), nullable=True))
    op.add_column("step_runs", sa.Column("flags", sa.JSON(), nullable=False, server_default=EMPTY_LIST))


def downgrade() -> None:
    op.drop_column("step_runs", "flags")
    for column in ("step_index", "sample_id", "operator", "step_id", "command_id"):
        op.drop_column("telemetry", column)
    op.drop_index("ix_data_rules_org_id", table_name="data_rules")
    op.drop_table("data_rules")
    for column in ("instrument", "station_id", "flags"):
        op.drop_column("result_values", column)
