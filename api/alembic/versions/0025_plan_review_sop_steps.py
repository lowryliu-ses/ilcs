"""方案模板、多级审批、批注、数字 SOP 与版本恢复

Revision ID: 0025_plan_review_sop_steps
Revises: 0024_sample_labware

- plan_versions 加 approvals：多级审批的逐级结论；
- 新表 plan_templates：方案模板库；
- 新表 comments：方案 / SOP 版本 / 方法 / 报告版本上的批注；
- sop_versions 加 steps（结构化步骤）与 restored_from（从哪个历史版本恢复）。
驳回的方案审批状态改记为 rejected（此前回到 draft）：已有数据不改。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0025_plan_review_sop_steps"
down_revision: Union[str, None] = "0024_sample_labware"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

EMPTY_LIST = sa.text("'[]'")


def upgrade() -> None:
    op.add_column("plan_versions", sa.Column("approvals", sa.JSON(), nullable=False, server_default=EMPTY_LIST))
    op.create_table(
        "plan_templates",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("org_id", sa.String(), nullable=False, server_default=""),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("plan_type", sa.String(), nullable=False, server_default="matrix"),
        sa.Column("recipe_id", sa.String(), nullable=False, server_default=""),
        sa.Column("body", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("source_plan_id", sa.String(), nullable=False, server_default=""),
        sa.Column("retired", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_by", sa.String(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.create_index("ix_plan_templates_org_id", "plan_templates", ["org_id"])
    op.create_table(
        "comments",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("org_id", sa.String(), nullable=False, server_default=""),
        sa.Column("target_type", sa.String(), nullable=False),
        sa.Column("target_id", sa.String(), nullable=False),
        sa.Column("target_version", sa.String(), nullable=False, server_default=""),
        sa.Column("anchor", sa.String(), nullable=False, server_default=""),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("author_id", sa.String(), nullable=False, server_default=""),
        sa.Column("author_name", sa.String(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("resolved", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("resolved_by", sa.String(), nullable=False, server_default=""),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_comments_org_id", "comments", ["org_id"])
    op.create_index("ix_comments_target_id", "comments", ["target_id"])
    op.add_column("sop_versions", sa.Column("steps", sa.JSON(), nullable=False, server_default=EMPTY_LIST))
    op.add_column("sop_versions", sa.Column("restored_from", sa.String(), nullable=False, server_default=""))


def downgrade() -> None:
    op.drop_column("sop_versions", "restored_from")
    op.drop_column("sop_versions", "steps")
    op.drop_index("ix_comments_target_id", table_name="comments")
    op.drop_index("ix_comments_org_id", table_name="comments")
    op.drop_table("comments")
    op.drop_index("ix_plan_templates_org_id", table_name="plan_templates")
    op.drop_table("plan_templates")
    op.drop_column("plan_versions", "approvals")
