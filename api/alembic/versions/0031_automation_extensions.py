"""扩展自动化场景：依赖放行条件、协同资源、多载具角色

Revision ID: 0031_automation_extensions
Revises: 0030_occupancy_and_cleaning

- experiment_tasks.dependency_gate：上游怎样才算满足（运行结束 / 数据复核通过 / 报告发布放行），
  已有任务记为 run_completed，行为不变；
- commands.assist_station_ids：一步执行期间一并占用的协同工位，随设备动作一起取得、一起释放；
- labware.role：载具在批次里的角色，空串是主载具。已有绑定都是主载具，行为不变。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0031_automation_extensions"
down_revision: Union[str, None] = "0030_occupancy_and_cleaning"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "experiment_tasks",
        sa.Column("dependency_gate", sa.String(), nullable=False, server_default="run_completed"),
    )
    op.add_column(
        "commands", sa.Column("assist_station_ids", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )
    op.add_column("labware", sa.Column("role", sa.String(), nullable=False, server_default=""))


def downgrade() -> None:
    op.drop_column("labware", "role")
    op.drop_column("commands", "assist_station_ids")
    op.drop_column("experiment_tasks", "dependency_gate")
