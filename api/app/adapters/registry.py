"""适配器注册表。

当前内置模拟适配器与通用 `http_json_v1` HTTPS 网关驱动。不能适配统一网关的设备
在 DEC-02 确认厂商协议后继续登记专用驱动；执行层始终按 Adapter.kind/driver 取实现。
"""
from __future__ import annotations

from ..core.config import settings
from ..models import Adapter
from .base import AdapterContract, AdapterError, DeviceAdapter
from .http_json import DRIVER as HTTP_JSON_DRIVER, HttpJsonAdapter
from .simulation import SimulationAdapter

_CACHE: dict[str, DeviceAdapter] = {}
REAL_IMPLEMENTATIONS: dict[str, type] = {HTTP_JSON_DRIVER: HttpJsonAdapter}


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
                f"但当前版本没有登记该驱动；请改用 http_json_v1 网关或先完成专用驱动接入"
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
    _CACHE[key] = instance
    return instance


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
    _CACHE.clear()
