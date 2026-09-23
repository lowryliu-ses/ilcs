"""账号生命周期与首次改密

Revision ID: 0007_account_lifecycle
Revises: 0006_service_identity_version
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007_account_lifecycle"
down_revision: Union[str, None] = "0006_service_identity_version"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        # 历史账号保持可登录；新建/重置的账号由服务层显式置为 True。
        batch_op.add_column(
            sa.Column("must_change_password", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.add_column(sa.Column("password_changed_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"))


def downgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("row_version")
        batch_op.drop_column("password_changed_at")
        batch_op.drop_column("must_change_password")
