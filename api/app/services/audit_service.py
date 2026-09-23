from __future__ import annotations

from sqlalchemy.orm import Session

from ..core.context import AccessContext
from ..models import AuditEvent, User
from ..repositories.governance import AuditRepository

DEVICE_USER = "设备事件"


class AuditService:
    """所有写路径都经过这里。审计只追加，绝不更新。

    审计带组织归属与对象版本：审计查询也在访问范围内，跨组织读不到；
    版本让「批准的是哪一版」在事后可核对。
    """

    def __init__(self, db: Session, ctx: AccessContext | None = None):
        self.db = db
        self.ctx = ctx
        self.repository = AuditRepository(db, ctx)

    def record(
        self,
        user: User | None,
        action: str,
        target: str,
        *,
        sign: bool = False,
        meaning: str = "",
        before: str = "",
        after: str = "",
        detail: str = "",
        signature_id: str = "",
        command_id: str = "",
        checkpoint_id: str = "",
        request_id: str = "",
        object_version: int = 0,
        org_id: str = "",
    ) -> AuditEvent:
        subject_label = DEVICE_USER
        role = "device"
        user_id = ""
        if user is not None:
            subject_label, role, user_id = user.display_name, user.role, user.id
        elif self.ctx is not None and not self.ctx.is_user:
            subject_label, role = self.ctx.subject_label or "系统", self.ctx.role
            user_id = self.ctx.subject_id
        event = AuditEvent(
            org_id=org_id or (self.ctx.org_id if self.ctx else ""),
            user=subject_label,
            user_id=user_id,
            role=role,
            action=action,
            target=target,
            object_version=object_version,
            sign=sign,
            meaning=meaning,
            before=before,
            after=after,
            detail=detail,
            signature_id=signature_id,
            command_id=command_id,
            checkpoint_id=checkpoint_id,
            request_id=request_id or (self.ctx.request_id if self.ctx else ""),
        )
        self.db.add(event)
        return event

    def recent(self, limit: int = 200) -> list[AuditEvent]:
        return self.repository.recent(limit)

    def for_target(self, target: str) -> list[AuditEvent]:
        return self.repository.for_target(target)

    def page(self, offset: int, limit: int, target: str | None = None, action: str | None = None):
        return self.repository.page(offset, limit, target, action)
