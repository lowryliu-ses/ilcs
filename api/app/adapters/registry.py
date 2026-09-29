"""适配器注册表。

当前内置模拟适配器与这些真实驱动：

| 驱动 | 设备侧 | 指令号 / 去重 / 查询 |
|---|---|---|
| `http_json_v1` | 实现 ILCS 网关契约的 HTTPS 服务（含厂家 SDK 接口服务） | 设备侧 |
| `sila2_v1` | 实现 ILCS TaskExecution 特性的 SiLA 2 服务器 | 设备侧 |
| `opcua_v1` | 实现 ILCS TaskExecution 节点的 OPC UA 服务器 | 设备侧 |
| `modbus_tcp_v1` | 按 ILCS 任务寄存器表编程的 PLC | 设备侧 |
| `opcua_map_v1` | 设备自有 OPC UA 节点（PLC、视觉系统） | 驱动作业台账 |
| `modbus_map_v1` | 设备自有 Modbus 寄存器表（PLC、温控仪表） | 驱动作业台账 |
| `line_command_v1` | 串口 / TCP 文本命令（RS232、RS485、仪表盘服务） | 驱动作业台账 |
| `mt_sics_v1` | 梅特勒 MT-SICS 天平 | 驱动作业台账 |
| `rest_map_v1` | 设备或调度系统自有 REST 接口（AGV 车队等） | 驱动作业台账 + 设备任务号 |
| `sql_table_v1` | 数据库中间表（设备侧软件轮询 ILCS 中间库契约的作业表） | 中间库（作业表主键是指令号） |
| `composite_v1` | 一个工位多台仪器，按能力分派到上面的驱动 | 各子驱动 |

执行层始终按 Adapter.kind/driver 取实现。
"""
from __future__ import annotations

from ..core.config import settings
from ..models import Adapter
from .base import AdapterContract, AdapterError, DeviceAdapter
from .drivers.composite import DRIVER as COMPOSITE_DRIVER, CompositeAdapter
from .drivers.http_json import DRIVER as HTTP_JSON_DRIVER, HttpJsonAdapter
from .drivers.line_command import DRIVER as LINE_COMMAND_DRIVER, LineCommandAdapter
from .drivers.modbus_map import DRIVER as MODBUS_MAP_DRIVER, ModbusMapAdapter
from .drivers.modbus_tcp import DRIVER as MODBUS_TCP_DRIVER, ModbusTcpAdapter
from .drivers.mt_sics import DRIVER as MT_SICS_DRIVER, MtSicsAdapter
from .drivers.opcua import DRIVER as OPCUA_DRIVER, OpcUaAdapter
from .drivers.opcua_map import DRIVER as OPCUA_MAP_DRIVER, OpcUaMapAdapter
from .drivers.rest_map import DRIVER as REST_MAP_DRIVER, RestMapAdapter
from .drivers.sila2 import DRIVER as SILA2_DRIVER, Sila2Adapter
from .drivers.sql_table import DRIVER as SQL_TABLE_DRIVER, SqlTableAdapter
from .drivers.simulation import SimulationAdapter

_CACHE: dict[str, DeviceAdapter] = {}
REAL_IMPLEMENTATIONS: dict[str, type] = {
    HTTP_JSON_DRIVER: HttpJsonAdapter,
    SILA2_DRIVER: Sila2Adapter,
    MODBUS_TCP_DRIVER: ModbusTcpAdapter,
    OPCUA_DRIVER: OpcUaAdapter,
    OPCUA_MAP_DRIVER: OpcUaMapAdapter,
    MODBUS_MAP_DRIVER: ModbusMapAdapter,
    LINE_COMMAND_DRIVER: LineCommandAdapter,
    MT_SICS_DRIVER: MtSicsAdapter,
    REST_MAP_DRIVER: RestMapAdapter,
    SQL_TABLE_DRIVER: SqlTableAdapter,
    COMPOSITE_DRIVER: CompositeAdapter,
}
# 这些协议的设备不会往系统推心跳：在线状态由执行器按周期读取设备身份得到
PROBE_DRIVERS = {
    SILA2_DRIVER, MODBUS_TCP_DRIVER, OPCUA_DRIVER, OPCUA_MAP_DRIVER, MODBUS_MAP_DRIVER, LINE_COMMAND_DRIVER,
    MT_SICS_DRIVER, REST_MAP_DRIVER, SQL_TABLE_DRIVER, COMPOSITE_DRIVER,
}


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
                f"请在「工位与接入 → 设备连接」配置真实驱动并通过健康检查"
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


