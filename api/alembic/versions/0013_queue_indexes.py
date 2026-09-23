"""执行器队列与遥测读取索引

Revision ID: 0013_queue_indexes
Revises: 0012_role_permissions

- 执行器每轮按「sent + queued + 已到最早投递时刻」取队列、按状态扫在途指令；原来 commands
  只有 org_id / step_run_id 索引，队列一长就是全表扫描。
- 遥测读取一律按批次过滤（批次详情曲线），原来只有设备时间与去重索引。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0013_queue_indexes"
down_revision: Union[str, None] = "0012_role_permissions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_commands_queue", "commands", ["created_at"],
        postgresql_where=sa.text("state = 'sent' AND delivery_state = 'queued'"),
    )
    op.create_index("ix_commands_state", "commands", ["state"])
    op.create_index("ix_telemetry_batch_ts", "telemetry", ["batch_id", "device_ts"])


def downgrade() -> None:
    op.drop_index("ix_telemetry_batch_ts", table_name="telemetry")
    op.drop_index("ix_commands_state", table_name="commands")
    op.drop_index("ix_commands_queue", table_name="commands")
