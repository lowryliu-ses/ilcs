"""账号多角色与组织可编辑的角色权限矩阵

Revision ID: 0012_role_permissions
Revises: 0011_automation_campaigns
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012_role_permissions"
down_revision: Union[str, None] = "0011_automation_campaigns"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 已有账号的角色列表就是原来的单一角色；矩阵不预置行，组织沿用出厂默认值直到管理员修改
    op.add_column("users", sa.Column("roles", sa.JSON(), nullable=False, server_default=sa.text("'[]'")))
    op.execute("UPDATE users SET roles = json_build_array(role)")
    op.create_table(
        "role_permission_sets",
        sa.Column("org_id", sa.String(), sa.ForeignKey("organizations.id"), primary_key=True),
        sa.Column("matrix", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_by", sa.String(), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("role_permission_sets")
    op.drop_column("users", "roles")
