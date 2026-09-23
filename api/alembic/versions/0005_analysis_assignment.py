"""检测任务允许只绑定物理样本

Revision ID: 0005_analysis_assignment
Revises: 0004_adapter_config
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005_analysis_assignment"
down_revision: Union[str, None] = "0004_adapter_config"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # sample_id 是某次批次运行分配；独立物理样本没有运行分配，不能拿物理
    # 样本 ID 冒充 samples.id。保留旧外键但允许 NULL，权威实体写 physical_sample_id。
    with op.batch_alter_table("analysis_tasks") as batch_op:
        batch_op.alter_column("sample_id", existing_type=sa.String(), nullable=True)


def downgrade() -> None:
    bind = op.get_bind()
    missing = bind.execute(
        sa.text("SELECT COUNT(*) FROM analysis_tasks WHERE sample_id IS NULL")
    ).scalar_one()
    if missing:
        raise RuntimeError(
            f"不能降级：{missing} 个检测任务只绑定物理样本，没有运行分配 sample_id"
        )
    with op.batch_alter_table("analysis_tasks") as batch_op:
        batch_op.alter_column("sample_id", existing_type=sa.String(), nullable=False)
