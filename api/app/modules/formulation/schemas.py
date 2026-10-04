"""配液模板的接口输入模型。"""
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ...schemas import Versioned

# 表格单元格：xlsx 里的数字是 float，文字与 csv 单元格是 str，空格子是 None
TableCell = str | float | None


class FormulationTemplateIn(BaseModel):
    """配液模板：固定步骤 + 加料阶段 + 物料类别 → 加法 + 搅拌规则 + 实验参数，config 结构见 rules.py。"""

    model_config = ConfigDict(extra="forbid")

    code: str
    name: str
    description: str = ""
    config: dict[str, Any]


class FormulationTemplatePatchIn(Versioned):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    description: str | None = None
    config: dict[str, Any] | None = None


class FormulationPreviewIn(BaseModel):
    """按已解析的表格（parse 返回的原样表格，或界面改了实验参数后重提）生成预览，不写库。
    实验参数是数，选项型的实验参数是选项文字。"""

    filename: str = Field(default="", max_length=200)
    table: list[list[TableCell]]
    params: dict[str, float | str] = {}


class FormulationCheckIn(BaseModel):
    """模板编辑器：按还没保存的配置核对问题；带了表格就顺便按它试算一次（不写库、不登记瓶子）。"""

    model_config = ConfigDict(extra="forbid")

    config: dict[str, Any]
    name: str = Field(default="", max_length=200)
    description: str = ""
    filename: str = Field(default="", max_length=200)
    table: list[list[TableCell]] | None = None
    params: dict[str, float | str] = {}


class FormulationImportIn(FormulationPreviewIn):
    """导入：服务端重新生成（不信任前端预览），一个事务里登记样本、建流程草稿（或沿用）与方案草稿。"""

    plan_name: str = Field(default="", max_length=200)
