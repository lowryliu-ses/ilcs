"""指标定义、回传事件、类型化结果与结果审核。

采集完成 ≠ 质量有效 ≠ 审核通过：三个维度分列，正式统计要求三者同时满足。
"""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from ..core.clock import now
from .base import Base, uid

VALUE_TYPES = ("number", "text", "enum")
QUALITIES = ("unassessed", "valid", "suspect", "invalid")
REVIEW_STATES = ("pending", "approved", "rejected")


class MetricDefinition(Base):
    """指标定义版本。已被结果引用的版本不可原位修改。"""

    __tablename__ = "metric_definitions"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    code: Mapped[str] = mapped_column(String)
    name: Mapped[str] = mapped_column(String)
    version: Mapped[str] = mapped_column(String, default="v1")
    value_type: Mapped[str] = mapped_column(String, default="number")
    unit: Mapped[str] = mapped_column(String, default="")
    method_version: Mapped[str] = mapped_column(String, default="")
    sample_types: Mapped[list] = mapped_column(JSON, default=list)
    # {"min": .., "max": .., "options": [..]} —— 允许范围是校验规则，不代表质量合格
    rules: Mapped[dict] = mapped_column(JSON, default=dict)
    state: Mapped[str] = mapped_column(String, default="active")  # active | retired
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    __table_args__ = (UniqueConstraint("org_id", "code", "version", name="uq_metric_code_version"),)


class IngestEvent(Base):
    """回传事件。唯一键 = 组织 + 认证来源 + 检测任务 + 事件编号。"""

    __tablename__ = "ingest_events"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    source: Mapped[str] = mapped_column(String)
    service_identity_id: Mapped[str] = mapped_column(String, default="")
    analysis_task_id: Mapped[str] = mapped_column(String)
    event_id: Mapped[str] = mapped_column(String)
    digest: Mapped[str] = mapped_column(String, default="")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    state: Mapped[str] = mapped_column(String, default="accepted")  # accepted | rejected
    response: Mapped[dict] = mapped_column(JSON, default=dict)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    __table_args__ = (
        UniqueConstraint("org_id", "source", "analysis_task_id", "event_id", name="uq_ingest_event"),
    )


class ResultValue(Base):
    """单指标结果。缺值就是没有记录，不写 0。"""

    __tablename__ = "result_values"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    analysis_task_id: Mapped[str] = mapped_column(String, index=True)
    physical_sample_id: Mapped[str] = mapped_column(String, default="")
    assignment_id: Mapped[str] = mapped_column(String, default="")
    metric_definition_id: Mapped[str] = mapped_column(ForeignKey("metric_definitions.id"))
    ingest_event_id: Mapped[str] = mapped_column(String, default="")
    value_num: Mapped[float | None] = mapped_column(nullable=True)
    value_text: Mapped[str] = mapped_column(String, default="")
    unit: Mapped[str] = mapped_column(String, default="")
    collected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    raw_file_id: Mapped[str] = mapped_column(String, default="")
    # 历史迁移保留的原始 URI；取不到原件时这里有值而 raw_file_id 为空，不生成替代曲线冒充原件
    source_ref: Mapped[str] = mapped_column(String, default="")
    parser_version: Mapped[str] = mapped_column(String, default="")
    result_version: Mapped[int] = mapped_column(Integer, default=1)
    revises_id: Mapped[str] = mapped_column(String, default="")
    superseded_by_id: Mapped[str] = mapped_column(String, default="")
    # 未测但已声明无法测得时写这里，不能悄悄满足「全部指标已采集」
    not_measured_reason: Mapped[str] = mapped_column(Text, default="")
    quality: Mapped[str] = mapped_column(String, default="unassessed")
    review_state: Mapped[str] = mapped_column(String, default="pending")
    # 自动打标：越界、逻辑冲突。[{code, message, rule_id?}]；不改变值，交审核下结论
    flags: Mapped[list] = mapped_column(JSON, default=list)
    # 测出这个值的设备：工位（如有）与回传声明的仪器序列号
    station_id: Mapped[str] = mapped_column(String, default="")
    instrument: Mapped[str] = mapped_column(String, default="")
    # legacy_unreviewed 标记历史迁移数据，不伪造审核人
    provenance: Mapped[str] = mapped_column(String, default="device")
    entered_by: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    row_version: Mapped[int] = mapped_column(Integer, default=1)
    __table_args__ = (
        UniqueConstraint(
            "analysis_task_id", "metric_definition_id", "result_version", name="uq_result_version"
        ),
    )


class ResultReview(Base):
    """审核记录。绑定确切的结果版本，本人不能审核本人录入的记录。"""

    __tablename__ = "result_reviews"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    result_value_id: Mapped[str] = mapped_column(ForeignKey("result_values.id"), index=True)
    result_version: Mapped[int] = mapped_column(Integer)
    reviewer_id: Mapped[str] = mapped_column(String)
    conclusion: Mapped[str] = mapped_column(String)  # approved | rejected
    quality: Mapped[str] = mapped_column(String)
    reason: Mapped[str] = mapped_column(Text, default="")
    signature_id: Mapped[str] = mapped_column(String, default="")
    decided_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class DataRule(Base):
    """前后逻辑校验规则：左指标 op 右指标 × factor + offset（或常数）。同一检测任务内比较。

    级别 flag 打标交审核；reject 整次拒收——只用于物理上不可能的组合（如库仑效率超过 100%）。
    """

    __tablename__ = "data_rules"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    name: Mapped[str] = mapped_column(String)
    # 指标代码（不绑版本：指标修订后规则照样适用）
    left_metric: Mapped[str] = mapped_column(String)
    op: Mapped[str] = mapped_column(String, default="<=")
    right_metric: Mapped[str] = mapped_column(String, default="")
    right_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    factor: Mapped[float] = mapped_column(Float, default=1.0)
    offset: Mapped[float] = mapped_column(Float, default=0.0)
    severity: Mapped[str] = mapped_column(String, default="flag")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    row_version: Mapped[int] = mapped_column(Integer, default=1)
