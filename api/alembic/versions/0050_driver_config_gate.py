"""驱动配置摘要闸门：adapters.driver_info / approved_driver / driver_approval，acceptance_runs.driver_info

Revision ID: 0050_driver_config_gate
Revises: 0049_point_writes

设备驱动移到 ILCS 之外的驱动宿主以后，点表与映射在驱动项目里改，ILCS 看不到改了什么。设备服务在
DeviceInfo.Driver 里报插件与配置摘要：执行器探测时记下最近一次报的（driver_info），接入验收通过时把验收看到的那份记为
已批准（approved_driver）；报的摘要变了、又不是已批准的，照配置变更处理——配置版本加一、欠接入验收、停派工，
并且要有权限的人核对后签名批准这一份（driver_approval：批准了哪个摘要、谁、哪个签名），验收看到的正是这一份才放行。
验收记录同时存下它验收的是哪一份驱动配置，作为上线证据。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0050_driver_config_gate"
down_revision: Union[str, None] = "0049_point_writes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("adapters", sa.Column("driver_info", sa.JSON(), nullable=False, server_default=sa.text("'{}'")))
    op.add_column("adapters", sa.Column("approved_driver", sa.JSON(), nullable=False, server_default=sa.text("'{}'")))
    op.add_column("adapters", sa.Column("driver_approval", sa.JSON(), nullable=False, server_default=sa.text("'{}'")))
    op.add_column("acceptance_runs", sa.Column("driver_info", sa.JSON(), nullable=False, server_default=sa.text("'{}'")))


def downgrade() -> None:
    op.drop_column("acceptance_runs", "driver_info")
    op.drop_column("adapters", "driver_approval")
    op.drop_column("adapters", "approved_driver")
    op.drop_column("adapters", "driver_info")
