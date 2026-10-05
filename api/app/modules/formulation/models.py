from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from ...core.clock import now
from ...models.base import Base, uid


class FormulationTemplate(Base):
    """配液模板：一条配液线「怎么把一张配方表变成流程」。

    config 里是固定步骤（物料准备段、测试段）、加料阶段、物料类别 → 加法（能力、方法、用量参数）、
    搅拌规则与每次实验可配的参数；结构见 rules.py。模板不走发布：它只决定怎么生成
    流程草稿，生成的流程照旧走 评审 → 批准 → 发布，那才是受控点。改模板要乐观锁并留审计。
    """

    __tablename__ = "formulation_templates"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    code: Mapped[str] = mapped_column(String)
    name: Mapped[str] = mapped_column(String)
    description: Mapped[str] = mapped_column(Text, default="")
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    # active | retired
    state: Mapped[str] = mapped_column(String, default="active")
    created_by: Mapped[str] = mapped_column(String, default="")
    created_by_name: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    row_version: Mapped[int] = mapped_column(Integer, default=1)
    __table_args__ = (UniqueConstraint("org_id", "code", name="uq_formulation_template_code"),)


class FormulationSubmission(Base):
    """上游系统经服务身份提交的一张配方表（`POST /runtime/formulation-templates/{编号}/imports`）。

    按提交方给的请求编号去重：(组织, 服务身份, 请求编号) 唯一，同一编号同一内容重发回放 `result`，内容不同拒绝。
    记下生成的方案与流程：提交方按请求编号查进度（方案审批、实验任务、批次）与已复核的结果。
    """

    __tablename__ = "formulation_submissions"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=uid)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True)
    service_id: Mapped[str] = mapped_column(String)
    source: Mapped[str] = mapped_column(String, default="")
    request_id: Mapped[str] = mapped_column(String)
    # 请求内容摘要（表格、实验参数、方案名称）：同一编号内容不同就拒绝
    digest: Mapped[str] = mapped_column(String)
    template_id: Mapped[str] = mapped_column(String)
    template_code: Mapped[str] = mapped_column(String)
    filename: Mapped[str] = mapped_column(String, default="")
    plan_id: Mapped[str] = mapped_column(String, index=True)
    recipe_id: Mapped[str] = mapped_column(String)
    # 首次提交的响应（回放用）
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    __table_args__ = (UniqueConstraint("org_id", "service_id", "request_id", name="uq_formulation_submission_request"),)
