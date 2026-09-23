from __future__ import annotations

from datetime import datetime

from sqlalchemy import or_

from ..models import Person, Qualification
from .base import ScopedRepository


class PersonRepository(ScopedRepository[Person]):
    model = Person

    def list(self) -> list[Person]:
        return list(self.query().order_by(Person.code).all())

    def page(self, offset: int, limit: int, keyword: str = "", state: str | None = None):
        query = self.query()
        if keyword:
            like = f"%{keyword}%"
            query = query.filter(or_(Person.name.like(like), Person.code.like(like)))
        if state:
            query = query.filter(Person.employment_state == state)
        total = query.count()
        rows = query.order_by(Person.code).offset(offset).limit(limit).all()
        return list(rows), total

    def by_user(self, user_id: str) -> Person | None:
        if not user_id:
            return None
        return self.query().filter(Person.user_id == user_id).first()

    def by_code(self, code: str) -> Person | None:
        return self.query().filter(Person.code == code).first()


class QualificationRepository(ScopedRepository[Qualification]):
    model = Qualification

    def for_person(self, person_id: str) -> list[Qualification]:
        return list(
            self.query()
            .filter(Qualification.person_id == person_id)
            .order_by(Qualification.effective_from.desc())
            .all()
        )

    def live_for_person(self, person_id: str, at: datetime) -> list[Qualification]:
        """在给定时刻有效的资质。到期、撤销一律不算。"""
        return [
            row for row in self.for_person(person_id)
            if row.revoked_at is None
            and row.effective_from <= at
            and (row.expires_at is None or row.expires_at >= at)
        ]

    def expiring(self, before: datetime) -> list[Qualification]:
        return list(
            self.query()
            .filter(
                Qualification.revoked_at.is_(None),
                Qualification.expires_at.isnot(None),
                Qualification.expires_at <= before,
            )
            .order_by(Qualification.expires_at)
            .all()
        )
