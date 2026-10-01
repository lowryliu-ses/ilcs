"""报告模板：内置模板（代码里，只读）与组织自己的模板（入库、按版本）。

组织模板：起草 → 发布（发布人不能是起草人，发布后冻结）→ 出了新版本或停用就退役。改已发布的模板要「修订」出新版本草稿，
新版本发布时旧版本退役。报告生成、换模板、重新取数都按模板键取「最新的已发布版本」，章节清单写进报告内容快照——
之后模板怎么改，已有报告都不变。
"""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy.orm import Session

from ..core.clock import now
from ..core.context import AccessContext
from ..core.errors import NotFound, PermissionDenied, StateConflict, ValidationFailed
from ..domain import report_templates as rules
from ..models import ReportTemplate, User
from ..repositories.reports import ReportTemplateRepository
from .audit_service import AuditService

STATE_LABEL = {"draft": "草稿", "released": "已发布", "retired": "已退役"}


class ReportTemplateService:
    def __init__(self, db: Session, ctx: AccessContext):
        self.db = db
        self.ctx = ctx
        self.templates = ReportTemplateRepository(db, ctx)
        self.audit = AuditService(db, ctx)

    # ---------- 读 ----------

    def out(self, row: ReportTemplate) -> dict[str, Any]:
        return {
            "id": row.id, "key": row.key, "version": row.version, "name": row.name, "description": row.description,
            "state": row.state, "state_label": STATE_LABEL.get(row.state, row.state), "builtin": False,
            "sections": [
                {**section, "title": section.get("title") or rules.SECTION_TITLES.get(section["key"], section["key"])}
                for section in row.sections or []
            ],
            "created_by": row.created_by, "created_by_name": row.created_by_name, "released_by_name": row.released_by_name,
            "created_at": row.created_at.isoformat(timespec="seconds") if row.created_at else None,
            "released_at": row.released_at.isoformat(timespec="seconds") if row.released_at else None,
            "row_version": row.row_version,
        }

    def listing(self) -> dict[str, Any]:
        """管理页：内置模板（只读）+ 组织模板的全部版本，外加可选的内置章节。"""
        return {
            "builtin": [{**row, "builtin": True} for row in rules.catalog()],
            "custom": [self.out(row) for row in self.templates.list()],
            "sections": rules.builtin_sections(),
        }

    def catalog(self) -> list[dict[str, Any]]:
        """生成报告时可选的模板：内置的，加上组织模板各键最新的已发布版本。"""
        rows = [{**row, "builtin": True} for row in rules.catalog()]
        latest: dict[str, ReportTemplate] = {}
        for row in self.templates.list():
            if row.state == "released":
                latest[row.key] = row
        for row in latest.values():
            item = self.out(row)
            rows.append({"key": row.key, "name": row.name, "version": str(row.version), "description": row.description,
                         "sections": [{"key": section["key"], "title": section["title"]} for section in item["sections"]],
                         "builtin": False})
        return rows

    def resolve(self, key: str | None) -> dict[str, Any]:
        """报告用的模板快照：没给键用缺省内置模板；内置键取内置的；组织模板取该键最新的已发布版本，
        没有已发布版本或键不存在都明确拒绝，不悄悄换成别的模板。"""
        if key and key not in rules.BUILTIN_KEYS:
            versions = self.templates.for_key(key)
            released = [row for row in versions if row.state == "released"]
            if released:
                row = released[-1]
                return rules.snapshot(row.key, row.name, row.version, row.sections or [])
            if versions:
                raise ValidationFailed(f"报告模板 {key} 没有在用的已发布版本，不能用来出报告", code="report_template_unreleased")
            raise ValidationFailed(f"报告模板 {key} 不存在", code="report_template_unknown")
        base = rules.template(key)
        return {**base, "titles": {}, "texts": {}}

    @staticmethod
    def version_label(snapshot: dict[str, Any]) -> str:
        return f"{snapshot['key']}-{snapshot['version']}"

    def _require(self, template_id: str) -> ReportTemplate:
        row = self.templates.get(template_id)
        if row is None:
            raise NotFound("报告模板不存在")
        return row

    def _validated(self, sections: Any) -> list[dict]:
        problems = rules.section_issues(sections)
        if problems:
            raise ValidationFailed(f"报告模板有 {len(problems)} 处问题：{problems[0]}", {"problems": problems},
                                   code="report_template_invalid")
        return rules.clean_sections(sections)

    # ---------- 写 ----------

    def create(self, payload: dict[str, Any], user: User) -> dict[str, Any]:
        """新建组织模板草稿。`copy_from` 可以是内置模板键或组织模板 id：以它的章节为起点。"""
        key = str(payload.get("key") or "").strip()
        if not re.fullmatch(rules.KEY_PATTERN, key):
            raise ValidationFailed("模板键 2–32 位，小写字母开头，只含小写字母、数字、下划线、横线", code="report_template_key_invalid")
        if key in rules.BUILTIN_KEYS:
            raise StateConflict(f"{key} 是内置模板的键，换一个", code="report_template_key_taken")
        if self.templates.for_key(key):
            raise StateConflict(f"报告模板 {key} 已存在；要改它请修订出新版本", code="report_template_key_taken")
        name = str(payload.get("name") or "").strip()
        if not name:
            raise ValidationFailed("报告模板要有名称", code="report_template_invalid")
        sections = payload.get("sections")
        source = payload.get("copy_from") or ""
        if not sections and source:
            if source in rules.BUILTIN_KEYS:
                sections = [{"key": section} for section in rules.TEMPLATES[source]["sections"]]
            else:
                sections = list(self._require(source).sections or [])
        row = ReportTemplate(
            org_id=self.ctx.org_id, key=key, version=1, name=name, description=str(payload.get("description") or ""),
            sections=self._validated(sections), state="draft", created_by=user.id, created_by_name=user.display_name,
            created_at=now(), updated_at=now(),
        )
        self.templates.add(row)
        self.audit.record(user, "新建报告模板", row.id, before="—", after=f"{key} v1 草稿", detail=name,
                          object_version=row.row_version)
        self.db.commit()
        return self.out(row)

    def update(self, template_id: str, changes: dict[str, Any], expected: int | None, user: User) -> dict[str, Any]:
        row = self._require(template_id)
        self.templates.check_version(row, expected, "报告模板")
        if row.state != "draft":
            raise StateConflict(f"{STATE_LABEL.get(row.state)}的报告模板不能改；要改请修订出新版本", code="report_template_frozen")
        if "name" in changes and changes["name"] is not None:
            name = str(changes["name"]).strip()
            if not name:
                raise ValidationFailed("报告模板要有名称", code="report_template_invalid")
            row.name = name
        if changes.get("description") is not None:
            row.description = str(changes["description"])
        if changes.get("sections") is not None:
            row.sections = self._validated(changes["sections"])
        row.updated_at = now()
        self.templates.bump(row)
        self.audit.record(user, "修改报告模板", row.id, after=f"{row.key} v{row.version}",
                          detail="、".join(key for key in ("name", "description", "sections") if changes.get(key) is not None),
                          object_version=row.row_version)
        self.db.commit()
        return self.out(row)

    def release(self, template_id: str, expected: int | None, user: User) -> dict[str, Any]:
        """发布：冻结章节清单；同一个键原来已发布的版本退役。起草人不能发布本人起草的模板。"""
        row = self._require(template_id)
        self.templates.check_version(row, expected, "报告模板")
        if row.state != "draft":
            raise StateConflict("只有草稿可以发布", code="report_template_frozen")
        if row.created_by == user.id:
            raise PermissionDenied("起草人不能发布本人起草的报告模板", code="self_approval")
        self._validated(row.sections or [])
        for other in self.templates.for_key(row.key):
            if other.id != row.id and other.state == "released":
                other.state = "retired"
                other.updated_at = now()
                self.templates.bump(other)
        row.state = "released"
        row.released_by, row.released_by_name, row.released_at = user.id, user.display_name, now()
        row.updated_at = now()
        self.templates.bump(row)
        self.audit.record(user, "发布报告模板", row.id, before="草稿", after=f"{row.key} v{row.version} 已发布",
                          detail=row.name, object_version=row.row_version)
        self.db.commit()
        return self.out(row)

    def revise(self, template_id: str, user: User) -> dict[str, Any]:
        """从已发布的版本修订出新版本草稿（同一个键只能有一份草稿）。"""
        row = self._require(template_id)
        if row.state != "released":
            raise StateConflict("只有已发布的报告模板可以修订", code="report_template_not_released")
        versions = self.templates.for_key(row.key)
        if any(other.state == "draft" for other in versions):
            raise StateConflict(f"报告模板 {row.key} 已有一份草稿，先改那份", code="report_template_draft_exists")
        draft = ReportTemplate(
            org_id=self.ctx.org_id, key=row.key, version=max(other.version for other in versions) + 1, name=row.name,
            description=row.description, sections=list(row.sections or []), state="draft", created_by=user.id,
            created_by_name=user.display_name, created_at=now(), updated_at=now(),
        )
        self.templates.add(draft)
        self.audit.record(user, "修订报告模板", draft.id, before=f"v{row.version}", after=f"v{draft.version} 草稿",
                          detail=row.key, object_version=draft.row_version)
        self.db.commit()
        return self.out(draft)

    def retire(self, template_id: str, expected: int | None, user: User) -> dict[str, Any]:
        """停用：之后不能再用它出新报告；已有报告不受影响。草稿直接退役等于丢弃。"""
        row = self._require(template_id)
        self.templates.check_version(row, expected, "报告模板")
        if row.state == "retired":
            raise StateConflict("报告模板已退役", code="report_template_retired")
        before = STATE_LABEL.get(row.state, row.state)
        row.state = "retired"
        row.updated_at = now()
        self.templates.bump(row)
        self.audit.record(user, "退役报告模板", row.id, before=before, after="已退役", detail=f"{row.key} v{row.version}",
                          object_version=row.row_version)
        self.db.commit()
        return self.out(row)
