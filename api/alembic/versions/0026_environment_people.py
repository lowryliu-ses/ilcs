"""环境读数与人员预占

Revision ID: 0026_environment_people
Revises: 0025_plan_review_sop_steps

- 新表 environment_readings：区域 × 指标的环境读数，步骤的环境要求按最新读数核对；
- 新表 person_bookings：人员预占（批次步骤、请假、培训、值守）。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0026_environment_people"
down_revision: Union[str, None] = "0025_plan_review_sop_steps"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "environment_readings",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("org_id", sa.String(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("zone", sa.String(), nullable=False),
        sa.Column("metric", sa.String(), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("unit", sa.String(), nullable=False, server_default=""),
        sa.Column("source", sa.String(), nullable=False, server_default="manual"),
        sa.Column("measured_at", sa.DateTime(), nullable=False),
        sa.Column("recorded_by", sa.String(), nullable=False, server_default=""),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_environment_readings_org_id", "environment_readings", ["org_id"])
    op.create_index("ix_environment_readings_zone", "environment_readings", ["zone"])
    op.create_index("ix_environment_latest", "environment_readings", ["org_id", "zone", "metric", "measured_at"])
    op.create_table(
        "person_bookings",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("org_id", sa.String(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("person_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False, server_default="step"),
        sa.Column("starts_at", sa.DateTime(), nullable=False),
        sa.Column("ends_at", sa.DateTime(), nullable=False),
        sa.Column("batch_id", sa.String(), nullable=False, server_default=""),
        sa.Column("step_id", sa.String(), nullable=False, server_default=""),
        sa.Column("state", sa.String(), nullable=False, server_default="confirmed"),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_by", sa.String(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_person_bookings_org_id", "person_bookings", ["org_id"])
    op.create_index("ix_person_bookings_person_id", "person_bookings", ["person_id"])
    op.create_index("ix_person_bookings_batch_id", "person_bookings", ["batch_id"])


def downgrade() -> None:
    op.drop_index("ix_person_bookings_batch_id", table_name="person_bookings")
    op.drop_index("ix_person_bookings_person_id", table_name="person_bookings")
    op.drop_index("ix_person_bookings_org_id", table_name="person_bookings")
    op.drop_table("person_bookings")
    op.drop_index("ix_environment_latest", table_name="environment_readings")
    op.drop_index("ix_environment_readings_zone", table_name="environment_readings")
    op.drop_index("ix_environment_readings_org_id", table_name="environment_readings")
    op.drop_table("environment_readings")
