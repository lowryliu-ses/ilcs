"""设备方法目录：起草 → 发布 → 修订 / 退役。

方法是「怎么做」：能力 + 适用型号 + 设备端程序 + 参数缺省值与范围 + 数据输出规则。流程的设备步骤引用
已发布的方法；建批次时方法内容冻结进快照。发布新版本时同编号的旧发布版退役——引用旧版的流程在校验里
报「已退役，请改引用 vN」，要改引用并重新评审，和子流程同一条规矩。

发布走职责分离：起草人不能发布本人起草的方法（测试环境管理员自审开关例外，且留审计）。
"""
from __future__ import annotations

import copy

from sqlalchemy.orm import Session

from ..core.clock import now
from ..core.context import AccessContext
from ..core.errors import NotFound, PermissionDenied, StateConflict, ValidationFailed
from ..domain import methods as rules
from ..domain.access import same_person
from ..domain.steps import normalize
from ..models import DeviceMethod, Recipe, User
from ..repositories.methods import DeviceMethodRepository
from ..repositories.resources import CapabilityRepository
from .audit_service import AuditService
from .identity_service import admin_self_approval

EDITABLE = ("name", "capability_id", "instrument_models", "program", "params", "outputs", "dur_min", "note")
STATE_LABEL = {"draft": "草稿", "released": "已发布", "retired": "已退役"}


def resolver(db: Session, ctx: AccessContext) -> rules.Resolver:
    repo = DeviceMethodRepository(db, ctx)
    latest: dict[str, int] = {}

    def resolve(method_id: str) -> rules.MethodSpec | None:
        method = repo.get(method_id)
        if method is None:
            return None
        if method.code not in latest:
            latest[method.code] = max(
                (row.version for row in repo.versions(method.code) if row.state == "released"), default=0,
            )
        return spec_of(method, latest[method.code])

    return resolve


def spec_of(method: DeviceMethod, latest_version: int = 0) -> rules.MethodSpec:
    return rules.MethodSpec(
        id=method.id, code=method.code, version=int(method.version), name=method.name,
        capability_id=method.capability_id, state=method.state, program=method.program or "",
        instrument_models=tuple(str(value) for value in method.instrument_models or []),
        params=copy.deepcopy(method.params or {}), outputs=tuple(copy.deepcopy(method.outputs or [])),
        dur_min=float(method.dur_min or 0), latest_version=latest_version,
    )


