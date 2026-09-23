"""访问上下文。

一个请求的「我是谁、在哪个组织、能做什么」只有这一个来源：由认证推导，
不由请求体里的 organization_id 决定。仓储与服务统一收这个对象，
列表、主键读取、修改、关联、导出、统计、下载与审计查询都用它过滤。
"""
from __future__ import annotations

from dataclasses import dataclass, field

USER = "user"
SERVICE = "service"
SYSTEM = "system"


@dataclass(frozen=True)
class AccessContext:
    org_id: str
    subject_id: str
    subject_kind: str = USER
    subject_label: str = ""
    role: str = ""
    perms: tuple[str, ...] = ()
    # 受限项目模式下可见的项目；空集合表示「组织内可见」
    project_ids: frozenset[str] = frozenset()
    restricted_projects: bool = False
    # 服务身份的授权范围：{"stations": [...], "analysis_tasks": "assigned" | [...]}
    scopes: dict = field(default_factory=dict)
    request_id: str = ""

    @property
    def is_user(self) -> bool:
        return self.subject_kind == USER

    @property
    def is_service(self) -> bool:
        return self.subject_kind == SERVICE

    def has(self, permission: str) -> bool:
        return permission in self.perms

    def with_request_id(self, request_id: str) -> AccessContext:
        return AccessContext(
            org_id=self.org_id, subject_id=self.subject_id, subject_kind=self.subject_kind,
            subject_label=self.subject_label, role=self.role, perms=self.perms,
            project_ids=self.project_ids, restricted_projects=self.restricted_projects,
            scopes=self.scopes, request_id=request_id,
        )


def system_context(org_id: str, label: str = "后台推进器") -> AccessContext:
    """后台进程用的受限系统上下文。

    组织由持久化任务决定，不存在「自动绕过过滤的全局身份」；也不伪装成人类用户，
    否则权限校验会按别人的角色放行。
    """
    return AccessContext(
        org_id=org_id, subject_id="system", subject_kind=SYSTEM, subject_label=label,
        role="system", perms=("batch.control", "batch.schedule", "workflow.advance"),
    )
