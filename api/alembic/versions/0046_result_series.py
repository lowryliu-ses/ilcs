"""曲线型检测值：结果值加 value_series

Revision ID: 0046_result_series
Revises: 0045_report_templates

充放电曲线、循环曲线、谱图这类「一组 x–y 点」的结果以前只能当原始文件附件，进不了结果审核、分析与报告。
指标多一种类型 series（单位是 y 的单位，规则里写 x 轴名称与单位、点数上限、从曲线派生的数值指标），
值存在 result_values.value_series（JSON：{"traces": [{"name", "x", "y"}]}）。只加一列，已有结果不变。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0046_result_series"
down_revision: Union[str, None] = "0045_report_templates"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("result_values", sa.Column("value_series", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("result_values", "value_series")
