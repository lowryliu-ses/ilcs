"""物料主数据、批号、预留与只追加库存流水。

账面库存 = 组织仍持有且未消耗的总量（含已领用未消耗）。
未耗用占用 = 授权预留 − 已消耗 − 已核销损耗 − 已释放。
可用量 = 账面库存 − 全部未耗用占用。
"""
from datetime import datetime

from sqlalchemy import (
    Boolean, CheckConstraint, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from ..core.clock import now
from ..core.db import Quantity
from .base import Base, uid


class Material(Base):
    """稳定物料标识。旧物料名称保留展示与迁移映射，不用名称关联不同单位的物料。"""

    __tablename__ = "materials"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    code: Mapped[str] = mapped_column(String)
    name: Mapped[str] = mapped_column(String)
    base_unit: Mapped[str] = mapped_column(String)
    category: Mapped[str] = mapped_column(String, default="")
    cas: Mapped[str] = mapped_column(String, default="")
    # 登记的精确换算：{"kg": "1000"}；未登记的单位一律拒绝
    conversions: Mapped[dict] = mapped_column(JSON, default=dict)
    external_ref: Mapped[str] = mapped_column(String, default="")
    ghs: Mapped[list] = mapped_column(JSON, default=list)
    state: Mapped[str] = mapped_column(String, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    __table_args__ = (UniqueConstraint("org_id", "code", name="uq_material_org_code"),)


class Lot(Base):
    __tablename__ = "lots"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    org_id: Mapped[str] = mapped_column(String, default="", index=True)
    material_id: Mapped[str] = mapped_column(String, default="")
    material: Mapped[str] = mapped_column(String)
    cas: Mapped[str] = mapped_column(String, default="")
    type: Mapped[str] = mapped_column(String, default="")
    # 账面库存
    qty: Mapped[object] = mapped_column(Quantity, default=0)
    # 期初余额：迁移时由责任人确认，作为流水的起点
    opening_balance: Mapped[object] = mapped_column(Quantity, default=0)
    unit: Mapped[str] = mapped_column(String)
    release: Mapped[str] = mapped_column(String)
    sds: Mapped[str] = mapped_column(String, default="")
    compat: Mapped[str] = mapped_column(String, default="")
    expiry: Mapped[str] = mapped_column(String)
    opened: Mapped[str] = mapped_column(String, default="")
    # 开封后的截止时间与依据。按类别自动推算的天数待业务确认，不预置假定值。
    open_expiry: Mapped[str] = mapped_column(String, default="")
    open_expiry_basis: Mapped[str] = mapped_column(String, default="")
    storage: Mapped[str] = mapped_column(String, default="")
    ghs: Mapped[list] = mapped_column(JSON, default=list)
    # active | scrapped。报废的批号留档：它可能已经进过某个批次的投料记录
    state: Mapped[str] = mapped_column(String, default="active")
    scrap_reason: Mapped[str] = mapped_column(String, default="")
    __table_args__ = (CheckConstraint("qty >= 0", name="ck_lot_qty_nonneg"),)


class Reservation(Base):
    """预留占用。终止只自动释放未领用未消耗部分；已领用的走归还或处置。"""

    __tablename__ = "reservations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(String, default="", index=True)
    batch_id: Mapped[str] = mapped_column(String, index=True)
    lot_id: Mapped[str] = mapped_column(ForeignKey("lots.id"))
    # 授权预留总量
    qty: Mapped[object] = mapped_column(Quantity, default=0)
    unit: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String, default="reserved")  # reserved | consumed | released
    consumed_qty: Mapped[object] = mapped_column(Quantity, default=0)
    loss_qty: Mapped[object] = mapped_column(Quantity, default=0)
    released_qty: Mapped[object] = mapped_column(Quantity, default=0)
    issued_qty: Mapped[object] = mapped_column(Quantity, default=0)
    returned_qty: Mapped[object] = mapped_column(Quantity, default=0)
    # 兼容旧字段名：历史行里它等于已消耗量
    delivered_qty: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    row_version: Mapped[int] = mapped_column(Integer, default=1)


class InventoryEvent(Base):
    """库存业务事件。唯一约束让同一事件重试只入账一次。"""

    __tablename__ = "inventory_events"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    source: Mapped[str] = mapped_column(String)  # device | manual | weighing | return | migration
    event_id: Mapped[str] = mapped_column(String)
    # receive | reserve | issue | consume | loss | return | release | adjust | reverse
    event_type: Mapped[str] = mapped_column(String)
    batch_id: Mapped[str] = mapped_column(String, default="")
    step_run_id: Mapped[str] = mapped_column(String, default="")
    command_id: Mapped[str] = mapped_column(String, default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    reverses_id: Mapped[str] = mapped_column(String, default="")
    created_by: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    __table_args__ = (UniqueConstraint("org_id", "source", "event_id", name="uq_inventory_event"),)


class InventoryLedger(Base):
    """只追加流水。错误记录通过受控冲正修复，不直接改已入账的行。"""

    __tablename__ = "inventory_ledger"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    event_row_id: Mapped[str] = mapped_column(ForeignKey("inventory_events.id"), index=True)
    source: Mapped[str] = mapped_column(String)
    event_id: Mapped[str] = mapped_column(String)
    line_no: Mapped[int] = mapped_column(Integer, default=1)
    event_type: Mapped[str] = mapped_column(String)
    lot_id: Mapped[str] = mapped_column(ForeignKey("lots.id"), index=True)
    material_id: Mapped[str] = mapped_column(String, default="")
    reservation_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    batch_id: Mapped[str] = mapped_column(String, default="")
    step_run_id: Mapped[str] = mapped_column(String, default="")
    quantity: Mapped[object] = mapped_column(Quantity, default=0)
    unit: Mapped[str] = mapped_column(String)
    # 对账面库存的影响；不影响库存的事件（预留、领用）为 0
    balance_delta: Mapped[object] = mapped_column(Quantity, default=0)
    balance_after: Mapped[object] = mapped_column(Quantity, default=0)
    operator: Mapped[str] = mapped_column(String, default="")
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    __table_args__ = (
        UniqueConstraint("org_id", "source", "event_id", "line_no", name="uq_ledger_event_line"),
    )


class WasteTank(Base):
    __tablename__ = "waste_tanks"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    org_id: Mapped[str] = mapped_column(String, default="", index=True)
    kind: Mapped[str] = mapped_column(String)
    level_pct: Mapped[float] = mapped_column(Float, default=0)
    capacity_l: Mapped[float] = mapped_column(Float, default=20)
