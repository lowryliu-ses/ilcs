"""配液模板：按实验表格生成流程草稿与方案草稿的规则

Revision ID: 0042_formulation_templates
Revises: 0041_device_templates

- formulation_templates：一条产线「怎么把一张配方表变成流程」——固定步骤、加料阶段、物料类别对应的加法、
  搅拌规则与每次实验可配的参数（config），(组织, 编号) 唯一。

模板本身不走发布：它只决定怎么生成流程草稿，生成的流程照旧走 评审 → 批准 → 发布，那才是受控点。
改模板要带 row_version（乐观锁）并留审计；不再使用的模板退役，不删除——导入审计指回它。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0042_formulation_templates"
down_revision: Union[str, None] = "0041_device_templates"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "formulation_templates",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("org_id", sa.String(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("code", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("config", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        # active | retired
        sa.Column("state", sa.String(), nullable=False, server_default="active"),
        sa.Column("created_by", sa.String(), nullable=False, server_default=""),
        sa.Column("created_by_name", sa.String(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
        sa.UniqueConstraint("org_id", "code", name="uq_formulation_template_code"),
    )
    op.create_index("ix_formulation_templates_org_id", "formulation_templates", ["org_id"])


def downgrade() -> None:
    op.drop_index("ix_formulation_templates_org_id", table_name="formulation_templates")
    op.drop_table("formulation_templates")
