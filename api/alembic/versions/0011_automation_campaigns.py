"""C 批：工位并行通道、方案显式设计点与设计空间、闭环提案

Revision ID: 0011_automation_campaigns
Revises: 0010_operations_b
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011_automation_campaigns"
down_revision: Union[str, None] = "0010_operations_b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 并行通道数与样品位是两回事：已有工位一律按 1 个通道，不因为样品位多就放开并行
    op.add_column("stations", sa.Column("channels", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("plans", sa.Column("design_points", sa.JSON(), nullable=False, server_default=sa.text("'[]'")))
    op.add_column("plans", sa.Column("design_space", sa.JSON(), nullable=False, server_default=sa.text("'{}'")))
    op.add_column("plans", sa.Column("parent_plan_id", sa.String(), nullable=False, server_default=""))
    op.add_column("plans", sa.Column("round_no", sa.Integer(), nullable=False, server_default="1"))
    op.create_table(
        "plan_proposals",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("org_id", sa.String(), sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("plan_id", sa.String(), nullable=False, index=True),
        sa.Column("proposal_key", sa.String(), nullable=False),
        sa.Column("digest", sa.String(), nullable=False, server_default=""),
        sa.Column("source", sa.String(), nullable=False, server_default=""),
        sa.Column("model_version", sa.String(), nullable=False, server_default=""),
        sa.Column("rationale", sa.Text(), nullable=False, server_default=""),
        sa.Column("points", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("state", sa.String(), nullable=False, server_default="accepted"),
        sa.Column("issues", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("created_plan_id", sa.String(), nullable=False, server_default=""),
        sa.Column("created_by", sa.String(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("org_id", "plan_id", "proposal_key", name="uq_plan_proposal_key"),
    )


def downgrade() -> None:
    op.drop_table("plan_proposals")
    op.drop_column("plans", "round_no")
    op.drop_column("plans", "parent_plan_id")
    op.drop_column("plans", "design_space")
    op.drop_column("plans", "design_points")
    op.drop_column("stations", "channels")
