"""前后逻辑校验规则的维护。规则按指标代码写，指标修订后照样适用；停用而不是删除，历史打标仍能对上规则。"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..core.context import AccessContext
from ..core.errors import NotFound, ValidationFailed
from ..domain import dataquality
from ..models import DataRule, User
from ..repositories.metrics import DataRuleRepository, MetricRepository
from .audit_service import AuditService

EDITABLE = ("name", "left_metric", "op", "right_metric", "right_value", "factor", "offset", "severity", "enabled", "note")
SEVERITY_LABEL = {"flag": "打标交审核", "reject": "整次拒收"}


class DataRuleService:
    def __init__(self, db: Session, ctx: AccessContext):
        self.db = db
        self.ctx = ctx
        self.rules = DataRuleRepository(db, ctx)
        self.metrics = MetricRepository(db, ctx)
        self.audit = AuditService(db, ctx)

    def list(self) -> list[dict]:
        return [self.out(row) for row in self.rules.list()]

    def out(self, rule: DataRule) -> dict:
        right = rule.right_metric or (f"{rule.right_value:g}" if rule.right_value is not None else "")
        scaled = right if not rule.right_metric else (
            f"{right}{f' × {rule.factor:g}' if rule.factor not in (None, 1) else ''}"
            f"{f' + {rule.offset:g}' if rule.offset else ''}"
        )
        return {
            "id": rule.id, "name": rule.name, "left_metric": rule.left_metric, "op": rule.op,
            "right_metric": rule.right_metric, "right_value": rule.right_value, "factor": rule.factor,
            "offset": rule.offset, "severity": rule.severity, "severity_label": SEVERITY_LABEL.get(rule.severity, rule.severity),
            "enabled": rule.enabled, "note": rule.note, "expression": f"{rule.left_metric} {rule.op} {scaled}",
            "row_version": rule.row_version,
        }

    def _validate(self, values: dict) -> None:
        issues = dataquality.rule_issues(
            values.get("left_metric") or "", values.get("op") or "", values.get("right_metric") or "",
            values.get("right_value"), values.get("severity") or "flag",
        )
        codes = {row.code for row in self.metrics.list()}
        for key in ("left_metric", "right_metric"):
            code = values.get(key)
            if code and code not in codes:
                issues.append(f"指标代码 {code} 不存在")
        if not str(values.get("name") or "").strip():
            issues.append("规则名称必填")
        if issues:
            raise ValidationFailed("；".join(issues), code="data_rule_invalid")

    def create(self, payload: dict, user: User) -> dict:
        values = {key: payload.get(key) for key in EDITABLE if key in payload}
        values.setdefault("factor", 1.0)
        values.setdefault("offset", 0.0)
        self._validate(values)
        rule = DataRule(created_by=user.id, **values)
        self.rules.add(rule)
        self.audit.record(user, "新建数据逻辑规则", rule.id, before="—", after="启用" if rule.enabled else "停用",
                          detail=f"{rule.name}：{self.out(rule)['expression']}（{SEVERITY_LABEL.get(rule.severity)}）")
        self.db.commit()
        return self.out(rule)

    def update(self, rule_id: str, changes: dict, user: User) -> dict:
        rule = self.rules.get(rule_id)
        if rule is None:
            raise NotFound("规则不存在")
        self.rules.check_version(rule, changes.pop("row_version", None), "数据逻辑规则")
        before = self.out(rule)["expression"]
        merged = {key: getattr(rule, key) for key in EDITABLE}
        merged.update({key: value for key, value in changes.items() if key in EDITABLE})
        self._validate(merged)
        for key, value in changes.items():
            if key in EDITABLE:
                setattr(rule, key, value)
        self.rules.bump(rule)
        self.audit.record(user, "编辑数据逻辑规则", rule.id, before=before, after=self.out(rule)["expression"],
                          detail=f"{rule.name}；{'启用' if rule.enabled else '停用'}；{SEVERITY_LABEL.get(rule.severity)}")
        self.db.commit()
        return self.out(rule)
