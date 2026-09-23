from datetime import datetime

from sqlalchemy import (
    Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from ..core.clock import now
from .base import Base, uid


class Recipe(Base):
    """方法与配方。steps 绑定能力而非设备；hard 为结构化硬时限。

    步骤字段：`step_id` 稳定不复用、`kind` ∈ device|manual|wait|review、
    `dur` 预期时长、`resource` 资源需求、`inputs`/`outputs`、`timeout`。
    """

    __tablename__ = "recipes"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    org_id: Mapped[str] = mapped_column(String, default="", index=True)
    name: Mapped[str] = mapped_column(String)
    version: Mapped[str] = mapped_column(String)
    state: Mapped[str] = mapped_column(String)
    owner: Mapped[str] = mapped_column(String)
    updated: Mapped[str] = mapped_column(String)
    plate: Mapped[int] = mapped_column(Integer)
    risk: Mapped[str] = mapped_column(String, default="")
    design: Mapped[str] = mapped_column(String, default="")
    golden_batch_id: Mapped[str] = mapped_column(String, default="")
    parent: Mapped[str] = mapped_column(String, default="")
    needs_revision: Mapped[bool] = mapped_column(Boolean, default=False)
    sop_version_id: Mapped[str] = mapped_column(String, default="")
    bom: Mapped[list] = mapped_column(JSON, default=list)
    steps: Mapped[list] = mapped_column(JSON, default=list)
    history: Mapped[list] = mapped_column(JSON, default=list)
    diff: Mapped[list] = mapped_column(JSON, default=list)
    # 职责分离依据：稳定用户 ID，不用会改名的显示名
    author_user_id: Mapped[str] = mapped_column(String, default="")
    submitted_by: Mapped[str] = mapped_column(String, default="")
    # 这个方法（含派生来源）用过的全部 step_id；删掉的步骤 ID 也留在这里，永不复用
    used_step_ids: Mapped[list] = mapped_column(JSON, default=list)
    row_version: Mapped[int] = mapped_column(Integer, default=1)


class Plan(Base):
    """实验方案。plan_type 决定校验分支；审批状态与矩阵锁定分别维护。"""

    __tablename__ = "plans"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    org_id: Mapped[str] = mapped_column(String, default="", index=True)
    project_id: Mapped[str] = mapped_column(String, default="")
    name: Mapped[str] = mapped_column(String)
    recipe_id: Mapped[str] = mapped_column(ForeignKey("recipes.id"))
    owner: Mapped[str] = mapped_column(String)
    # 矩阵锁定：draft | locked。结构冻结，不代表已审批
    state: Mapped[str] = mapped_column(String)
    # 审批：draft | review | approved | rejected
    approval_state: Mapped[str] = mapped_column(String, default="draft")
    plan_type: Mapped[str] = mapped_column(String, default="matrix")
    version: Mapped[int] = mapped_column(Integer, default=1)
    created: Mapped[str] = mapped_column(String)
    goal: Mapped[str] = mapped_column(Text, default="")
    repeats: Mapped[int] = mapped_column(Integer, default=1)
    layout: Mapped[str] = mapped_column(String, default="sequential")
    seed: Mapped[int] = mapped_column(Integer, default=1)
    factors: Mapped[list] = mapped_column(JSON, default=list)
    control: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 单条件方案：显式样本数或样本清单
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    sample_ids: Mapped[list] = mapped_column(JSON, default=list)
    # 所需检测指标（指标定义版本 ID）与资源需求
    required_metrics: Mapped[list] = mapped_column(JSON, default=list)
    resource_requirements: Mapped[list] = mapped_column(JSON, default=list)
    method_version: Mapped[str] = mapped_column(String, default="")
    reject_reason: Mapped[str] = mapped_column(Text, default="")
    row_version: Mapped[int] = mapped_column(Integer, default=1)


class PlanVersion(Base):
    """方案版本。批准版本不可修改，修订生成新版本。"""

    __tablename__ = "plan_versions"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, default="", index=True)
    plan_id: Mapped[str] = mapped_column(ForeignKey("plans.id"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    state: Mapped[str] = mapped_column(String, default="draft")
    snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    author_id: Mapped[str] = mapped_column(String, default="")
    approver_id: Mapped[str] = mapped_column(String, default="")
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    signature_id: Mapped[str] = mapped_column(String, default="")
    reject_reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    __table_args__ = (UniqueConstraint("plan_id", "version", name="uq_plan_version"),)


class ExperimentTask(Base):
    """一次实验工作。首期每个任务对应一个执行批次，不与 Batch 竞争执行真相。

    执行阶段从 Batch / StepRun 派生，数据与报告阶段从相应对象派生，
    所以没有「把状态 PATCH 成完成」的入口。
    """

    __tablename__ = "experiment_tasks"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    plan_id: Mapped[str] = mapped_column(String, index=True)
    plan_version: Mapped[int] = mapped_column(Integer, default=1)
    plan_version_id: Mapped[str] = mapped_column(String, default="")
    title: Mapped[str] = mapped_column(String, default="")
    owner_user_id: Mapped[str] = mapped_column(String, default="")
    assignee_user_id: Mapped[str] = mapped_column(String, default="")
    reviewer_user_id: Mapped[str] = mapped_column(String, default="")
    batch_id: Mapped[str] = mapped_column(String, default="", index=True)
    sample_ids: Mapped[list] = mapped_column(JSON, default=list)
    due_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    priority: Mapped[int] = mapped_column(Integer, default=2)
    # unassigned | pending_accept | accepted | running | data_review | reporting | done | cancelled
    state: Mapped[str] = mapped_column(String, default="unassigned")
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cancel_reason: Mapped[str] = mapped_column(Text, default="")
    note: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    row_version: Mapped[int] = mapped_column(Integer, default=1)
    # 首期一个任务对应一个批次。部分唯一索引：还没绑定批次的任务可以有很多个，
    # 已绑定的不能重复——用完整唯一约束会让所有「待分配」任务互相撞车。
    __table_args__ = (
        Index(
            "uq_task_batch", "batch_id", unique=True,
            postgresql_where=text("batch_id <> ''"),
        ),
    )


class TaskAssignment(Base):
    """分配与转派留痕。转派保留原因，历史身份按稳定用户 ID 关联。"""

    __tablename__ = "task_assignments"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    task_id: Mapped[str] = mapped_column(ForeignKey("experiment_tasks.id"), index=True)
    from_user_id: Mapped[str] = mapped_column(String, default="")
    to_user_id: Mapped[str] = mapped_column(String, default="")
    action: Mapped[str] = mapped_column(String)  # assign | reassign | accept | cancel
    reason: Mapped[str] = mapped_column(Text, default="")
    actor_id: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
