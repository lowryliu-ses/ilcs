"""服务身份配置增加乐观并发版本

Revision ID: 0006_service_identity_version
Revises: 0005_analysis_assignment
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006_service_identity_version"
down_revision: Union[str, None] = "0005_analysis_assignment"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("service_identities") as batch_op:
        batch_op.add_column(
            sa.Column("row_version", sa.Integer(), nullable=False, server_default="1")
        )
        batch_op.create_unique_constraint("uq_service_identity_source_global", ["source"])


def downgrade() -> None:
    with op.batch_alter_table("service_identities") as batch_op:
        batch_op.drop_constraint("uq_service_identity_source_global", type_="unique")
        batch_op.drop_column("row_version")
