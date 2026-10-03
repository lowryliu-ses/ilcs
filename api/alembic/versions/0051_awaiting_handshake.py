"""设备适配器：配置保存后在等第一次握手（adapters.awaiting_handshake_since）

Revision ID: 0051_awaiting_handshake
Revises: 0050_driver_config_gate

新登记、或保存了改变连谁 / 怎么判结论的连接配置之后，适配器先离线、等执行器探测或设备心跳握手。这几秒以前也按
「设备适配器失联」报警：条件随后自动复位，报警却留着等人确认、关闭，每改一次配置就多一条。记下从什么时候开始等，
监控在宽限期内不把它当失联；握手成功就清空。执行门照样按离线挡住下发。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0051_awaiting_handshake"
down_revision: Union[str, None] = "0050_driver_config_gate"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("adapters", sa.Column("awaiting_handshake_since", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("adapters", "awaiting_handshake_since")
