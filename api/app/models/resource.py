from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from ..core.clock import now
from .base import Base, uid


class Capability(Base):
    """能力字典。恢复规则挂在能力上，流程步骤只继承不覆盖。"""

    __tablename__ = "capabilities"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    # 参数键 → 显示名称。界面与历史快照都读它，保持原样
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    # 参数键 → {type, unit, required}：数值 / 整数、单位、是否必填。没登记的按「数值、单位未登记、必填」
    param_specs: Mapped[dict] = mapped_column(JSON, default=dict)
    recovery: Mapped[dict] = mapped_column(JSON, default=dict)
    # 停用而不是删除：已有流程快照仍引用它，字典条目必须留着才解释得了历史批次
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
    """工位：系统里的执行位置。

    实物身份（型号、序列号、固件）、校准与总容量归资产档案；工位只管能接什么活（能力极限）、
    同时接几份（通道）、怎么连设备（适配器）。校准到期与样品位不在这里存（迁移 0036 删列）：
    前者是资产校准的副本、会和资产各说各话，后者从来不参与任何判断。
    """

    __tablename__ = "stations"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    org_id: Mapped[str] = mapped_column(String, default="", index=True)
    # 一台资产可映射多个工位，共享资产级容量
    asset_id: Mapped[str] = mapped_column(String, default="")
    island: Mapped[int] = mapped_column(Integer, default=0)
    name: Mapped[str] = mapped_column(String)
    # 只对没关联资产的工位（AGV、机械臂一类）有意义；关联了资产就以资产登记的型号为准（`station_model`）
    model: Mapped[str] = mapped_column(String, default="")
    status: Mapped[str] = mapped_column(String, default="idle")
    # 并行通道数（如 8 通道充放电柜）。排程按它允许同一工位的时间窗重叠
    channels: Mapped[int] = mapped_column(Integer, default=1)
    # 通道怎么计：batch 一个批次的一个设备步骤占 1 个；sample 批次里每个样本各占 1 个
    # （一颗电芯占一个物理通道的充放电柜）。排程、写入守门、投递与资产容量都按同一口径数份数
    channel_unit: Mapped[str] = mapped_column(String, default="batch")
    clean: Mapped[bool] = mapped_column(Boolean, default=True)
    # 待清洗时是哪个批次用过它：同一批次的后续步骤可以接着用，别的批次要等清洗确认
    dirty_batch_id: Mapped[str] = mapped_column(String, default="")
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
    # 配置变更后还欠的接入验收：'' 不欠 / readonly 只读级 / physical 动作级。欠着就按「待接入验收」挡住下发
    acceptance_required: Mapped[str] = mapped_column(String, default="")
    # 最近一次满足要求的验收：对应的配置版本与验收记录
    accepted_config_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    accepted_run_id: Mapped[str] = mapped_column(String, default="")
    # 套用的设备接入模板（某一版）与工位自己的连接参数；config 仍是合并后的完整配置。空表示没套模板
    template_id: Mapped[str] = mapped_column(String, default="")
    template_connection: Mapped[dict] = mapped_column(JSON, default=dict)


class DeviceTemplate(Base):
    """设备接入模板：一类设备怎么接。驱动 + 映射配置 + 连接参数示例 + 支持标志 + 验收缺省，按修订号管理。

    草稿可改；发布要另一个人签名（起草人不能发布本人起草的模板），发布后内容冻结（触发器），要改就新建修订。
    发布新修订时同编号的旧发布版退役，但不自动推给工位：套用旧版的工位照常运行，由人逐台切换、签名、重新验收。
    设备模块交付的 profile.json 就是它的导出文件。
    """

    __tablename__ = "device_templates"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    code: Mapped[str] = mapped_column(String)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    name: Mapped[str] = mapped_column(String)
    # 适用的资产型号（空表示不限）；登记同型号的工位时排在前面
    model: Mapped[str] = mapped_column(String, default="")
    vendor: Mapped[str] = mapped_column(String, default="")
    driver: Mapped[str] = mapped_column(String)
    protocol: Mapped[str] = mapped_column(String, default="")
    version: Mapped[str] = mapped_column(String, default="")
    # 映射配置：不含每台设备的连接参数（地址、证书、设备编号）
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    # 连接参数示例：套用时由工位填写，导出文件里带着给接入的人看
    connection: Mapped[dict] = mapped_column(JSON, default=dict)
    # {hold, abort, query, dedup}
    supports: Mapped[dict] = mapped_column(JSON, default=dict)
    # 接入验收的缺省：{capability, params}
    acceptance: Mapped[dict] = mapped_column(JSON, default=dict)
    note: Mapped[str] = mapped_column(Text, default="")
    # draft | released | retired
    state: Mapped[str] = mapped_column(String, default="draft")
    digest: Mapped[str] = mapped_column(String, default="")
    # 从哪来：{kind: manual | import | revise, file, digest, from}
    source: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by: Mapped[str] = mapped_column(String, default="")
    created_by_name: Mapped[str] = mapped_column(String, default="")
    # 起草与改过草稿的人（用户 ID）：他们都不能发布它
    editors: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    released_by: Mapped[str] = mapped_column(String, default="")
    released_by_name: Mapped[str] = mapped_column(String, default="")
    released_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    row_version: Mapped[int] = mapped_column(Integer, default=1)
    __table_args__ = (UniqueConstraint("org_id", "code", "revision", name="uq_device_template_revision"),)


