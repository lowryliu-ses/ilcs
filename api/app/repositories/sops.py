from __future__ import annotations

from datetime import datetime

from ..models import Sop, SopAck, SopVersion
from .base import Repository, ScopedRepository


class SopRepository(ScopedRepository[Sop]):
    model = Sop

    def list(self) -> list[Sop]:
        return list(self.query().order_by(Sop.code).all())

    def by_code(self, code: str) -> Sop | None:
        return self.query().filter(Sop.code == code).first()

    def categories(self) -> list[str]:
        return sorted({row.category for row in self.query().all() if row.category})


def is_effective(version: SopVersion, at: datetime) -> bool:
    """已发布、已到生效时间、未到失效时间。被取代的版本失效时间就是新版本的生效时间。"""
    return (
        version.state == "published"
        and version.effective_from is not None
        and version.effective_from <= at
        and (version.effective_to is None or version.effective_to > at)
    )


class SopVersionRepository(ScopedRepository[SopVersion]):
    model = SopVersion

    def for_sop(self, sop_id: str) -> list[SopVersion]:
        return list(
            self.query().filter(SopVersion.sop_id == sop_id).order_by(SopVersion.version).all()
        )

    def effective(self, sop_id: str, at: datetime) -> SopVersion | None:
        """生效且可用的版本。已退役、已失效（含被取代）与未到生效时间的都不算。"""
        candidates = [row for row in self.for_sop(sop_id) if is_effective(row, at)]
        if not candidates:
            return None
        return sorted(candidates, key=lambda row: row.effective_from)[-1]

    def published(self) -> list[SopVersion]:
        return list(self.query().filter(SopVersion.state == "published").all())

    def page(self, offset: int, limit: int, state: str | None = None, category: str | None = None):
        query = self.query()
        if state:
            query = query.filter(SopVersion.state == state)
        if category:
            query = query.join(Sop, Sop.id == SopVersion.sop_id).filter(Sop.category == category)
        total = query.count()
        rows = query.order_by(SopVersion.created_at.desc()).offset(offset).limit(limit).all()
        return list(rows), total


class SopAckRepository(Repository[SopAck]):
    model = SopAck

    def find(self, sop_version_id: str, person_id: str) -> SopAck | None:
        return (
            self.db.query(SopAck)
            .filter(SopAck.sop_version_id == sop_version_id, SopAck.person_id == person_id)
            .first()
        )

    def for_version(self, sop_version_id: str) -> list[SopAck]:
        return list(self.db.query(SopAck).filter(SopAck.sop_version_id == sop_version_id).all())
