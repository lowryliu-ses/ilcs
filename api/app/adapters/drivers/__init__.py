"""内置驱动：ILCS 只经两个契约接设备，外加内置模拟。

框架层在上一级：驱动契约（`base.py`）、回执解读（`contract.py`）、HTTP 通道（`http_client.py`）、注册表
（`registry.py`）、驱动目录（`catalog.py`）与接入验收（`acceptance.py`）。

- `sila2`：SiLA 2 设备服务——驱动宿主（ilcs-devices/host）上的协议插件（PLC 点表、Modbus / OPC UA 任务契约、REST、
  串口命令），或厂商的 SiLA 服务器；
- `http_json`：实现 ILCS 网关契约的 HTTPS 服务（设备模块的网关、厂家 SDK 接口服务）；
- `simulation`：系统内置的模拟适配器。

协议驱动不再放这里：新协议写成驱动宿主的插件（ilcs-devices/host/ilcs_host/plugins）。
"""
