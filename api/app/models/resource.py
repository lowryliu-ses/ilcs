from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from ..core.clock import now
from .base import Base, uid


class Capability(Base):
    """能力字典。恢复规则挂在能力上，配方步骤只继承不覆盖。"""

    __tablename__ = "capabilities"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    recovery: Mapped[dict] = mapped_column(JSON, default=dict)
    # 停用而不是删除：已有配方快照仍引用它，字典条目必须留着才解释得了历史批次
    retired: Mapped[bool] = mapped_column(Boolean, default=False)


class Asset(Base):
    """资产档案。支持无适配器的仪器和手工工作台，它们没有 Station 也要能预约。"""

    __tablename__ = "assets"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    asset_no: Mapped[str] = mapped_column(String)
    name: Mapped[str] = mapped_column(String)
    model: Mapped[str] = mapped_column(String, default="")
    vendor: Mapped[str] = mapped_column(String, default="")
    serial: Mapped[str] = mapped_column(String, default="")
    firmware: Mapped[str] = mapped_column(String, default="")
    lab_id: Mapped[str] = mapped_column(String, default="")
    owner_person_id: Mapped[str] = mapped_column(String, default="")
    location: Mapped[str] = mapped_column(String, default="")
    # active | maintenance | retired
    state: Mapped[str] = mapped_column(String, default="active")
    # 共享资产级容量。首期默认 1 份独占容量；可独立并行的通道必须显式配置。
    capacity: Mapped[int] = mapped_column(Integer, default=1)
    # 明确「不适用校准」的资源不强制证书；缺失不等同不适用
    calibration_applicable: Mapped[bool] = mapped_column(Boolean, default=True)
    calibration_exempt_reason: Mapped[str] = mapped_column(String, default="")
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    row_version: Mapped[int] = mapped_column(Integer, default=1)
    __table_args__ = (UniqueConstraint("org_id", "asset_no", name="uq_asset_org_no"),)


class CalibrationRecord(Base):
    """校准记录。只有有效且合格的记录构成许可。"""

    __tablename__ = "calibration_records"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id"), index=True)
    # 空表示覆盖整台资产；否则只覆盖列出的能力
    capability_scope: Mapped[list] = mapped_column(JSON, default=list)
    result: Mapped[str] = mapped_column(String, default="pass")  # pass | fail
    effective_from: Mapped[datetime] = mapped_column(DateTime, default=now)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    certificate_file_id: Mapped[str] = mapped_column(String, default="")
    registered_by: Mapped[str] = mapped_column(String, default="")
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class ResourceBooking(Base):
    """资源占用。维护、人工预约与自动排程共用一套冲突判断。"""

    __tablename__ = "resource_bookings"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    asset_id: Mapped[str] = mapped_column(String, default="", index=True)
    station_id: Mapped[str] = mapped_column(String, default="")
    # maintenance | manual | calibration | schedule
    kind: Mapped[str] = mapped_column(String, default="maintenance")
    starts_at: Mapped[datetime] = mapped_column(DateTime)
    ends_at: Mapped[datetime] = mapped_column(DateTime)
    reason: Mapped[str] = mapped_column(Text, default="")
    # pending | confirmed | cancelled | done
    state: Mapped[str] = mapped_column(String, default="confirmed")
    batch_id: Mapped[str] = mapped_column(String, default="")
    step_run_id: Mapped[str] = mapped_column(String, default="")
    created_by: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    row_version: Mapped[int] = mapped_column(Integer, default=1)


class Station(Base):
    __tablename__ = "stations"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    org_id: Mapped[str] = mapped_column(String, default="", index=True)
    # 一台资产可映射多个工位，共享资产级容量
    asset_id: Mapped[str] = mapped_column(String, default="")
    island: Mapped[int] = mapped_column(Integer, default=0)
    name: Mapped[str] = mapped_column(String)
    model: Mapped[str] = mapped_column(String, default="")
    status: Mapped[str] = mapped_column(String, default="idle")
    cal_due: Mapped[str] = mapped_column(String, default="")
    positions: Mapped[int] = mapped_column(Integer, default=1)
    # 并行通道数（如 8 通道充放电柜）。排程按它允许同一工位的时间窗重叠
    channels: Mapped[int] = mapped_column(Integer, default=1)
    clean: Mapped[bool] = mapped_column(Boolean, default=True)
    limits: Mapped[dict] = mapped_column(JSON, default=dict)  # {capability: {param: [lo, hi]}}
    # 退役工位不参与排程匹配，但历史分配与检查点仍指向它，所以不删行
    retired: Mapped[bool] = mapped_column(Boolean, default=False)
    row_version: Mapped[int] = mapped_column(Integer, default=1)


