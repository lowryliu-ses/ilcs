"""B 批：按时开工、遥测上报去重、维护工单

Revision ID: 0010_operations_b
Revises: 0009_operations_hardening
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010_operations_b"
down_revision: Union[str, None] = "0009_operations_hardening"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("commands", sa.Column("not_before", sa.DateTime(), nullable=True))

    op.add_column("telemetry", sa.Column("event_id", sa.String(), nullable=False, server_default=""))
    op.add_column("telemetry", sa.Column("received_at", sa.DateTime(), nullable=True))
    # 设备重发同一批点不重复入库；历史点（event_id 为空）不参与唯一约束
    op.create_index(
        "uq_telemetry_event", "telemetry", ["station_id", "event_id", "metric"], unique=True,
        postgresql_where=sa.text("event_id <> ''"),
    )
    op.create_index("ix_telemetry_device_ts", "telemetry", ["device_ts"])

    op.create_table(
        "maintenance_orders",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("org_id", sa.String(), sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("asset_id", sa.String(), nullable=False, index=True),
        sa.Column("kind", sa.String(), nullable=False, server_default="preventive"),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False, server_default=""),
        sa.Column("planned_start", sa.DateTime(), nullable=False),
        sa.Column("planned_end", sa.DateTime(), nullable=False),
        sa.Column("state", sa.String(), nullable=False, server_default="planned"),
        sa.Column("booking_id", sa.String(), nullable=False, server_default=""),
        sa.Column("assignee_user_id", sa.String(), nullable=False, server_default=""),
        sa.Column("asset_state_before", sa.String(), nullable=False, server_default=""),
        sa.Column("created_by", sa.String(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("started_by", sa.String(), nullable=False, server_default=""),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("completed_by", sa.String(), nullable=False, server_default=""),
        sa.Column("result", sa.String(), nullable=False, server_default=""),
        sa.Column("record", sa.Text(), nullable=False, server_default=""),
        sa.Column("signature_id", sa.String(), nullable=False, server_default=""),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
    )


def downgrade() -> None:
    op.drop_table("maintenance_orders")
    op.drop_index("ix_telemetry_device_ts", table_name="telemetry")
    op.drop_index("uq_telemetry_event", table_name="telemetry")
    op.drop_column("telemetry", "received_at")
    op.drop_column("telemetry", "event_id")
    op.drop_column("commands", "not_before")
