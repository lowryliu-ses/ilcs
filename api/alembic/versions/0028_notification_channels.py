"""站外通知渠道：企业微信 / 钉钉机器人、邮件

Revision ID: 0028_notification_channels
Revises: 0027_audit_append_only

webhook_subscriptions 加 channel（webhook | wecom | dingtalk | email）与 config（收件人、报警严重度门槛）。
已有订阅都是 webhook，行为不变。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0028_notification_channels"
down_revision: Union[str, None] = "0027_audit_append_only"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("webhook_subscriptions", sa.Column("channel", sa.String(), nullable=False, server_default="webhook"))
    op.add_column("webhook_subscriptions", sa.Column("config", sa.JSON(), nullable=False, server_default=sa.text("'{}'")))


def downgrade() -> None:
    op.drop_column("webhook_subscriptions", "config")
    op.drop_column("webhook_subscriptions", "channel")
