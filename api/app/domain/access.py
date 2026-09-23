"""访问范围判据。纯函数，不碰数据库。

首期默认「组织内可见、按角色控制动作」；启用受限项目时，通过项目成员关系控制访问，
关联样本、结果与报告继承项目访问范围。跨组织隔离在任何模式下都生效。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ScopeDecision:
    visible: bool
    reason: str = ""


def visible(
    ctx_org_id: str,
    object_org_id: str,
    object_project_id: str = "",
    restricted_projects: bool = False,
    project_ids: frozenset[str] = frozenset(),
) -> ScopeDecision:
    if not object_org_id or object_org_id != ctx_org_id:
        return ScopeDecision(False, "对象不在当前组织范围内")
    if restricted_projects and object_project_id and object_project_id not in project_ids:
        return ScopeDecision(False, "对象属于未授权的受限项目")
    return ScopeDecision(True)


def service_may_use_station(scopes: dict, station_id: str) -> bool:
    allowed = scopes.get("stations")
    if allowed in (None, "all"):
        return False  # 未声明范围的服务身份不获得任何设备权限
    return station_id in set(allowed)


def service_may_submit_task(scopes: dict, task_id: str) -> bool:
    allowed = scopes.get("analysis_tasks")
    if allowed == "all":
        return True
    if isinstance(allowed, list):
        return task_id in set(allowed)
    return False


def same_person(a: str, b: str) -> bool:
    """职责分离判据：按稳定用户 ID 比较，显示名称变化不改变历史身份。"""
    return bool(a) and bool(b) and a == b


def service_may_propose(scopes: dict, plan_id: str) -> bool:
    """外部优化器只能向授权过的方案提交提案；未声明范围就没有权限。"""
    allowed = (scopes or {}).get("plan_proposals")
    if allowed == "all":
        return True
    return isinstance(allowed, list) and plan_id in set(allowed)