class AcceptanceRun(Base):
    """一次设备接入验收：申请、执行器执行、报告。出了结论就只读，永不删除（上线证据）。

    验收的对象是执行时刻的驱动与配置（`driver`、`config_version`、`config_digest`），报告与它一起存档：
    配置之后再变，这份报告说的仍是当时那一版。
    """

    __tablename__ = "acceptance_runs"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    station_id: Mapped[str] = mapped_column(String, index=True)
    # readonly 只读级 | physical 动作级（会让设备动作）
    level: Mapped[str] = mapped_column(String, default="readonly")
    # 故障项目（丢回执、忙、联锁、失联）：只对自报为模拟器、登记了控制口的设备生效
    faults: Mapped[bool] = mapped_column(Boolean, default=False)
    capability: Mapped[str] = mapped_column(String, default="")
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    # manual 手动 | config_change 配置变更后自动 | device_online 设备恢复在线后自动重跑
    trigger: Mapped[str] = mapped_column(String, default="manual")
    approval: Mapped[str] = mapped_column(Text, default="")
    signature_id: Mapped[str] = mapped_column(String, default="")
    requested_by: Mapped[str] = mapped_column(String, default="")
    requested_by_id: Mapped[str] = mapped_column(String, default="")
    # queued | running | done | error | cancelled
    state: Mapped[str] = mapped_column(String, default="queued", index=True)
    kind: Mapped[str] = mapped_column(String, default="")
    driver: Mapped[str] = mapped_column(String, default="")
    protocol: Mapped[str] = mapped_column(String, default="")
    adapter_version: Mapped[str] = mapped_column(String, default="")
    config_version: Mapped[int] = mapped_column(Integer, default=0)
    config_digest: Mapped[str] = mapped_column(String, default="")
    # 验收时工位套用的设备接入模板
    template_id: Mapped[str] = mapped_column(String, default="")
    template_code: Mapped[str] = mapped_column(String, default="")
    template_revision: Mapped[int] = mapped_column(Integer, default=0)
    ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    simulator: Mapped[bool] = mapped_column(Boolean, default=False)
    identity: Mapped[dict] = mapped_column(JSON, default=dict)
    checks: Mapped[list] = mapped_column(JSON, default=list)
    report_md: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


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


class EnvironmentReading(Base):
    """环境读数：区域（工位编号或房间 / 手套箱名）× 指标。步骤的环境要求按最新读数核对。"""

    __tablename__ = "environment_readings"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    zone: Mapped[str] = mapped_column(String, index=True)
    metric: Mapped[str] = mapped_column(String)
    value: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String, default="")
    # device：传感器 / 设备上报；manual：人工抄录
    source: Mapped[str] = mapped_column(String, default="manual")
    measured_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    recorded_by: Mapped[str] = mapped_column(String, default="")
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class PersonBooking(Base):
    """人员预占。排程时为人工 / 审核步骤预占执行人的时间，请假、培训也登记在这里，同一时间一个人只做一件事。"""

    __tablename__ = "person_bookings"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    person_id: Mapped[str] = mapped_column(String, index=True)
    # step：批次步骤 | leave：请假 | training：培训 | duty：值守
    kind: Mapped[str] = mapped_column(String, default="step")
    starts_at: Mapped[datetime] = mapped_column(DateTime)
    ends_at: Mapped[datetime] = mapped_column(DateTime)
    batch_id: Mapped[str] = mapped_column(String, default="", index=True)
    step_id: Mapped[str] = mapped_column(String, default="")
    # confirmed | done | cancelled
    state: Mapped[str] = mapped_column(String, default="confirmed")
    reason: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
