"""适配器注册表。

当前内置模拟适配器与四个真实驱动：`http_json_v1`（HTTPS 网关）、`sila2_v1`（SiLA 2）、
`modbus_tcp_v1`（Modbus TCP 任务寄存器）、`opcua_v1`（OPC UA TaskExecution）。其他厂商协议在 DEC-02
确认后继续登记专用驱动；执行层始终按 Adapter.kind/driver 取实现。
"""
from __future__ import annotations

from ..core.config import settings
from ..models import Adapter
from .base import AdapterContract, AdapterError, DeviceAdapter
from .http_json import DRIVER as HTTP_JSON_DRIVER, HttpJsonAdapter
from .modbus_tcp import DRIVER as MODBUS_TCP_DRIVER, ModbusTcpAdapter
from .opcua import DRIVER as OPCUA_DRIVER, OpcUaAdapter
from .sila2 import DRIVER as SILA2_DRIVER, Sila2Adapter
from .simulation import SimulationAdapter

_CACHE: dict[str, DeviceAdapter] = {}
REAL_IMPLEMENTATIONS: dict[str, type] = {
    HTTP_JSON_DRIVER: HttpJsonAdapter,
    SILA2_DRIVER: Sila2Adapter,
    MODBUS_TCP_DRIVER: ModbusTcpAdapter,
    OPCUA_DRIVER: OpcUaAdapter,
}
# 这些协议的设备不会往系统推心跳：在线状态由执行器按周期读取设备身份得到
PROBE_DRIVERS = {SILA2_DRIVER, MODBUS_TCP_DRIVER, OPCUA_DRIVER}


def adapter_for(record: Adapter, capabilities: tuple[str, ...] = ()) -> DeviceAdapter:
    key = f"{record.station_id}:{record.kind}:{record.driver}:{record.config_version}"
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    if record.kind == "real":
        implementation = REAL_IMPLEMENTATIONS.get(record.driver)
        if implementation is None:
            raise NotImplementedError(
                f"工位 {record.station_id} 声明驱动 {record.driver}（{record.protocol}）为真实设备，"
                f"但当前版本没有登记该驱动；已登记的驱动：{', '.join(sorted(REAL_IMPLEMENTATIONS))}"
            )
        instance = implementation(record)
    else:
        if not settings.simulation_allowed:
            # 正式环境里「漏配成模拟」的工位不能在没有硬件的情况下报告执行完成
            raise AdapterError(
                f"工位 {record.station_id} 仍是模拟适配器；正式环境禁止模拟执行，"
                f"请在工位与能力页配置真实驱动并通过健康检查"
            )
        instance = SimulationAdapter(record.station_id, record.protocol, capabilities)
    # 同一工位的旧版本实例（配置已变更）不会再被用到：关掉它持有的连接
    for stale in [cached_key for cached_key in _CACHE if cached_key.startswith(f"{record.station_id}:")]:
        _close(_CACHE.pop(stale))
    _CACHE[key] = instance
    return instance


def _close(instance: DeviceAdapter) -> None:
    close = getattr(instance, "close", None)
    if close is not None:
        try:
            close()
        except Exception:  # 关旧连接失败不影响新实例
            pass


def contract_of(record: Adapter) -> AdapterContract:
    """不实例化也能回答界面「这台设备支持什么」。"""
    return AdapterContract(
        kind=record.kind,
        protocol=record.protocol,
        version=record.version,
        supports_hold=record.supports_hold,
        supports_abort=record.supports_abort,
        supports_query=record.supports_query,
        supports_dedup=record.supports_dedup,
        note=record.note,
    )


def reset_cache() -> None:
    for instance in _CACHE.values():
        _close(instance)
    _CACHE.clear()


def probe_interval(record: Adapter) -> float | None:
    """由执行器主动探测在线的适配器返回探测周期（秒）；设备自己推心跳的返回 None。

    `heartbeat_mode` 可在适配器配置里显式指定；sila2_v1 / modbus_tcp_v1 / opcua_v1 默认探测，
    http_json_v1 默认推送（网关也可以配成 probe，由执行器读 /health）。
    """
    if record.kind != "real":
        return None
    config = record.config or {}
    mode = config.get("heartbeat_mode") or ("probe" if record.driver in PROBE_DRIVERS else "push")
    return float(config.get("probe_interval_sec") or 10) if mode == "probe" else None
