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
