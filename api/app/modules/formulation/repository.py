from __future__ import annotations

from ...repositories.base import ScopedRepository
from .models import FormulationSubmission, FormulationTemplate


class FormulationTemplateRepository(ScopedRepository[FormulationTemplate]):
    model = FormulationTemplate

    def list(self, state: str | None = None) -> list[FormulationTemplate]:
        query = self.query()
        if state:
            query = query.filter(FormulationTemplate.state == state)
        return list(query.order_by(FormulationTemplate.code).all())

    def by_code(self, code: str) -> FormulationTemplate | None:
        return self.query().filter(FormulationTemplate.code == code).first()


class FormulationSubmissionRepository(ScopedRepository[FormulationSubmission]):
    model = FormulationSubmission

    def find(self, service_id: str, request_id: str) -> FormulationSubmission | None:
        return self.query().filter(
            FormulationSubmission.service_id == service_id, FormulationSubmission.request_id == request_id,
        ).first()
