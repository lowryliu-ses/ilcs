"""批注：方案、SOP 版本、方法、报告版本上的评审意见。

批注记下它针对的对象版本（方案版本号、方法版本等）：对象修订之后，旧批注仍然知道自己说的是哪一版。
能看到对象的人就能批注；解决批注的是作者本人或有该对象编辑 / 审批权限的人。批注不删除。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..core.clock import now
from ..core.context import AccessContext
from ..core.errors import NotFound, PermissionDenied, StateConflict
from ..models import Comment, Plan, Recipe, ReportVersion, SopVersion, User
from .audit_service import AuditService

TARGETS = {
    "plan": (Plan, ("plan.edit", "plan.approve"), lambda row: f"v{row.version}"),
    "recipe": (Recipe, ("recipe.edit", "recipe.approve"), lambda row: f"v{row.version}"),
    "sop_version": (SopVersion, ("sop.edit", "sop.approve"), lambda row: row.version),
    "report_version": (ReportVersion, ("report.edit", "report.approve"), lambda row: f"v{row.version}"),
}


class CommentService:
    def __init__(self, db: Session, ctx: AccessContext):
        self.db = db
        self.ctx = ctx
        self.audit = AuditService(db, ctx)

    def _target(self, target_type: str, target_id: str):
        spec = TARGETS.get(target_type)
        if spec is None:
            raise NotFound("不支持的批注对象")
        row = self.db.get(spec[0], target_id)
        if row is None or (getattr(row, "org_id", "") or self.ctx.org_id) != self.ctx.org_id:
            raise NotFound("批注对象不存在")
        return spec, row

    def out(self, comment: Comment) -> dict:
        return {
            "id": comment.id, "target_type": comment.target_type, "target_id": comment.target_id,
            "target_version": comment.target_version, "anchor": comment.anchor, "body": comment.body,
            "author_id": comment.author_id, "author_name": comment.author_name,
            "created_at": comment.created_at.isoformat(timespec="seconds"),
            "resolved": comment.resolved, "resolved_by": comment.resolved_by,
            "resolved_at": comment.resolved_at.isoformat(timespec="seconds") if comment.resolved_at else None,
        }

    def list(self, target_type: str, target_id: str) -> list[dict]:
        self._target(target_type, target_id)
        rows = (
            self.db.query(Comment)
            .filter(Comment.org_id == self.ctx.org_id, Comment.target_type == target_type, Comment.target_id == target_id)
            .order_by(Comment.created_at)
            .all()
        )
        return [self.out(row) for row in rows]

    def create(self, payload: dict, user: User) -> dict:
        spec, target = self._target(payload["target_type"], payload["target_id"])
        comment = Comment(
            org_id=self.ctx.org_id, target_type=payload["target_type"], target_id=payload["target_id"],
            target_version=str(spec[2](target)), anchor=payload.get("anchor", ""), body=payload["body"].strip(),
            author_id=user.id, author_name=user.display_name,
        )
        self.db.add(comment)
        self.db.flush()
        self.audit.record(user, "添加批注", payload["target_id"], after=comment.target_version,
                          detail=(comment.anchor + "：" if comment.anchor else "") + comment.body[:120])
        self.db.commit()
        return self.out(comment)

    def resolve(self, comment_id: str, user: User) -> dict:
        comment = self.db.get(Comment, comment_id)
        if comment is None or comment.org_id != self.ctx.org_id:
            raise NotFound("批注不存在")
        if comment.resolved:
            raise StateConflict("批注已解决")
        spec, _ = self._target(comment.target_type, comment.target_id)
        if comment.author_id != user.id and not any(self.ctx.has(permission) for permission in spec[1]):
            raise PermissionDenied("只有批注作者或对象的编辑 / 审批人可以标记解决")
        comment.resolved = True
        comment.resolved_by = user.display_name
        comment.resolved_at = now()
        self.audit.record(user, "解决批注", comment.target_id, before="待处理", after="已解决", detail=comment.body[:120])
        self.db.commit()
        return self.out(comment)
