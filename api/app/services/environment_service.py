"""环境读数与步骤环境要求的核对。

读数来源：传感器 / 设备经服务身份上报，或人工抄录。核对用区域（工位编号或房间 / 手套箱名）× 指标的最新读数。
开跑检查核对全部步骤；每个设备步骤投递前再核对一次——长批次里环境可能中途变坏，不能只看开工那一刻。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from ..core.clock import as_utc, now
from ..core.config import settings
from ..core.context import AccessContext
from ..core.errors import ValidationFailed
from ..domain import environment as rules
from ..domain.steps import needs_station, normalize, step_id_of
from ..models import Allocation, Batch, EnvironmentReading, User
from .audit_service import AuditService


class EnvironmentService:
    def __init__(self, db: Session, ctx: AccessContext):
        self.db = db
        self.ctx = ctx
        self.audit = AuditService(db, ctx)

    def out(self, row: EnvironmentReading) -> dict:
        label, unit = rules.METRICS.get(row.metric, (row.metric, ""))
        return {
            "id": row.id, "zone": row.zone, "metric": row.metric, "metric_label": label, "value": row.value,
            "unit": row.unit or unit, "source": row.source, "measured_at": row.measured_at.isoformat(timespec="seconds"),
            "recorded_by": row.recorded_by, "note": row.note,
            "age_min": round((now() - row.measured_at).total_seconds() / 60, 1),
            "stale": (now() - row.measured_at).total_seconds() / 60 > settings.environment_max_age_min,
        }

    def record(self, payload: dict, user: User | None, source: str = "manual") -> list[dict]:
        rows = payload.get("readings") or [payload]
        written = []
        moment = now()
        for item in rows:
            zone = str(item.get("zone") or "").strip()
            metric = str(item.get("metric") or "").strip()
            if not zone or not metric:
                raise ValidationFailed("环境读数要有区域和指标")
            measured = as_utc(item.get("measured_at")) or moment
            if (measured - moment).total_seconds() > 300:
                raise ValidationFailed("读数时间超前服务器 5 分钟以上，请先校准时钟", code="device_clock_skew")
            reading = EnvironmentReading(
                org_id=self.ctx.org_id, zone=zone, metric=metric, value=float(item["value"]),
                unit=item.get("unit") or rules.METRICS.get(metric, ("", ""))[1], source=source,
                measured_at=measured, recorded_by=user.display_name if user else (self.ctx.subject_label or "设备"),
                note=item.get("note", ""),
            )
            self.db.add(reading)
            written.append(reading)
        self.db.flush()
        if user is not None:
            self.audit.record(user, "录入环境读数", written[0].zone,
                              detail="；".join(f"{row.zone} {row.metric}={row.value:g}{row.unit}" for row in written)[:300])
        self.db.commit()
        return [self.out(row) for row in written]

    def latest(self, zone: str = "") -> list[dict]:
        """每个区域 × 指标的最新读数。"""
        query = self.db.query(EnvironmentReading).filter(EnvironmentReading.org_id == self.ctx.org_id)
        if zone:
            query = query.filter(EnvironmentReading.zone == zone)
        found: dict[tuple[str, str], EnvironmentReading] = {}
        for row in query.order_by(EnvironmentReading.measured_at.desc()).limit(2000).all():
            found.setdefault((row.zone, row.metric), row)
        return [self.out(row) for row in sorted(found.values(), key=lambda row: (row.zone, row.metric))]

    def history(self, zone: str, metric: str, limit: int = 200) -> list[dict]:
        rows = (
            self.db.query(EnvironmentReading)
            .filter(EnvironmentReading.org_id == self.ctx.org_id, EnvironmentReading.zone == zone,
                    EnvironmentReading.metric == metric)
            .order_by(EnvironmentReading.measured_at.desc()).limit(limit).all()
        )
        return [self.out(row) for row in rows]

    def _reading(self, zone: str, metric: str) -> rules.Reading | None:
        row = (
            self.db.query(EnvironmentReading)
            .filter(EnvironmentReading.org_id == self.ctx.org_id, EnvironmentReading.zone == zone,
                    EnvironmentReading.metric == metric)
            .order_by(EnvironmentReading.measured_at.desc()).first()
        )
        return rules.Reading(row.value, row.measured_at, row.unit) if row is not None else None

    def zone_of(self, batch: Batch, step: dict, index: int, requirement: dict) -> str:
        explicit = str(requirement.get("zone") or "").strip()
        if explicit:
            return explicit
        allocation = (
            self.db.query(Allocation)
            .filter(Allocation.batch_id == batch.id, Allocation.step_index == index, Allocation.kind == "work")
            .first()
        )
        return allocation.station_id if allocation is not None else ""

    def step_problems(self, batch: Batch, index: int, at: datetime | None = None) -> list[str]:
        steps = normalize((batch.recipe_snapshot or {}).get("steps") or [])
        if not 0 <= index < len(steps):
            return []
        step = steps[index]
        moment = at or now()
        problems = []
        for requirement in rules.requirements(step):
            zone = self.zone_of(batch, step, index, requirement)
            reason = rules.check(requirement, zone, self._reading(zone, requirement["metric"]) if zone else None,
                                 moment, settings.environment_max_age_min)
            if reason:
                problems.append(f"第 {index + 1} 步「{step.get('name') or step_id_of(step, index)}」{reason}")
        return problems

    def batch_checks(self, batch: Batch) -> list[str] | None:
        """开跑检查：全部步骤的环境要求。没有任何步骤声明要求时返回 None（不适用）。"""
        steps = normalize((batch.recipe_snapshot or {}).get("steps") or [])
        declared = [index for index, step in enumerate(steps) if rules.requirements(step)]
        if not declared:
            return None
        problems: list[str] = []
        for index in declared:
            problems.extend(self.step_problems(batch, index))
        return problems


def step_environment_issues(step: dict) -> list[str]:
    return rules.requirement_issues(step, needs_zone=not needs_station(step))
