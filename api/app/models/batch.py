from datetime import datetime

from sqlalchemy import (
    Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from ..core.clock import now
from .base import Base, uid


class Batch(Base):
    """批次。recipe_snapshot 创建后只读，配方修订不影响在途批次。"""

    __tablename__ = "batches"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    org_id: Mapped[str] = mapped_column(String, default="", index=True)
    plan_id: Mapped[str] = mapped_column(ForeignKey("plans.id"))
    plan_version: Mapped[int] = mapped_column(Integer, default=1)
    recipe_id: Mapped[str] = mapped_column(ForeignKey("recipes.id"))
    task_id: Mapped[str] = mapped_column(String, default="")
    state: Mapped[str] = mapped_column(String, default="planned")
    priority: Mapped[int] = mapped_column(Integer, default=2)
    operator: Mapped[str] = mapped_column(String, default="")
    note: Mapped[str] = mapped_column(Text, default="")
    recipe_snapshot: Mapped[dict] = mapped_column(JSON)
    plan_snapshot: Mapped[dict] = mapped_column(JSON)
    # 固化实际采用的 SOP 版本及附件摘要
    sop_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    current_step: Mapped[int] = mapped_column(Integer, default=0)
    failure_reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    held_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    row_version: Mapped[int] = mapped_column(Integer, default=1)


class Allocation(Base):
    """步骤级资源预约。transfer/clean 也占时间线，参与冲突检测。"""

    __tablename__ = "allocations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[str] = mapped_column(ForeignKey("batches.id"))
    step_index: Mapped[int] = mapped_column(Integer)
    station_id: Mapped[str] = mapped_column(ForeignKey("stations.id"))
    asset_id: Mapped[str] = mapped_column(String, default="")
    starts_at: Mapped[datetime] = mapped_column(DateTime)
    ends_at: Mapped[datetime] = mapped_column(DateTime)
    kind: Mapped[str] = mapped_column(String, default="work")  # work | transfer | clean


class Sample(Base):
    """运行分配：物理样本 × 批次 × 容器孔位 × 条件组 × 重复。

    实验属性（孔位、levels、对照）属于这一次运行；物理实体在 `physical_samples`，
    重测创建新的检测任务或新的运行分配，不覆盖原位置与结果。
    """

    __tablename__ = "samples"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    org_id: Mapped[str] = mapped_column(String, default="", index=True)
    physical_sample_id: Mapped[str] = mapped_column(String, default="", index=True)
    batch_id: Mapped[str] = mapped_column(ForeignKey("batches.id"))
    container_id: Mapped[str] = mapped_column(String, default="")
    well: Mapped[str] = mapped_column(String)
    position: Mapped[int] = mapped_column(Integer)
    condition_group: Mapped[str] = mapped_column(String, default="")
    condition_label: Mapped[str] = mapped_column(String, default="")
    repeat: Mapped[int] = mapped_column(Integer, default=1)
    levels: Mapped[list | None] = mapped_column(JSON, nullable=True)
    is_control: Mapped[bool] = mapped_column(Boolean, default=False)
    state: Mapped[str] = mapped_column(String, default="pending")  # pending|running|done|failed
    # 历史人工质量标记。新模型的权威质量在 ResultValue.quality 上；
    # 这里的值只作历史展示，不等于审核通过。
    quality: Mapped[str | None] = mapped_column(String, nullable=True)
    flag_note: Mapped[str] = mapped_column(Text, default="")
    station_id: Mapped[str] = mapped_column(String, default="")


class AnalysisTask(Base):
    """检测任务。创建时冻结要求指标集合，单个指标回传不能完成其他任务。"""

    __tablename__ = "analysis_tasks"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, default="", index=True)
    # 某次批次运行分配。独立物理样本没有运行分配，允许为空。
    sample_id: Mapped[str | None] = mapped_column(ForeignKey("samples.id"), nullable=True)
    physical_sample_id: Mapped[str] = mapped_column(String, default="", index=True)
    method: Mapped[str] = mapped_column(String)
    method_version: Mapped[str] = mapped_column(String, default="")
    # 冻结的要求指标：[metric_definition_id]
    required_metrics: Mapped[list] = mapped_column(JSON, default=list)
    round_no: Mapped[int] = mapped_column(Integer, default=1)
    # pending | collecting | collected | cancelled
    state: Mapped[str] = mapped_column(String, default="pending")
    external_ref: Mapped[str] = mapped_column(String, default="")
    retest_of: Mapped[str] = mapped_column(String, default="")
    created_by: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    row_version: Mapped[int] = mapped_column(Integer, default=1)


class Result(Base):
    """历史固定三指标结果。新数据走 ResultValue；这张表保留给历史批次读取。"""

    __tablename__ = "results"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, default="", index=True)
    sample_id: Mapped[str] = mapped_column(ForeignKey("samples.id"))
    task_id: Mapped[str] = mapped_column(String, default="")
    areal_density: Mapped[float | None] = mapped_column(Float, nullable=True)
    discharge_capacity: Mapped[float | None] = mapped_column(Float, nullable=True)
    retention: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw_uri: Mapped[str] = mapped_column(String, default="")
    checksum: Mapped[str] = mapped_column(String, default="")
    parser_version: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