class MethodService:
    def __init__(self, db: Session, ctx: AccessContext):
        self.db = db
        self.ctx = ctx
        self.methods = DeviceMethodRepository(db, ctx)
        self.capabilities = CapabilityRepository(db, ctx)
        self.audit = AuditService(db, ctx)

    # ---------- 读 ----------

    def list(self, state: str | None = None, capability_id: str | None = None) -> list[dict]:
        usage = self._usage()
        return [self.out(row, usage) for row in self.methods.list(state, capability_id)]

    def get(self, method_id: str) -> dict:
        method = self._require(method_id)
        out = self.out(method, self._usage())
        out["versions"] = [
            {"id": row.id, "version": row.version, "state": row.state, "state_label": STATE_LABEL.get(row.state, row.state),
             "released_at": row.released_at.isoformat(timespec="seconds") if row.released_at else None}
            for row in self.methods.versions(method.code)
        ]
        return out

    def out(self, method: DeviceMethod, usage: dict[str, list[dict]] | None = None) -> dict:
        issues = rules.definition_issues(
            method.capability_id, method.params or {}, method.outputs or [], self.capabilities.specs(),
            method.name, method.dur_min,
        )
        return {
            "id": method.id, "code": method.code, "version": method.version, "name": method.name,
            "capability_id": method.capability_id,
            "capability_name": (self.capabilities.specs().get(method.capability_id) or {}).get("name", method.capability_id),
            "instrument_models": list(method.instrument_models or []), "program": method.program,
            "params": method.params or {}, "outputs": method.outputs or [], "dur_min": method.dur_min,
            "state": method.state, "state_label": STATE_LABEL.get(method.state, method.state),
            "note": method.note, "created_by": method.created_by,
            "created_at": method.created_at.isoformat(timespec="seconds") if method.created_at else None,
            "released_by": method.released_by,
            "released_at": method.released_at.isoformat(timespec="seconds") if method.released_at else None,
            "issues": issues, "row_version": method.row_version,
            "used_by": (usage or {}).get(method.id, []),
        }

    def _usage(self) -> dict[str, list[dict]]:
        """哪些流程引用了哪条方法（按方法行，即具体版本）。"""
        usage: dict[str, list[dict]] = {}
        for recipe in self.db.query(Recipe).filter(Recipe.org_id == self.ctx.org_id).all():
            if recipe.state == "retired":
                continue
            for ref in set(rules.references(normalize(recipe.steps or []))):
                usage.setdefault(ref, []).append(
                    {"id": recipe.id, "name": recipe.name, "version": recipe.version, "state": recipe.state},
                )
        return usage

    def _require(self, method_id: str) -> DeviceMethod:
        method = self.methods.get(method_id)
        if method is None:
            raise NotFound("设备方法不存在")
        return method

    # ---------- 写 ----------

    def create(self, payload: dict, user: User) -> dict:
        if not str(payload.get("name") or "").strip():
            raise ValidationFailed("方法名称必填")
        if payload.get("capability_id") not in self.capabilities.specs():
            raise ValidationFailed(f"能力 {payload.get('capability_id')} 不存在")
        method = DeviceMethod(
            code=self.methods.next_code(), version=1, state="draft", created_by=user.id,
            **{key: payload.get(key) for key in EDITABLE if payload.get(key) is not None},
        )
        method.instrument_models = _clean_models(method.instrument_models or [])
        self.methods.add(method)
        self.audit.record(user, "起草设备方法", method.id, before="—", after="草稿",
                          detail=f"{method.code} v1 {method.name}；能力 {method.capability_id}",
                          object_version=method.row_version)
        self.db.commit()
        return self.get(method.id)

    def update(self, method_id: str, changes: dict, user: User) -> dict:
        method = self._require(method_id)
        self.methods.check_version(method, changes.pop("row_version", None), "设备方法")
        if method.state != "draft":
            raise StateConflict("只有草稿可以编辑；已发布的方法请新建修订版本", code="method_not_editable")
        unknown = [key for key in changes if key not in EDITABLE]
        if unknown:
            raise ValidationFailed(f"不能修改：{'、'.join(unknown)}")
        if "capability_id" in changes and changes["capability_id"] not in self.capabilities.specs():
            raise ValidationFailed(f"能力 {changes['capability_id']} 不存在")
        if "instrument_models" in changes:
            changes["instrument_models"] = _clean_models(changes["instrument_models"] or [])
        for key, value in changes.items():
            setattr(method, key, value)
        self.methods.bump(method)
        self.audit.record(user, "编辑设备方法", method.id, object_version=method.row_version,
                          detail=f"{method.code} v{method.version}：{'、'.join(changes) or '无改动'}")
        self.db.commit()
        return self.get(method.id)

    def release(self, method_id: str, expected_version: int | None, user: User) -> dict:
        method = self._require(method_id)
        self.methods.check_version(method, expected_version, "设备方法")
        if method.state != "draft":
            raise StateConflict("只有草稿可以发布")
        issues = rules.definition_issues(
            method.capability_id, method.params or {}, method.outputs or [], self.capabilities.specs(),
            method.name, method.dur_min,
        )
        if issues:
            raise StateConflict(
                "方法定义不完整，不能发布", {"blocked": [{"key": "definition", "label": issue} for issue in issues]},
                code="method_invalid",
            )
        if same_person(method.created_by, user.id) and not admin_self_approval(
            self.db, self.ctx, user, method.id, "发布本人起草的设备方法",
        ):
            raise PermissionDenied("不能发布本人起草的设备方法：请由另一位有发布权限的人发布", code="self_approval")
        retired = []
        for other in self.methods.versions(method.code):
            if other.id != method.id and other.state == "released":
                other.state = "retired"
                self.methods.bump(other)
                retired.append(f"v{other.version}")
        method.state = "released"
        method.released_by = user.id
        method.released_at = now()
        self.methods.bump(method)
        self.audit.record(
            user, "发布设备方法", method.id, before="草稿", after="已发布", object_version=method.row_version,
            detail=f"{method.code} v{method.version} {method.name}"
            + (f"；同编号旧版本 {'、'.join(retired)} 退役，引用旧版本的流程需改引用并重新评审" if retired else ""),
        )
        self.db.commit()
        return self.get(method.id)

    def revise(self, method_id: str, user: User) -> dict:
        source = self._require(method_id)
        versions = self.methods.versions(source.code)
        if any(row.state == "draft" for row in versions):
            raise StateConflict(f"{source.code} 已有一个修订草稿，请先完成或删除它", code="method_draft_exists")
        method = DeviceMethod(
            code=source.code, version=max(row.version for row in versions) + 1, state="draft", created_by=user.id,
            **{key: copy.deepcopy(getattr(source, key)) for key in EDITABLE},
        )
        self.methods.add(method)
        self.audit.record(user, "新建设备方法修订", method.id, before=f"v{source.version}", after=f"v{method.version} 草稿",
                          detail=f"{method.code} 从 v{source.version} 复制", object_version=method.row_version)
        self.db.commit()
        return self.get(method.id)

    def retire(self, method_id: str, expected_version: int | None, user: User) -> dict:
        method = self._require(method_id)
        self.methods.check_version(method, expected_version, "设备方法")
        if method.state != "released":
            raise StateConflict("只有已发布的方法可以退役")
        method.state = "retired"
        self.methods.bump(method)
        users = self._usage().get(method.id, [])
        self.audit.record(
            user, "退役设备方法", method.id, before="已发布", after="已退役", object_version=method.row_version,
            detail=f"{method.code} v{method.version}；引用它的流程 {len(users)} 个，需改引用后才能再建批次",
        )
        self.db.commit()
        return self.get(method.id)

    def delete(self, method_id: str, user: User) -> dict:
        method = self._require(method_id)
        if method.state != "draft":
            raise StateConflict("只有草稿可以删除；发布过的方法只能退役")
        if self._usage().get(method.id):
            raise StateConflict("有流程引用这份草稿，不能删除")
        self.audit.record(user, "删除设备方法草稿", method.id, before="草稿", after="已删除",
                          detail=f"{method.code} v{method.version}")
        self.db.delete(method)
        self.db.commit()
        return {"id": method_id, "deleted": True}


def _clean_models(models: list) -> list[str]:
    return list(dict.fromkeys(str(value).strip() for value in models if str(value).strip()))
