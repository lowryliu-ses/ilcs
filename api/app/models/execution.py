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
    type: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String, default="sent")
    # queued | maybe_sent | delivered | unreachable
    delivery_state: Mapped[str] = mapped_column(String, default="queued")
    step_index: Mapped[int] = mapped_column(Integer)
    checkpoint_id: Mapped[str] = mapped_column(String, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    # 交给适配器的时刻：超时判据的起点（updated_at 每次轮询都会变，不能用）
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 已发出超时报警的时刻；同一条指令只报一次
    overdue_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ExecutorHeartbeat(Base):
    """执行器存活记录。执行器停了，执行门必须知道——否则界面上能下发，却没有人去投递。"""

    __tablename__ = "executor_heartbeats"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    host: Mapped[str] = mapped_column(String, default="")
    pid: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=now)


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
