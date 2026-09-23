"""物理样本、容器孔位占用与流转记录。

原 `samples` 表保留为「运行分配」（样本 × 批次 × 孔位 × 条件组），
物理实体独立成 `physical_samples`：登记样本不再要求先有批次。
"""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from ..core.clock import now
from ..core.db import Quantity
from .base import Base, uid


class PhysicalSample(Base):
    __tablename__ = "physical_samples"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    project_id: Mapped[str] = mapped_column(String, default="")
    barcode: Mapped[str] = mapped_column(String, default="")
    source: Mapped[str] = mapped_column(String, default="")
    sample_type: Mapped[str] = mapped_column(String, default="")
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("physical_samples.id"), nullable=True)
    quantity: Mapped[object | None] = mapped_column(Quantity, nullable=True)
    unit: Mapped[str] = mapped_column(String, default="")
    storage_condition: Mapped[str] = mapped_column(String, default="")
    current_location: Mapped[str] = mapped_column(String, default="")
    # 历史数据没有实际位置记录时写这一句，不能拿计划工位冒充实际位置
    location_note: Mapped[str] = mapped_column(String, default="")
    custodian: Mapped[str] = mapped_column(String, default="")
    # registered | received | in_use | stored | exhausted | disposed
    lifecycle_state: Mapped[str] = mapped_column(String, default="registered")
    note: Mapped[str] = mapped_column(Text, default="")
    # registered 人工登记或方案引用 / batch_generated 由批次生成 / split 分样 / legacy 迁移
    origin: Mapped[str] = mapped_column(String, default="registered")
    created_by: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    row_version: Mapped[int] = mapped_column(Integer, default=1)
    __table_args__ = (UniqueConstraint("org_id", "barcode", name="uq_sample_org_barcode"),)


class SlotOccupancy(Base):
    """在途孔位占用。一个容器孔位同时只能有一个在途样本。"""

    __tablename__ = "slot_occupancies"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    container_id: Mapped[str] = mapped_column(String)
    well: Mapped[str] = mapped_column(String)
    physical_sample_id: Mapped[str] = mapped_column(ForeignKey("physical_samples.id"))
    assignment_id: Mapped[str] = mapped_column(String, default="")
    occupied_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    released_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 部分唯一索引：只约束「在途」的占用。用完整唯一约束不管用——两种后端都不让
    # NULL 参与唯一比较，released_at 为 NULL 的两行照样能同时插进去。
    __table_args__ = (
        Index(
            "uq_slot_live", "container_id", "well", unique=True,
            postgresql_where=text("released_at IS NULL"),
        ),
    )


class SampleTransfer(Base):
    """交接与流转。覆盖一个 station_id 不算历史，这张表才是。"""

    __tablename__ = "sample_transfers"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    physical_sample_id: Mapped[str] = mapped_column(ForeignKey("physical_samples.id"), index=True)
    event_key: Mapped[str] = mapped_column(String, default="")
    # receive | handover | split | store | dispose | move
    kind: Mapped[str] = mapped_column(String, default="handover")
    from_location: Mapped[str] = mapped_column(String, default="")
    to_location: Mapped[str] = mapped_column(String, default="")
    from_party: Mapped[str] = mapped_column(String, default="")
    to_party: Mapped[str] = mapped_column(String, default="")
    quantity: Mapped[object | None] = mapped_column(Quantity, nullable=True)
    unit: Mapped[str] = mapped_column(String, default="")
    confirm_method: Mapped[str] = mapped_column(String, default="barcode")
    note: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String, default="")
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    __table_args__ = (
        UniqueConstraint("org_id", "physical_sample_id", "event_key", name="uq_transfer_event"),
    )
