from __future__ import annotations

from ..models import Adapter, DeviceTemplate
from .base import ScopedRepository


class DeviceTemplateRepository(ScopedRepository[DeviceTemplate]):
    model = DeviceTemplate

    def list(self, state: str | None = None, driver: str | None = None) -> list[DeviceTemplate]:
        query = self.query()
        if state:
            query = query.filter(DeviceTemplate.state == state)
        if driver:
            query = query.filter(DeviceTemplate.driver == driver)
        return list(query.order_by(DeviceTemplate.code, DeviceTemplate.revision.desc()).all())

    def versions(self, code: str) -> list[DeviceTemplate]:
        return list(self.query().filter(DeviceTemplate.code == code).order_by(DeviceTemplate.revision).all())

    def latest_released(self, code: str) -> DeviceTemplate | None:
        return (
            self.query().filter(DeviceTemplate.code == code, DeviceTemplate.state == "released")
            .order_by(DeviceTemplate.revision.desc()).first()
        )

    def adapters_using(self, template_ids: list[str]) -> list[Adapter]:
        """套用这些模板（某几版）的工位适配器。适配器按工位全站登记，组织范围由调用方按工位过滤。"""
        if not template_ids:
            return []
        return list(self.db.query(Adapter).filter(Adapter.template_id.in_(template_ids)).order_by(Adapter.station_id).all())