class Island(Base):
    __tablename__ = "islands"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String)


class Adapter(Base):
    """设备适配器登记与运行时状态。执行门由这里的心跳与联锁计算。"""

    __tablename__ = "adapters"
    station_id: Mapped[str] = mapped_column(ForeignKey("stations.id"), primary_key=True)
    protocol: Mapped[str] = mapped_column(String)
    # driver 是代码注册键（如 simulation / modbus_tcp）；protocol 是给人看的协议名称。
    driver: Mapped[str] = mapped_column(String, default="simulation")
    version: Mapped[str] = mapped_column(String, default="")
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    # 这里只保存密钥管理器中的引用，禁止保存口令、token 或私钥原文。
    credential_ref: Mapped[str] = mapped_column(String, default="")
    config_version: Mapped[int] = mapped_column(Integer, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    row_version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    note: Mapped[str] = mapped_column(Text, default="")
    connected: Mapped[bool] = mapped_column(Boolean, default=True)
    accepts_commands: Mapped[bool] = mapped_column(Boolean, default=True)
    site_interlock: Mapped[bool] = mapped_column(Boolean, default=False)
    dedup_count: Mapped[int] = mapped_column(Integer, default=0)
    last_heartbeat: Mapped[datetime] = mapped_column(DateTime, default=now)
    current_command_id: Mapped[str] = mapped_column(String, default="")
    # 适配器契约声明：真实设备不支持的能力在 UI 禁用并说明原因，不假装通用支持
    kind: Mapped[str] = mapped_column(String, default="simulation")  # simulation | real
    supports_hold: Mapped[bool] = mapped_column(Boolean, default=True)
    supports_abort: Mapped[bool] = mapped_column(Boolean, default=True)
    supports_query: Mapped[bool] = mapped_column(Boolean, default=True)
    supports_dedup: Mapped[bool] = mapped_column(Boolean, default=True)
    # 驱动自报（或登记时声明）的设备身份与方法目录。空列表表示没报过，不据此筛工位
    vendor: Mapped[str] = mapped_column(String, default="")
    firmware: Mapped[str] = mapped_column(String, default="")
    reported_model: Mapped[str] = mapped_column(String, default="")
    # [{program, name, capability}]
    methods: Mapped[list] = mapped_column(JSON, default=list)
    # 设备接受的指令类型，如 dispatch / hold / abort / query / resume
    commands: Mapped[list] = mapped_column(JSON, default=list)
    # device：设备自报；config：驱动协议带不了方法目录，按登记配置
    described_from: Mapped[str] = mapped_column(String, default="")
    described_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class MaintenanceOrder(Base):
    """维护 / 点检工单。计划 → 执行 → 完成，完成要写记录并签名。

    建单即登记一条维护占用（排程据此让路）；开工把资产转入维护状态（开跑检查据此拦截）；
    完成或取消恢复资产原状态并结束占用。
    """

    __tablename__ = "maintenance_orders"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    asset_id: Mapped[str] = mapped_column(String, index=True)
    # preventive 预防性维护 | corrective 故障维修 | inspection 点检
    kind: Mapped[str] = mapped_column(String, default="preventive")
    title: Mapped[str] = mapped_column(String)
    detail: Mapped[str] = mapped_column(Text, default="")
    planned_start: Mapped[datetime] = mapped_column(DateTime)
    planned_end: Mapped[datetime] = mapped_column(DateTime)
    # planned | in_progress | done | cancelled
    state: Mapped[str] = mapped_column(String, default="planned")
    booking_id: Mapped[str] = mapped_column(String, default="")
    assignee_user_id: Mapped[str] = mapped_column(String, default="")
    asset_state_before: Mapped[str] = mapped_column(String, default="")
    created_by: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    started_by: Mapped[str] = mapped_column(String, default="")
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_by: Mapped[str] = mapped_column(String, default="")
    # pass 合格 | fail 不合格（资产保持维护状态，不回到可用）
    result: Mapped[str] = mapped_column(String, default="")
    record: Mapped[str] = mapped_column(Text, default="")
    signature_id: Mapped[str] = mapped_column(String, default="")
    row_version: Mapped[int] = mapped_column(Integer, default=1)
