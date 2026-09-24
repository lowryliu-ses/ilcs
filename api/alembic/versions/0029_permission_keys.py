"""角色权限矩阵记下保存时已有的权限键

Revision ID: 0029_permission_keys
Revises: 0028_notification_channels

存过矩阵的组织：之后新增的权限键按出厂默认补给各角色（否则一升级就丢掉新功能的权限）。
已有行为空，按「P1 新增的权限键之前的全集」处理。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0029_permission_keys"
down_revision: Union[str, None] = "0028_notification_channels"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("role_permission_sets", sa.Column("known_permissions", sa.JSON(), nullable=False,
                                                    server_default=sa.text("'[]'")))


def downgrade() -> None:
    op.drop_column("role_permission_sets", "known_permissions")
