from __future__ import annotations

from ..models import FormulationTemplate
from .base import ScopedRepository


class FormulationTemplateRepository(ScopedRepository[FormulationTemplate]):
    model = FormulationTemplate

    def list(self, state: str | None = None) -> list[FormulationTemplate]:
        query = self.query()
        if state:
            query = query.filter(FormulationTemplate.state == state)
        return list(query.order_by(FormulationTemplate.code).all())

    def by_code(self, code: str) -> FormulationTemplate | None:
        return self.query().filter(FormulationTemplate.code == code).first()
