"""ILCS 设备网关 SDK：把一台设备（多半是厂家 SDK / DLL）包成 ILCS 的 `http_json_v1` 网关契约。

设备开发者实现 `Device`（身份、开始、读状态、保持 / 恢复 / 终止），其余由 SDK 负责：
按指令号去重与回放、先落盘的作业台账、按指令号查询、回执丢了宁可不回、令牌与 TLS、模拟设备的统一控制口。

    from ilcs_gateway import Device, Job, Rejected, Status, serve

    class Oven(Device):
        def identity(self): ...
        def start(self, job: Job) -> str: ...
        def status(self, job: Job) -> Status: ...

    serve(Oven(), device_id="OVEN-01", state_dir="./state", port=8443, token_file="./secrets/OVEN-01.token",
          cert="./secrets/OVEN-01.crt", key="./secrets/OVEN-01.key", host_name="oven-gw.lab.internal")

模块结构、交付要求与样板见仓库的 device-modules/README.md。
"""
from .device import Device, Job, ReceiptLost, Rejected, Status
from .gateway import Gateway
from .ledger import Ledger, LedgerError
from .server import GatewayServer, serve
from .simulation import FaultState

__all__ = [
    "Device", "FaultState", "Gateway", "GatewayServer", "Job", "Ledger", "LedgerError", "ReceiptLost", "Rejected",
    "Status", "serve",
]
