from datetime import datetime

from sqlalchemy import (
    Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from ..core.clock import now
from .base import Base, uid


class Recipe(Base):
    """实验流程。steps 绑定能力而非设备；hard 为结构化硬时限。

    步骤字段：`step_id` 稳定不复用；`kind` 见 `domain/steps.KINDS`（设备、人工、等待、审核、质检关卡、
    样本拆分、条件分支、子流程、消息通知）；`dur` 预期时长；`after` / `when` 依赖与分支出口；
    `timeout` 步骤级超时；`method` 引用的设备方法；`environment` 环境要求；`resource` 资源需求。
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
    # 最近一次执行前仿真的结论（含方法内容摘要）；内容改过就失效，提交评审与批准前会重新跑
    simulation: Mapped[dict] = mapped_column(JSON, default=dict)
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
    # 显式设计点：每项是与 factors 对齐的一组水平。非空时条件就是这些点，不再做全因子组合——
    # 优化器提出的下一轮流程是一组离散的点，不是网格
    design_points: Mapped[list] = mapped_column(JSON, default=list)
    # 设计空间：{"bounds": {因子名: {"min", "max"}}, "forbidden": [{因子名: 水平}], "max_points": N}。
    # 随方案审批冻结；外部提案超出它一律拒绝
    design_space: Mapped[dict] = mapped_column(JSON, default=dict)
    # 闭环实验活动：由哪一轮提案生成、第几轮
    parent_plan_id: Mapped[str] = mapped_column(String, default="")
    round_no: Mapped[int] = mapped_column(Integer, default=1)


class PlanProposal(Base):
    """外部优化器（或研究员）提交的下一轮实验提案。

    提案本身不直接变成可执行的实验：校验通过只生成方案草稿，仍需锁定、提交并由 QA 批准。
    被拒绝的提案也留档——为什么拒、拒了哪些点，是闭环调参要看的东西。
    """

    __tablename__ = "plan_proposals"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    plan_id: Mapped[str] = mapped_column(String, index=True)
    proposal_key: Mapped[str] = mapped_column(String)
    digest: Mapped[str] = mapped_column(String, default="")
    source: Mapped[str] = mapped_column(String, default="")
    model_version: Mapped[str] = mapped_column(String, default="")
    rationale: Mapped[str] = mapped_column(Text, default="")
    points: Mapped[list] = mapped_column(JSON, default=list)
    # accepted | rejected
    state: Mapped[str] = mapped_column(String, default="accepted")
    issues: Mapped[list] = mapped_column(JSON, default=list)
    created_plan_id: Mapped[str] = mapped_column(String, default="")
    created_by: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    __table_args__ = (UniqueConstraint("org_id", "plan_id", "proposal_key", name="uq_plan_proposal_key"),)


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
    # 多级审批：[{level, label, assignee_id, decided_by, decided_at, conclusion, reason, signature_id}]
    approvals: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    __table_args__ = (UniqueConstraint("plan_id", "version", name="uq_plan_version"),)


class PlanTemplate(Base):
    """方案模板：常用的方案结构（类型、因子与水平、重复、布局、指标、资源需求），新建方案时一键套用。"""

    __tablename__ = "plan_templates"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(String, default="", index=True)
    name: Mapped[str] = mapped_column(String)
    description: Mapped[str] = mapped_column(Text, default="")
    plan_type: Mapped[str] = mapped_column(String, default="matrix")
    # 建议使用的方法（可空）；套用时方法仍由新建人选
    recipe_id: Mapped[str] = mapped_column(String, default="")
    body: Mapped[dict] = mapped_column(JSON, default=dict)
    source_plan_id: Mapped[str] = mapped_column(String, default="")
    retired: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    row_version: Mapped[int] = mapped_column(Integer, default=1)


class ExperimentTask(Base):
    """一次实验工作。叶子任务对应一个执行批次，不与 Batch 竞争执行真相。

    执行阶段从 Batch / StepRun 派生，数据与报告阶段从相应对象派生，
    所以没有「把状态 PATCH 成完成」的入口。

    任务可以组成树（`parent_id`）：父任务是订单 / 实验活动这一层的容器，不绑定批次，
    状态由子任务汇总；子任务各自对应一个批次。任务之间可以声明先后（`depends_on`）：
    上游任务的批次运行结束之前，下游任务的批次不能下发，排程也不会把它排到上游结束之前。
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
    # 任务树：父任务编号；空串表示顶层任务
    parent_id: Mapped[str] = mapped_column(String, default="", index=True)
    # 上游任务编号：它们的批次运行结束后本任务才能下发（完成—开始约束）
    depends_on: Mapped[list] = mapped_column(JSON, default=list)
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
