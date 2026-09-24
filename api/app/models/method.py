from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from ..core.clock import now
from .base import Base, uid


class DeviceMethod(Base):
    """设备方法：「流程做什么，方法怎么做」里的后一半。

    一条方法 = 能力 + 适用仪器型号 + 设备端程序 + 参数（缺省值与允许范围）+ 数据输出规则，按版本管理。
    流程里的设备步骤引用一条已发布的方法；建批次时方法内容冻结进快照，之后方法怎么修订，
    在途批次都按快照执行。发布新版本时旧的已发布版本退役，引用它的流程要改引用并重新评审。
    """

    __tablename__ = "device_methods"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    # 同一方法各版本共用编号
    code: Mapped[str] = mapped_column(String)
    version: Mapped[int] = mapped_column(Integer, default=1)
    name: Mapped[str] = mapped_column(String)
    capability_id: Mapped[str] = mapped_column(String)
    # 空表示任何实现了该能力的型号都可以
    instrument_models: Mapped[list] = mapped_column(JSON, default=list)
    # 设备端程序 / 方法文件标识；驱动按它选择设备上的程序
    program: Mapped[str] = mapped_column(String, default="")
    # {参数: {default, min, max, unit}}
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    # 数据输出规则：[{key, label, unit, lo, hi, required}]；越界的值入库打标
    outputs: Mapped[list] = mapped_column(JSON, default=list)
    dur_min: Mapped[float] = mapped_column(Float, default=0)
    # draft | released | retired
    state: Mapped[str] = mapped_column(String, default="draft")
    note: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    released_by: Mapped[str] = mapped_column(String, default="")
    released_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    row_version: Mapped[int] = mapped_column(Integer, default=1)
    __table_args__ = (UniqueConstraint("org_id", "code", "version", name="uq_device_method_version"),)
