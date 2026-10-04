"""协议插件：从 ILCS 的 `api/app/adapters/drivers` 抽出的设备驱动，配置写法与 ILCS 里的完全一样。

- 映射插件（`modbus_map`、`opcua_map`、`rest_map`、`line_command`）：设备不认识 ILCS 指令号，插件记作业台账；
- 任务契约插件（`modbus_task`、`opcua_task`）：设备按 ILCS 任务契约编程，指令号、去重、查询都在设备侧。

和 ILCS 那份的差别只有两处：配置项读驱动宿主的 `settings`；明确失败带 SiLA 错误码（`AdapterError.code`）。
"""
from .line_command import LineCommandAdapter
from .modbus_map import ModbusMapAdapter
from .opcua_map import OpcUaMapAdapter
from .rest_map import RestMapAdapter
from .task_contract import ModbusTaskDevice, OpcUaTaskDevice

PLUGINS = {
    "line_command": LineCommandAdapter,
    "modbus_map": ModbusMapAdapter,
    "opcua_map": OpcUaMapAdapter,
    "rest_map": RestMapAdapter,
    "modbus_task": ModbusTaskDevice,
    "opcua_task": OpcUaTaskDevice,
}
