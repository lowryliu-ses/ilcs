"""外部系统经服务身份提交的配方表

Revision ID: 0054_formulation_submissions
Revises: 0053_command_runs

- formulation_submissions：上游系统（AI 配方预测、实验设计平台）用服务身份提交的一张配方表。按提交方自己给的
  请求编号去重（(组织, 服务身份, 请求编号) 唯一）：同一编号同一内容重发回放首次结果，内容不同拒绝；
  记下生成的方案、流程与瓶子，提交方之后按请求编号查进度与结果。

只加表，已有数据不变。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0054_formulation_submissions"
down_revision: Union[str, None] = "0053_command_runs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "formulation_submissions",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("org_id", sa.String(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("service_id", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False, server_default=""),
        sa.Column("request_id", sa.String(), nullable=False),
        sa.Column("digest", sa.String(), nullable=False),
        sa.Column("template_id", sa.String(), nullable=False),
        sa.Column("template_code", sa.String(), nullable=False),
        sa.Column("filename", sa.String(), nullable=False, server_default=""),
        sa.Column("plan_id", sa.String(), nullable=False),
        sa.Column("recipe_id", sa.String(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("org_id", "service_id", "request_id", name="uq_formulation_submission_request"),
    )
    op.create_index("ix_formulation_submissions_org_id", "formulation_submissions", ["org_id"])
    op.create_index("ix_formulation_submissions_plan_id", "formulation_submissions", ["plan_id"])


def downgrade() -> None:
    op.drop_index("ix_formulation_submissions_plan_id", table_name="formulation_submissions")
    op.drop_index("ix_formulation_submissions_org_id", table_name="formulation_submissions")
    op.drop_table("formulation_submissions")
