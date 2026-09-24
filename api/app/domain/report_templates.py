"""报告模板。模板决定报告包含哪些章节、按什么顺序；取数逻辑只有一套（`ReportService.build_content`），
所以换模板不会让同一个批次在两份报告里出现两种数。模板带版本，发布时写进固化快照。
"""
from __future__ import annotations

SECTION_TITLES = {
    "plan": "方案与目的",
    "method": "方法与 SOP 版本",
    "samples": "样本及来源",
    "resources": "人员与物料",
    "instruments": "仪器与设备方法",
    "execution": "执行与异常",
    "operation_log": "操作记录",
    "results": "结果表",
    "exclusions": "排除说明",
    "data_flags": "数据质量标记",
    "raw_files": "原始数据文件",
    "statistics": "统计",
    "conclusion": "结论",
    "approval": "复核与批准",
}

TEMPLATES: dict[str, dict] = {
    "standard": {
        "name": "完整实验报告", "version": "2.0",
        "description": "方案、方法、样本、人员物料、仪器、执行、结果、排除、数据标记、原始文件、统计与结论",
        "sections": ["plan", "method", "samples", "resources", "instruments", "execution", "results",
                     "exclusions", "data_flags", "raw_files", "statistics", "conclusion", "approval"],
    },
    "summary": {
        "name": "结果摘要", "version": "1.0",
        "description": "给项目方看的短报告：方案、方法版本、结果表、统计与结论",
        "sections": ["plan", "method", "results", "statistics", "conclusion", "approval"],
    },
    "audit": {
        "name": "质量审计报告", "version": "1.0",
        "description": "给 QA / 审计：方法与 SOP 版本、仪器与校准、执行与异常、完整操作记录、数据标记与原始文件",
        "sections": ["method", "samples", "instruments", "execution", "operation_log", "exclusions",
                     "data_flags", "raw_files", "conclusion", "approval"],
    },
}
DEFAULT = "standard"


def template(key: str | None) -> dict:
    chosen = key if key in TEMPLATES else DEFAULT
    row = TEMPLATES[chosen]
    return {"key": chosen, "name": row["name"], "version": row["version"], "sections": list(row["sections"])}


def template_version(key: str | None) -> str:
    row = template(key)
    return f"{row['key']}-{row['version']}"


def catalog() -> list[dict]:
    return [
        {"key": key, "name": row["name"], "version": row["version"], "description": row["description"],
         "sections": [{"key": section, "title": SECTION_TITLES[section]} for section in row["sections"]]}
        for key, row in TEMPLATES.items()
    ]
