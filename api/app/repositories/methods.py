from __future__ import annotations

from sqlalchemy import func

from ..models import DeviceMethod
from .base import ScopedRepository


class DeviceMethodRepository(ScopedRepository[DeviceMethod]):
    model = DeviceMethod

    def list(self, state: str | None = None, capability_id: str | None = None) -> list[DeviceMethod]:
        query = self.query()
        if state:
            query = query.filter(DeviceMethod.state == state)
        if capability_id:
            query = query.filter(DeviceMethod.capability_id == capability_id)
        return list(query.order_by(DeviceMethod.code, DeviceMethod.version.desc()).all())

    def versions(self, code: str) -> list[DeviceMethod]:
        return list(self.query().filter(DeviceMethod.code == code).order_by(DeviceMethod.version).all())

    def next_code(self) -> str:
        count = self.db.query(func.count(func.distinct(DeviceMethod.code))).filter(
            DeviceMethod.org_id == self.org_id,
        ).scalar() or 0
        number = int(count) + 1
        while self.query().filter(DeviceMethod.code == f"DM-{number:03d}").first() is not None:
            number += 1
        return f"DM-{number:03d}"
