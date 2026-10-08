"""退役早期固定三指标的历史结果：删 results 表与 samples.quality 列

Revision ID: 0055_legacy_results_retired
Revises: 0054_formulation_submissions

results 表是原型期的固定三指标结果（放电比容量、面密度、保持率），samples.quality 是样本上的人工「历史质量标记」。
0003 已把旧三指标映射成类型化结果（result_values，来源 legacy_unreviewed）；之后再没有代码写这两处，结果分析、
复核、报告都只读 result_values。表里还有数据就拒绝升级——先确认它们都已映射到 result_values（迁移报告的
「历史结果未被补造审核」一项），再清空后升级，不替人丢数据。

降级只恢复结构（空表、空列），数据不可恢复。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0055_legacy_results_retired"
down_revision: Union[str, None] = "0054_formulation_submissions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    results = bind.execute(sa.text("SELECT COUNT(*) FROM results")).scalar() or 0
    flagged = bind.execute(sa.text("SELECT COUNT(*) FROM samples WHERE quality IS NOT NULL AND quality <> ''")).scalar() or 0
    if results or flagged:
        raise RuntimeError(
            f"历史结果表还有 {results} 行、{flagged} 个样本带历史质量标记：这一版删掉它们。先确认这些数据已映射到 "
            "result_values（迁移 0003）或另行导出留存，清空后再升级"
        )
    op.drop_table("results")
    op.drop_column("samples", "quality")


def downgrade() -> None:
    op.add_column("samples", sa.Column("quality", sa.String(), nullable=True))
    op.create_table(
        "results",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("sample_id", sa.String(), sa.ForeignKey("samples.id"), nullable=False),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("areal_density", sa.Float(), nullable=True),
        sa.Column("discharge_capacity", sa.Float(), nullable=True),
        sa.Column("retention", sa.Float(), nullable=True),
        sa.Column("raw_uri", sa.String(), nullable=False),
        sa.Column("checksum", sa.String(), nullable=False),
        sa.Column("parser_version", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("org_id", sa.String(), nullable=False, server_default=""),
    )
    op.create_index("ix_results_org_id", "results", ["org_id"], unique=False)
