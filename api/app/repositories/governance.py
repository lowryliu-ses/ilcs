from __future__ import annotations

from ..core.clock import now
from ..models import AccessLog, Alarm, AuditEvent, ESignature, IdempotencyKey, User
from .base import Repository, ScopedRepository


class UserRepository(Repository[User]):
    model = User

    def by_username(self, username: str) -> User | None:
        return self.db.query(User).filter(User.username == username).first()

    def by_ids(self, ids: list[str]) -> dict[str, User]:
        if not ids:
            return {}
        rows = self.db.query(User).filter(User.id.in_(list(set(ids)))).all()
        return {row.id: row for row in rows}


class SignatureRepository(Repository[ESignature]):
    model = ESignature


class AuditRepository(ScopedRepository[AuditEvent]):
    model = AuditEvent

    def recent(self, limit: int = 200) -> list[AuditEvent]:
        return list(
            self.query().order_by(AuditEvent.time.desc(), AuditEvent.id.desc()).limit(limit).all()
        )

    def page(self, offset: int, limit: int, target: str | None = None, action: str | None = None):
        query = self.query()
        if target:
            query = query.filter(AuditEvent.target == target)
        if action:
            query = query.filter(AuditEvent.action.like(f"%{action}%"))
        total = query.count()
        rows = (
            query.order_by(AuditEvent.time.desc(), AuditEvent.id.desc())
            .offset(offset).limit(limit).all()
        )
        return list(rows), total

    def for_target(self, target: str) -> list[AuditEvent]:
        return list(
            self.query()
            .filter(AuditEvent.target == target)
            .order_by(AuditEvent.time.desc(), AuditEvent.id.desc())
            .all()
        )


class AccessLogRepository(Repository[AccessLog]):
    model = AccessLog

    def record(
        self, org_id: str, subject: str, subject_kind: str, method: str, path: str,
        code: str, reason: str, request_id: str = "", outcome: str = "denied",
    ) -> AccessLog:
        row = AccessLog(
            org_id=org_id, subject=subject, subject_kind=subject_kind, method=method, path=path,
            code=code, reason=reason, request_id=request_id, outcome=outcome,
        )
        self.db.add(row)
        return row

    def recent(self, limit: int = 100, org_id: str | None = None) -> list[AccessLog]:
        query = self.db.query(AccessLog)
        if org_id is not None:
            query = query.filter(AccessLog.org_id == org_id)
        return list(query.order_by(AccessLog.id.desc()).limit(limit).all())


class AlarmRepository(ScopedRepository[Alarm]):
    model = Alarm

    def list(self) -> list[Alarm]:
        return list(self.query().order_by(Alarm.severity, Alarm.raised_at.desc()).all())

    def open_alarms(self) -> list[Alarm]:
        return list(self.query().filter(Alarm.state == "active").all())

    def for_source(self, source_type: str, source_id: str) -> list[Alarm]:
        return list(
            self.query()
            .filter(Alarm.source_type == source_type, Alarm.source_id == source_id)
            .order_by(Alarm.raised_at.desc())
            .all()
        )

    def unresolved_for_batch(self, batch_id: str) -> list[Alarm]:
        return [
            a for a in self.for_source("batch", batch_id)
            if a.condition_active and a.state != "closed"
        ]

    def active_on_station(self, station_id: str) -> bool:
        return any(a.state == "active" and a.condition_active for a in self.for_source("station", station_id))

    def open_by_condition(self, condition_key: str) -> Alarm | None:
        return (
            self.query()
            .filter(
                Alarm.condition_key == condition_key,
                Alarm.condition_active.is_(True),
                Alarm.state != "closed",
            )
            .first()
        )

    def next_id(self) -> str:
        """现有最大编号 + 1。按行数算在删除或并发下会撞号。"""
        numbers = [
            int(row[0][2:]) for row in self.db.query(Alarm.id).all()
            if row[0].startswith("A-") and row[0][2:].isdigit()
        ]
        return f"A-{max(numbers, default=1040) + 1}"


class IdempotencyRepository(Repository[IdempotencyKey]):
    model = IdempotencyKey

    def find(self, org_id: str, subject: str, action: str, key: str) -> IdempotencyKey | None:
        return (
            self.db.query(IdempotencyKey)
            .filter(
                IdempotencyKey.org_id == org_id,
                IdempotencyKey.subject == subject,
                IdempotencyKey.action == action,
                IdempotencyKey.key == key,
            )
            .first()
        )

    def remember(
        self, org_id: str, subject: str, action: str, key: str, method: str, path: str,
        digest: str, body: dict,
    ) -> None:
        self.db.add(
            IdempotencyKey(
                org_id=org_id, subject=subject, action=action, key=key, method=method, path=path,
                request_digest=digest, status=200, body=body, created_at=now(),
            )
        )
