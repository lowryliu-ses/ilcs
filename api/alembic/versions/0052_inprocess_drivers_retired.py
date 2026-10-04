"""进程内协议驱动移出 ILCS：库里还有真实工位用它们就拒绝升级

Revision ID: 0052_inprocess_drivers_retired
Revises: 0051_awaiting_handshake

modbus_map_v1、opcua_map_v1、rest_map_v1、line_command_v1、modbus_tcp_v1、opcua_v1 已经移到驱动宿主（devices/host 的
同名插件），ILCS 只经 sila2_v1、http_json_v1 接设备。还有真实工位用这几个驱动时升级到这一版，执行器建不出驱动、
这些设备一律失联——所以在迁移这一步就停下，先把它们迁走再升级：
- 在驱动宿主里登记这些设备（映射原样放进设备文件），工位改 sila2_v1 接驱动宿主（本机的做法见
  scripts/load-driver-host-devices.py）；
- 或者工位不再接外部设备：切回内置模拟（scripts/configure-pilot-adapters.py simulate --station …）。

结构不变；降级什么也不做。
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0052_inprocess_drivers_retired"
down_revision: Union[str, None] = "0051_awaiting_handshake"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# 已移出 ILCS 的驱动 → 驱动宿主里对应的插件
RETIRED = {
    "modbus_map_v1": "modbus_map", "opcua_map_v1": "opcua_map", "rest_map_v1": "rest_map",
    "line_command_v1": "line_command", "modbus_tcp_v1": "modbus_task", "opcua_v1": "opcua_task",
}


def upgrade() -> None:
    rows = op.get_bind().execute(
        sa.text("SELECT station_id, driver FROM adapters WHERE kind = 'real' AND driver IN :drivers ORDER BY station_id")
        .bindparams(sa.bindparam("drivers", expanding=True)),
        {"drivers": sorted(RETIRED)},
    ).fetchall()
    if rows:
        listed = "、".join(f"{station}（{driver} → 驱动宿主插件 {RETIRED[driver]}）" for station, driver in rows)
        raise RuntimeError(
            f"这些工位还在用已经移出 ILCS 的驱动：{listed}。先在驱动宿主里登记这些设备、工位改 sila2_v1 接驱动宿主"
            "（或切回内置模拟），再升级；这一版的 ILCS 建不出这些驱动"
        )


def downgrade() -> None:
    pass
