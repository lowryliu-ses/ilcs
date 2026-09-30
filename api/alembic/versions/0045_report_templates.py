"""自定义报告模板：取舍、排序、改章节标题，加固定文字章节；草稿可改，发布后冻结，修订出新版本

Revision ID: 0045_report_templates
Revises: 0044_material_master_edit

- report_templates：组织自己的报告模板。(组织, 模板键, 版本) 唯一；模板键不能与内置模板（standard / summary / audit）重名。
  sections 是有序的章节清单：内置章节（取数只有一套，只决定选哪些、按什么顺序、叫什么）或固定文字章节（声明、方法说明）。
  state：draft 可改 → released 冻结（发布人不能是起草人）→ retired（出了新版本或停用）。报告生成时取该键最新的已发布版本，
  章节清单写进报告内容的快照里，之后模板怎么改都不影响已有报告。
只加新表，已有数据不变。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0045_report_templates"
down_revision: Union[str, None] = "0044_material_master_edit"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "report_templates",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("org_id", sa.String(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("sections", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        # draft | released | retired
        sa.Column("state", sa.String(), nullable=False, server_default="draft"),
        sa.Column("created_by", sa.String(), nullable=False, server_default=""),
        sa.Column("created_by_name", sa.String(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("released_by", sa.String(), nullable=False, server_default=""),
        sa.Column("released_by_name", sa.String(), nullable=False, server_default=""),
        sa.Column("released_at", sa.DateTime(), nullable=True),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
        sa.UniqueConstraint("org_id", "key", "version", name="uq_report_template_version"),
    )
    op.create_index("ix_report_templates_org_id", "report_templates", ["org_id"])


def downgrade() -> None:
    op.drop_index("ix_report_templates_org_id", table_name="report_templates")
    op.drop_table("report_templates")
