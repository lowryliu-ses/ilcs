from __future__ import annotations

from sqlalchemy import or_

from ..models import PhysicalSample, SampleTransfer, SlotOccupancy
from .base import ScopedRepository


class PhysicalSampleRepository(ScopedRepository[PhysicalSample]):
    model = PhysicalSample

    def page(
        self, offset: int, limit: int, keyword: str = "", state: str | None = None,
        project_id: str = "",
    ):
        query = self.query()
        if keyword:
            like = f"%{keyword}%"
            query = query.filter(
                or_(
                    PhysicalSample.id.like(like),
                    PhysicalSample.barcode.like(like),
                    PhysicalSample.source.like(like),
                )
            )
        if state:
            query = query.filter(PhysicalSample.lifecycle_state == state)
        if project_id:
            query = query.filter(PhysicalSample.project_id == project_id)
        total = query.count()
        rows = (
            query.order_by(PhysicalSample.created_at.desc()).offset(offset).limit(limit).all()
        )
        return list(rows), total

    def by_barcode(self, barcode: str) -> PhysicalSample | None:
        return self.query().filter(PhysicalSample.barcode == barcode).first()

    def children(self, parent_id: str) -> list[PhysicalSample]:
        return list(self.query().filter(PhysicalSample.parent_id == parent_id).all())

    def lineage(self, sample: PhysicalSample) -> list[PhysicalSample]:
        """向上找来源谱系。跨组织父子关联在写入时就被拒，这里只沿本组织走。"""
        chain: list[PhysicalSample] = []
        seen = {sample.id}
        current = sample
        while current.parent_id and current.parent_id not in seen:
            parent = self.get(current.parent_id)
            if not parent:
                break
            chain.append(parent)
            seen.add(parent.id)
            current = parent
        return chain

    def next_id(self, prefix: str, count_hint: int = 0) -> str:
        count = self.db.query(PhysicalSample).filter(PhysicalSample.id.startswith(prefix)).count()
        return f"{prefix}{count + 1 + count_hint:04d}"


class SlotOccupancyRepository(ScopedRepository[SlotOccupancy]):
    model = SlotOccupancy

    def live(self, container_id: str, well: str) -> SlotOccupancy | None:
        return (
            self.query()
            .filter(
                SlotOccupancy.container_id == container_id,
                SlotOccupancy.well == well,
                SlotOccupancy.released_at.is_(None),
            )
            .first()
        )

    def for_sample(self, physical_sample_id: str) -> list[SlotOccupancy]:
        return list(
            self.query()
            .filter(SlotOccupancy.physical_sample_id == physical_sample_id)
            .order_by(SlotOccupancy.occupied_at)
            .all()
        )


class SampleTransferRepository(ScopedRepository[SampleTransfer]):
    model = SampleTransfer

    def for_sample(self, physical_sample_id: str) -> list[SampleTransfer]:
        return list(
            self.query()
            .filter(SampleTransfer.physical_sample_id == physical_sample_id)
            .order_by(SampleTransfer.occurred_at)
            .all()
        )

    def find_event(self, physical_sample_id: str, event_key: str) -> SampleTransfer | None:
        if not event_key:
            return None
        return (
            self.query()
            .filter(
                SampleTransfer.physical_sample_id == physical_sample_id,
                SampleTransfer.event_key == event_key,
            )
            .first()
        )
