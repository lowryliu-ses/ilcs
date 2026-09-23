"""耗材载具（板、托盘、样品架）与它们在实验室里的位置。

高通量产线上「这块板现在在哪」是调度与安全的前提：机械臂 / AGV 要知道从哪取、放到哪，
设备要知道自己的放置位上有没有东西。这里只记录**被确认过**的位置：
- 转运指令的设备回执确认完成，或
- 操作员扫码确认的人工放置。
系统不按计划推算位置——计划说「应该在 ST-05」不等于板真的在那里。

- `LabwareType`：几何规格（行 × 列），孔位图按它画。全站共享。
- `Location`：能放一块板的物理位置：工位放置位（nest）、板库槽位（hotel）、缓冲位、库房。全站共享，
  与工位一样是物理资源。
- `Labware`：一块具体的板 / 托盘，条码在本组织内唯一；当前位置为空表示「未上线或位置未知」。
- `LabwareMove`：只追加的移位记录，是位置的唯一依据来源。
"""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from ..core.clock import now
from .base import Base, uid


class LabwareType(Base):
    __tablename__ = "labware_types"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    # plate | tray | rack | holder
    kind: Mapped[str] = mapped_column(String, default="plate")
    rows: Mapped[int] = mapped_column(Integer, default=1)
    cols: Mapped[int] = mapped_column(Integer, default=1)
    note: Mapped[str] = mapped_column(Text, default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Location(Base):
    __tablename__ = "locations"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    # nest（工位放置位）| hotel（板库槽位）| buffer（缓冲位）| storage（库房）
    kind: Mapped[str] = mapped_column(String, default="nest")
    station_id: Mapped[str] = mapped_column(String, default="", index=True)
    # 同一组槽位（板库、缓冲架）在界面上画在一起
    group: Mapped[str] = mapped_column(String, default="")
    position: Mapped[int] = mapped_column(Integer, default=0)
    # 允许放的载具种类；空表示不限
    accepts: Mapped[list] = mapped_column(JSON, default=list)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    row_version: Mapped[int] = mapped_column(Integer, default=1)


class Labware(Base):
    __tablename__ = "labware"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    barcode: Mapped[str] = mapped_column(String)
    type_id: Mapped[str] = mapped_column(ForeignKey("labware_types.id"))
    # 当前绑定的批次；批次结束后保留为「最近一次使用」
    batch_id: Mapped[str] = mapped_column(String, default="", index=True)
    location_id: Mapped[str | None] = mapped_column(ForeignKey("locations.id"), nullable=True)
    # idle | in_use | lost（部分执行 / 核查后位置不可信）| retired
    state: Mapped[str] = mapped_column(String, default="idle")
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    row_version: Mapped[int] = mapped_column(Integer, default=1)
    __table_args__ = (
        UniqueConstraint("org_id", "barcode", name="uq_labware_org_barcode"),
        # 一个位置同一时刻只放一块板：部分唯一索引只约束有位置的行
        Index(
            "uq_labware_location_live", "location_id", unique=True,
            postgresql_where=text("location_id IS NOT NULL AND state <> 'retired'"),
        ),
    )


class LabwareMove(Base):
    __tablename__ = "labware_moves"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    labware_id: Mapped[str] = mapped_column(ForeignKey("labware.id"), index=True)
    from_location_id: Mapped[str] = mapped_column(String, default="")
    to_location_id: Mapped[str] = mapped_column(String, default="")
    # transfer（设备回执确认）| manual（扫码人工放置）| verification（结果未知指令的现场核查）| lost
    source: Mapped[str] = mapped_column(String)
    command_id: Mapped[str] = mapped_column(String, default="")
    batch_id: Mapped[str] = mapped_column(String, default="")
    barcode_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str] = mapped_column(Text, default="")
    by: Mapped[str] = mapped_column(String, default="")
    at: Mapped[datetime] = mapped_column(DateTime, default=now)
