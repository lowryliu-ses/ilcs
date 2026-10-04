from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from ..core.clock import now
from .base import Base, uid


class Command(Base):
    """设备指令。先持久化命令与投递状态，再做网络 I/O。

    `delivery_state` 区分「还没发」「已发出但未确认」「已确认」：重启后处于
    maybe_sent 的指令按原 command_id 查询设备侧状态，不生成新命令盲目重试。
    """

    __tablename__ = "commands"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, default="", index=True)
    batch_id: Mapped[str] = mapped_column(ForeignKey("batches.id"))
    step_run_id: Mapped[str] = mapped_column(String, default="", index=True)
    station_id: Mapped[str] = mapped_column(ForeignKey("stations.id"))
    capability: Mapped[str] = mapped_column(String)
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    # 步骤引用的设备方法（快照里冻结的编号、版本、设备端程序）；驱动按 program 选设备上的程序
    method: Mapped[dict] = mapped_column(JSON, default=dict)
    type: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String, default="sent")
    # queued | maybe_sent | delivered | unreachable
    delivery_state: Mapped[str] = mapped_column(String, default="queued")
    # 设备对动作给出的结论，与投递事实分开记：'' 还没有 / failed 设备明确失败或拒绝（已停下）/
    # unknown 设备收到了指令却回报结论未知（动作可能仍在进行，占用保留到现场核查）
    outcome: Mapped[str] = mapped_column(String, default="")
    step_index: Mapped[int] = mapped_column(Integer)
    checkpoint_id: Mapped[str] = mapped_column(String, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    # 交给适配器的时刻：超时判据的起点（updated_at 每次轮询都会变，不能用）
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 已发出超时报警的时刻；同一条指令只报一次
    overdue_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 按时开工：执行器在这个时刻之前不投递（排程时间窗开始减允许提前量）
    not_before: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 前置指令：它完成（done）之前本指令不投递。设备步骤等它的转运指令把板送到位
    after_command_id: Mapped[str] = mapped_column(String, default="", index=True)
    # 转运指令搬的是哪块板
    labware_id: Mapped[str] = mapped_column(String, default="")
    # 保持 / 终止 / 续跑针对的动作指令：控制指令只确认它自己的目标，不代表同批次别的设备
    target_command_id: Mapped[str] = mapped_column(String, default="")
    # 协同资源：这一步执行期间一并占用的其他工位（机械臂、放置位、配套设备），随本指令一起取得、一起释放
    assist_station_ids: Mapped[list] = mapped_column(JSON, default=list)
    # 这条动作在主工位上占几份通道：按样本计通道的工位是下发时批次在用的样本数，其余为 1。
    # 协同工位各占 1 份
    units: Mapped[int] = mapped_column(Integer, default=1)
    # 取自上游结果的参数（前馈）：每个样本一条，记来源步骤与检查点 / 记录、原始值与单位、系数及其出处、
    # 下发的计算值。下发时算一次、随指令冻结；重投同一指令不重新求值
    bindings: Mapped[list] = mapped_column(JSON, default=list)
    # 按瓶拆开下发（设备接入配置 wells_per_command）：依次执行的设备指令，每条 {id: <指令号>/<序号>, wells, state,
    # delivered, telemetry, origin, quality, device_ts, error}。state：pending 还没发 / sent 已交给适配器、没确认 /
    # running / done / failed / unknown。为空就是不拆，整条指令照旧一次下发
    runs: Mapped[list] = mapped_column(JSON, default=list)


class ExecutorHeartbeat(Base):
    """执行器存活记录。执行器停了，执行门必须知道——否则界面上能下发，却没有人去投递。"""

    __tablename__ = "executor_heartbeats"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    host: Mapped[str] = mapped_column(String, default="")
    pid: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=now)
    # 最近一轮的运行情况：耗时、并发线程数、仍在跑 / 疑似卡住的工位
    detail: Mapped[dict] = mapped_column(JSON, default=dict)


class Checkpoint(Base):
    """步骤检查点：实际交付量、时间戳、参数快照。重启对账的依据。"""

    __tablename__ = "checkpoints"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    batch_id: Mapped[str] = mapped_column(ForeignKey("batches.id"))
    command_id: Mapped[str] = mapped_column(ForeignKey("commands.id"), unique=True)
    step_index: Mapped[int] = mapped_column(Integer)
    step_run_id: Mapped[str] = mapped_column(String, default="")
    state: Mapped[str] = mapped_column(String)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class AdapterExecution(Base):
    """适配器幂等台账。主键是指令号，重复投递回放终态而不驱动第二次物理动作。"""

    __tablename__ = "adapter_executions"
    command_id: Mapped[str] = mapped_column(ForeignKey("commands.id"), primary_key=True)
    station_id: Mapped[str] = mapped_column(ForeignKey("stations.id"))
    state: Mapped[str] = mapped_column(String)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Telemetry(Base):
    """遥测点。带设备时间戳与质量标记，超时即显示失联而不是在线。"""

    __tablename__ = "telemetry"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    station_id: Mapped[str] = mapped_column(String)
    batch_id: Mapped[str] = mapped_column(String, default="")
    metric: Mapped[str] = mapped_column(String)
    setpoint: Mapped[float | None] = mapped_column(nullable=True)
    value: Mapped[float | None] = mapped_column(nullable=True)
    quality: Mapped[str] = mapped_column(String, default="good")
    origin: Mapped[str] = mapped_column(String, default="simulation")
    device_ts: Mapped[datetime] = mapped_column(DateTime, default=now)
    # 设备上报批次的稳定 ID；同一 (工位, 事件, 指标) 只入库一次
    event_id: Mapped[str] = mapped_column(String, default="")
    # 归属：哪条指令、哪一步、谁在值守（批次操作员）、哪个样本（设备按孔位报或批次只有一个样本时）
    command_id: Mapped[str] = mapped_column(String, default="")
    step_id: Mapped[str] = mapped_column(String, default="")
    step_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    operator: Mapped[str] = mapped_column(String, default="")
    sample_id: Mapped[str] = mapped_column(String, default="")
    received_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
