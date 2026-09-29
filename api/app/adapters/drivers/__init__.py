"""内置驱动：一种设备协议一个模块，只写设备 I/O 与报文转换。

框架层在上一级：驱动契约（`base.py`）、回执解读（`contract.py`）、驱动作业台账（`jobs.py`）、HTTP 通道
（`http_client.py`）、注册表（`registry.py`）、驱动目录（`catalog.py`）与接入验收（`acceptance.py`）。
新驱动放这里，在 `registry.py` 注册、在 `catalog.py` 声明配置项。

- 认 ILCS 指令号（设备侧去重、按指令号查询）：`http_json`、`sila2`、`opcua`、`modbus_tcp`；
- 不认 ILCS 指令号（驱动作业台账补上去重与查询）：`line_command`、`opcua_map`、`modbus_map`
  （共用 `point_map`）、`rest_map`；
- `simulation`：系统内置的模拟适配器。
"""
