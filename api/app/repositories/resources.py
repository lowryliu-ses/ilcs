from __future__ import annotations

from datetime import datetime

from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..domain.capability import StationSpec
from ..domain.gate import AdapterHealth
from ..models import (
    Adapter, Asset, CalibrationRecord, Capability, Island, ResourceBooking, Station,
)
from .base import Repository, ScopedRepository


class StationRepository(ScopedRepository[Station]):
    model = Station

    def list(self) -> list[Station]:
        return list(self.query().order_by(Station.island, Station.id).all())

    def specs(self) -> list[StationSpec]:
        return [
            StationSpec(
                id=s.id, status=s.status, clean=s.clean, cal_due=s.cal_due,
                positions=s.positions, limits=s.limits or {}, retired=s.retired,
                asset_id=s.asset_id or "",
            )
            for s in self.list()
        ]

    def for_asset(self, asset_id: str) -> list[Station]:
        return list(self.query().filter(Station.asset_id == asset_id).all())

    def transfer_station_ids(self, capability_id: str = "cap.transfer") -> list[str]:
        return [
            s.id for s in self.list()
            if capability_id in (s.limits or {}) and s.status != "fault" and not s.retired
        ]


class CapabilityRepository(Repository[Capability]):
    """能力字典是全局主数据，不按组织分。历史快照要靠它解释，所以只停用不删除。"""

    model = Capability

    def list(self) -> list[Capability]:
        return list(self.db.query(Capability).order_by(Capability.id).all())

    def names(self) -> dict[str, str]:
        return {c.id: c.name for c in self.list()}

    def specs(self) -> dict[str, dict]:
        return {c.id: {"name": c.name, "params": c.params or {}, "retired": c.retired} for c in self.list()}

    def recovery_of(self, capability_id: str) -> dict:
        capability = self.get(capability_id)
        return (capability.recovery if capability else {}) or {}


class AssetRepository(ScopedRepository[Asset]):
    model = Asset

    def list(self) -> list[Asset]:
        return list(self.query().order_by(Asset.asset_no).all())

    def page(self, offset: int, limit: int, keyword: str = "", state: str | None = None):
        query = self.query()
        if keyword:
            like = f"%{keyword}%"
            query = query.filter(
                or_(Asset.asset_no.like(like), Asset.name.like(like), Asset.serial.like(like))
            )
        if state:
            query = query.filter(Asset.state == state)
        total = query.count()
        rows = query.order_by(Asset.asset_no).offset(offset).limit(limit).all()
        return list(rows), total

    def by_no(self, asset_no: str) -> Asset | None:
        return self.query().filter(Asset.asset_no == asset_no).first()

    def lock_for_update(self, asset_id: str) -> Asset | None:
        """锁住容量归属资产，串行化同一资产上的冲突检查与占用写入。"""
        query = self.query().filter(Asset.id == asset_id)
        query = query.with_for_update()
        return query.first()


class CalibrationRepository(ScopedRepository[CalibrationRecord]):
    model = CalibrationRecord

    def for_asset(self, asset_id: str) -> list[CalibrationRecord]:
        return list(
            self.query()
            .filter(CalibrationRecord.asset_id == asset_id)
            .order_by(CalibrationRecord.effective_from.desc())
            .all()
        )

    def all_by_asset(self) -> dict[str, list[CalibrationRecord]]:
        grouped: dict[str, list[CalibrationRecord]] = {}
        for row in self.query().order_by(CalibrationRecord.effective_from.desc()).all():
            grouped.setdefault(row.asset_id, []).append(row)
        return grouped


class BookingRepository(ScopedRepository[ResourceBooking]):
    model = ResourceBooking

    def live(self) -> list[ResourceBooking]:
        return list(
            self.query()
            .filter(ResourceBooking.state.in_(["pending", "confirmed"]))
            .order_by(ResourceBooking.starts_at)
            .all()
        )

    def overlapping(
        self, asset_id: str, starts_at: datetime, ends_at: datetime, exclude_id: str = "",
    ) -> list[ResourceBooking]:
        query = self.query().filter(
            ResourceBooking.asset_id == asset_id,
            ResourceBooking.state.in_(["pending", "confirmed"]),
            ResourceBooking.starts_at < ends_at,
            ResourceBooking.ends_at > starts_at,
        )
        if exclude_id:
            query = query.filter(ResourceBooking.id != exclude_id)
        return list(query.all())

    def for_asset(self, asset_id: str) -> list[ResourceBooking]:
        return list(
            self.query()
            .filter(ResourceBooking.asset_id == asset_id)
            .order_by(ResourceBooking.starts_at)
            .all()
        )

    def for_batch(self, batch_id: str) -> list[ResourceBooking]:
        return list(self.query().filter(ResourceBooking.batch_id == batch_id).all())


class AdapterRepository(Repository[Adapter]):
    model = Adapter

    def list(self) -> list[Adapter]:
        return list(self.db.query(Adapter).order_by(Adapter.station_id).all())

    def health(self) -> list[AdapterHealth]:
        return [
            AdapterHealth(
                station_id=a.station_id, connected=a.connected, site_interlock=a.site_interlock,
                last_heartbeat=a.last_heartbeat, enabled=bool(a.enabled),
            )
            for a in self.list()
        ]


class IslandRepository(Repository[Island]):
    model = Island

    def list(self) -> list[Island]:
        return list(self.db.query(Island).order_by(Island.id).all())


def resource_repositories(db: Session, ctx=None):
    return StationRepository(db, ctx), CapabilityRepository(db), AdapterRepository(db)
