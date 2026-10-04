from __future__ import annotations

from ...repositories.base import ScopedRepository
from .models import FormulationTemplate


class FormulationTemplateRepository(ScopedRepository[FormulationTemplate]):
    model = FormulationTemplate

    def list(self, state: str | None = None) -> list[FormulationTemplate]:
        query = self.query()
        if state:
            query = query.filter(FormulationTemplate.state == state)
        return list(query.order_by(FormulationTemplate.code).all())

    def by_code(self, code: str) -> FormulationTemplate | None:
        return self.query().filter(FormulationTemplate.code == code).first()
