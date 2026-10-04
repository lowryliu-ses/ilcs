"""协议插件：从 ILCS 的 `api/app/adapters/drivers` 抽出的映射驱动，配置写法与 ILCS 里的完全一样。

ILCS 里的那几份在迁移期间冻结（只修 bug，修了两边一起改），设备都切到驱动宿主之后删除。
和 ILCS 那份的差别只有两处：配置项读驱动宿主的 `settings`；明确失败带 SiLA 错误码（`AdapterError.code`）。
"""
from .line_command import LineCommandAdapter
from .modbus_map import ModbusMapAdapter
from .opcua_map import OpcUaMapAdapter
from .rest_map import RestMapAdapter

PLUGINS = {
    "line_command": LineCommandAdapter,
    "modbus_map": ModbusMapAdapter,
    "opcua_map": OpcUaMapAdapter,
    "rest_map": RestMapAdapter,
}
