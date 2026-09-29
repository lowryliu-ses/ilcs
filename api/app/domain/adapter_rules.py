"""适配器配置变更的规则。纯函数，服务层据此放行或拒绝。

设备上还有可能在动作的指令时，驱动得能按指令号把它查回来。换驱动、换连接目标、改状态映射或支持标志，
新建出来的驱动实例就对不上那条指令了：
- 设备侧去重的驱动（SiLA 2、OPC UA TaskExecution、HTTPS 网关）查不到它，批次判故障、转人工核查；
- 走作业台账的映射驱动更糟：台账按工位存、不按地址，新实例会拿旧作业去读新地址的状态，
  读到空闲就按「见过运行、回到空闲」判完成——设备其实可能还在动作。
所以这时只放行不改变「连谁、怎么判结论」的字段。
"""
from __future__ import annotations

from typing import Any

# 设备可能仍在动作时照常可改：说明、停用、给人看的协议名与版本
FREE_FIELDS = frozenset({"note", "enabled", "protocol", "version"})
# 连接配置里照常可改的键：只决定等多久、多久探测一次，不决定连谁、怎么判结论
FREE_CONFIG_KEYS = frozenset({"connect_timeout_sec", "request_timeout_sec", "probe_interval_sec"})
FIELD_LABELS = {
    "kind": "模式", "driver": "驱动", "credential_ref": "凭据引用",
    "supports_hold": "保持支持", "supports_abort": "终止支持", "supports_query": "按指令查询支持",
    "supports_dedup": "设备端去重支持", "template_id": "设备接入模板", "template_connection": "模板连接参数",
}
ACTING_LABELS = {
    "accepted": "在途", "running": "在途", "held": "已保持", "unknown": "结果未知", "manual": "人工核查中",
}


def busy_blocked_changes(current: dict[str, Any], changes: dict[str, Any]) -> list[str]:
    """设备可能仍在动作时，这次修改里不能放行的项（给人看的名称）。空列表表示可以保存。

    `current` 是适配器现有的字段值；只比较真的变了的字段——界面保存时会把没改的字段一并发回来。
    """
    blocked: list[str] = []
    for key, value in changes.items():
        if key in FREE_FIELDS or (key == "template_id" and not value):
            continue  # 不再按模板管理：配置原样保留，不改变连谁、怎么判结论
        if key == "config":
            old, new = current.get("config") or {}, value or {}
            blocked.extend(
                f"连接配置 {name}" for name in sorted(set(old) | set(new))
                if name not in FREE_CONFIG_KEYS and old.get(name) != new.get(name)
            )
        elif current.get(key) != value:
            blocked.append(FIELD_LABELS.get(key, key))
    return blocked


# ---------- 配置变更后的接入验收闸门 ----------

READONLY, PHYSICAL = "readonly", "physical"
LEVEL_LABELS = {READONLY: "只读级", PHYSICAL: "动作级"}


def acceptance_requirement(before: dict[str, Any], after: dict[str, Any], pending: str = "") -> str:
    """配置变更后要补的验收级别：'' 不用验收 / readonly / physical。

    - 模拟适配器不设闸门：内置模拟不连任何设备，正式环境本来就禁止模拟执行；
    - 第一次接成真实设备、或换了驱动：动作级。驱动怎么下发、怎么判结论全变了，只读检查证明不了；
    - 其他改动（地址、映射、超时、说明……）：至少只读级。还没补上的动作级要求不因为又改了一次而降级。
    """
    if after.get("kind") != "real":
        return ""
    if before.get("kind") != "real" or before.get("driver") != after.get("driver"):
        return PHYSICAL
    return PHYSICAL if pending == PHYSICAL else READONLY


def acceptance_satisfies(required: str, level: str, ok: bool, simulator: bool) -> bool:
    """这份验收报告能不能清掉闸门。

    报告要全部通过（跳过项不算不通过）。欠动作级时要动作级报告——自报为模拟器的设备例外：
    模拟设备不会造成物理后果、正式环境也不许接入，只读级就够；动作级照样可以手动跑，作为证据存档。
    """
    if not ok:
        return False
    if required == PHYSICAL:
        return level == PHYSICAL or simulator
    return True


def acceptance_reason(station_id: str, required: str, config_version: int) -> str:
    """闸门挡住下发时给人看的原因。"""
    return f"{station_id} 配置 v{config_version} 待接入验收（{LEVEL_LABELS.get(required, required)}）"