def contract_of(record: Adapter, capabilities: tuple[str, ...] = ()) -> AdapterContract:
    """不实例化也能回答界面「这台设备支持什么」。能力取工位能力极限登记的能力。"""
    return AdapterContract(
        kind=record.kind,
        protocol=record.protocol,
        version=record.version,
        capabilities=tuple(capabilities),
        supports_hold=record.supports_hold,
        supports_abort=record.supports_abort,
        supports_query=record.supports_query,
        supports_dedup=record.supports_dedup,
        note=record.note,
    )


def catalog_of(record: Adapter) -> dict:
    """驱动自报（或按登记配置）的设备身份与方法目录，界面与工位匹配用。"""
    return {
        "vendor": record.vendor or "", "firmware": record.firmware or "", "reported_model": record.reported_model or "",
        "methods": list(record.methods or []), "commands": list(record.commands or []),
        "described_from": record.described_from or "",
        "described_at": record.described_at.isoformat(timespec="seconds") if record.described_at else None,
    }


def describe(instance: DeviceAdapter, record: Adapter) -> dict:
    """读设备自报的身份与方法目录。

    SiLA 2 / OPC UA / HTTP 网关的设备身份是一段 JSON，带了 `methods` / `commands` 就按设备自报；
    Modbus 这类寄存器协议带不了目录，按适配器配置里登记的 `methods`（来源标 config）。
    读身份失败照常抛异常（结果按离线处理），不返回假目录。
    """
    identity = getattr(instance, "identity", None)
    raw = identity() if identity is not None else {}
    raw = raw if isinstance(raw, dict) else {}
    config = record.config or {}
    reported = raw.get("methods")
    if isinstance(reported, list):
        # 组合工位把各路由的目录汇总上来，并说明来源（设备自报还是按配置登记）
        methods, source = reported, str(raw.get("methods_source") or "device")
    elif isinstance(config.get("methods"), list):
        methods, source = config["methods"], "config"
    else:
        methods, source = [], "none"
    rows = []
    for item in methods:
        row = {"program": item} if isinstance(item, str) else dict(item) if isinstance(item, dict) else {}
        if str(row.get("program") or "").strip():
            rows.append({
                "program": str(row["program"]).strip(), "name": str(row.get("name") or row["program"]),
                "capability": str(row.get("capability") or ""),
            })
    commands = raw.get("commands") if isinstance(raw.get("commands"), list) else config.get("commands") or []
    return {
        "vendor": str(raw.get("vendor") or config.get("vendor") or ""),
        "firmware": str(raw.get("firmware") or raw.get("version") or config.get("firmware") or ""),
        "reported_model": str(raw.get("model") or ""),
        "methods": rows, "commands": [str(value) for value in commands], "described_from": source,
    }


def release(station_id: str) -> None:
    """关掉并丢弃这个工位缓存的驱动实例（接入验收要自己建实例：保持连接的设备不能被两个连接同时占着）。

    下一次投递、轮询时按当前配置重建。
    """
    for key in [cached_key for cached_key in _CACHE if cached_key.startswith(f"{station_id}:")]:
        _close(_CACHE.pop(key))


def reset_cache() -> None:
    for instance in _CACHE.values():
        _close(instance)
    _CACHE.clear()


def probe_interval(record: Adapter) -> float | None:
    """由执行器主动探测在线的适配器返回探测周期（秒）；设备自己推心跳的返回 None。

    `heartbeat_mode` 可在适配器配置里显式指定；除 http_json_v1 外的真实驱动默认探测，
    http_json_v1 默认推送（网关也可以配成 probe，由执行器读 /health）。
    """
    if record.kind != "real":
        return None
    config = record.config or {}
    mode = config.get("heartbeat_mode") or ("probe" if record.driver in PROBE_DRIVERS else "push")
    return float(config.get("probe_interval_sec") or 10) if mode == "probe" else None
