"""指标定义与版本。已被结果引用的版本不可原位修改，只能建新版本。"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..core.context import AccessContext
from ..core.errors import NotFound, StateConflict, ValidationFailed
from ..domain.metrics import VALUE_TYPES, validate_rules
from ..models import MetricDefinition, User
from ..repositories.metrics import MetricRepository
from .audit_service import AuditService


class MetricService:
    def __init__(self, db: Session, ctx: AccessContext):
        self.db = db
        self.ctx = ctx
        self.metrics = MetricRepository(db, ctx)
        self.audit = AuditService(db, ctx)

    def out(self, metric: MetricDefinition) -> dict:
        referenced = self.metrics.referenced(metric.id)
        return {
            "id": metric.id,
            "code": metric.code,
            "name": metric.name,
            "version": metric.version,
            "value_type": metric.value_type,
            "unit": metric.unit,
            "method_version": metric.method_version,
            "sample_types": metric.sample_types or [],
            "rules": metric.rules or {},
            "state": metric.state,
            "referenced_by": referenced,
            # 只有数值指标进入数值统计
            "numeric": metric.value_type == "number",
            "editable": referenced == 0,
            "created_at": metric.created_at.isoformat(timespec="seconds"),
        }

    def list(self, only_active: bool = False) -> list[dict]:
        rows = self.metrics.active() if only_active else self.metrics.list()
        return [self.out(row) for row in rows]

    def create(self, payload: dict, user: User) -> dict:
        code = (payload.get("code") or "").strip()
        if not code:
            raise ValidationFailed("指标代码必填")
        version = (payload.get("version") or "v1").strip()
        if self.metrics.query().filter_by(code=code, version=version).first():
            raise StateConflict(f"指标 {code} 的版本 {version} 已存在")
        value_type = payload.get("value_type", "number")
        if value_type not in VALUE_TYPES:
            raise ValidationFailed(f"值类型只能是 {'、'.join(VALUE_TYPES)}")
        problems = validate_rules(value_type, payload.get("rules") or {})
        if problems:
            raise ValidationFailed("；".join(problems))
        if value_type == "number" and not (payload.get("unit") or "").strip():
            raise ValidationFailed("数值指标必须有标准单位")
        metric = MetricDefinition(
            id=f"METRIC-{code}-{version}", org_id=self.ctx.org_id, code=code,
            name=payload["name"], version=version, value_type=value_type,
            unit=payload.get("unit", ""), method_version=payload.get("method_version", ""),
            sample_types=payload.get("sample_types") or [], rules=payload.get("rules") or {},
        )
        self.metrics.add(metric)
        self.audit.record(
            user, "登记指标定义", metric.id, before="—", after=f"{code} {version}",
            detail=f"{metric.name}；{value_type}；单位 {metric.unit or '—'}；允许范围是校验规则，不代表质量合格",
        )
        self.db.commit()
        return self.out(metric)

    def revise(self, metric_id: str, payload: dict, user: User) -> dict:
        """修订产生新版本。已被引用的定义不能原地改。"""
        source = self.metrics.get(metric_id)
        if not source:
            raise NotFound("指标定义不存在")
        version = (payload.get("version") or "").strip()
        if not version or version == source.version:
            raise ValidationFailed("修订必须指定新的版本号")
        return self.create(
            {
                "code": source.code,
                "name": payload.get("name", source.name),
                "version": version,
                "value_type": payload.get("value_type", source.value_type),
                "unit": payload.get("unit", source.unit),
                "method_version": payload.get("method_version", source.method_version),
                "sample_types": payload.get("sample_types", source.sample_types),
                "rules": payload.get("rules", source.rules),
            },
            user,
        )

    def update(self, metric_id: str, changes: dict, user: User) -> dict:
        metric = self.metrics.get(metric_id)
        if not metric:
            raise NotFound("指标定义不存在")
        referenced = self.metrics.referenced(metric.id)
        if referenced:
            raise StateConflict(
                f"该指标版本已被 {referenced} 条结果引用，不能原位修改",
                {"blocked": [{"key": "metric", "label": "请用修订建立新版本"}]},
                code="metric_referenced",
            )
        before = {key: getattr(metric, key) for key in changes}
        if "value_type" in changes and changes["value_type"] not in VALUE_TYPES:
            raise ValidationFailed(f"值类型只能是 {'、'.join(VALUE_TYPES)}")
        problems = validate_rules(
            changes.get("value_type", metric.value_type), changes.get("rules", metric.rules) or {}
        )
        if problems:
            raise ValidationFailed("；".join(problems))
        for key, value in changes.items():
            setattr(metric, key, value)
        self.audit.record(
            user, "编辑指标定义", metric.id,
            detail="；".join(f"{k}: {before[k]} → {v}" for k, v in changes.items()),
        )
        self.db.commit()
        return self.out(metric)

    def retire(self, metric_id: str, user: User) -> dict:
        metric = self.metrics.get(metric_id)
        if not metric:
            raise NotFound("指标定义不存在")
        metric.state = "retired"
        self.audit.record(
            user, "停用指标定义", metric.id, before="active", after="retired",
            detail="新检测任务不能再要求它；历史结果仍引用该版本",
        )
        self.db.commit()
        return self.out(metric)
