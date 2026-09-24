"""任务树与任务间依赖

Revision ID: 0017_task_tree
Revises: 0016_flow_control

- 实验任务加父任务编号（任务树）与上游任务列表（完成—开始依赖）。已有任务都是顶层、无依赖。
- 父任务不绑定批次、状态由子任务汇总；叶子任务仍与批次一一对应，部分唯一索引不变。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0017_task_tree"
down_revision: Union[str, None] = "0016_flow_control"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("experiment_tasks", sa.Column("parent_id", sa.String(), nullable=False, server_default=""))
    op.add_column("experiment_tasks", sa.Column("depends_on", sa.JSON(), nullable=False, server_default=sa.text("'[]'")))
    op.create_index("ix_experiment_tasks_parent_id", "experiment_tasks", ["parent_id"])


def downgrade() -> None:
    op.drop_index("ix_experiment_tasks_parent_id", table_name="experiment_tasks")
    op.drop_column("experiment_tasks", "depends_on")
    op.drop_column("experiment_tasks", "parent_id")
