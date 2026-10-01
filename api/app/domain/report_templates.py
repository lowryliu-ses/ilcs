"""报告模板。模板决定报告包含哪些章节、按什么顺序；取数逻辑只有一套（`ReportService.build_content`），
所以换模板不会让同一个批次在两份报告里出现两种数。模板带版本，发布时写进固化快照。

「分批情况」只在一个方案分多批执行、在父任务上出合并报告时有内容（各批样本数与状态、按批统计与批次差异、
短缺与放弃记录）；单批报告渲染时跳过这一节，章节编号照常连续。「曲线」同理：只有曲线型指标（充放电曲线、谱图）
有正式结果时才有内容，每个曲线指标一张按样本叠加的图；没有就跳过。

组织自己的模板（`models.ReportTemplate`）章节清单写成 `[{key, title?}, {kind: "text", key: "text:…", title, body}]`：
内置章节可以改标题，固定文字章节（声明、方法说明）原样印出。报告内容里的模板快照（`snapshot`）仍用
「章节键列表」表达顺序，另带 `titles`（改过的标题）与 `texts`（文字章节）——老报告只有键列表，照旧能渲染。
"""
from __future__ import annotations

SECTION_TITLES = {
    "plan": "方案与目的",
    "method": "流程与 SOP 版本",
    "samples": "样本及来源",
    "batches": "分批情况",
    "resources": "人员与物料",
    "instruments": "仪器与设备方法",
    "execution": "执行与异常",
    "operation_log": "操作记录",
    "results": "结果表",
    "exclusions": "排除说明",
    "data_flags": "数据质量标记",
    "raw_files": "原始数据文件",
    "statistics": "统计",
    "curves": "曲线",
    "conclusion": "结论",
    "approval": "复核与批准",
}

TEMPLATES: dict[str, dict] = {
    "standard": {
        "name": "完整实验报告", "version": "2.2",
        "description": "方案、流程、样本、分批情况、人员物料、仪器、执行、结果、排除、数据标记、原始文件、统计、曲线与结论",
        "sections": ["plan", "method", "samples", "batches", "resources", "instruments", "execution", "results",
                     "exclusions", "data_flags", "raw_files", "statistics", "curves", "conclusion", "approval"],
    },
    "summary": {
        "name": "结果摘要", "version": "1.2",
        "description": "给项目方看的短报告：方案、流程版本、分批情况、结果表、统计、曲线与结论",
        "sections": ["plan", "method", "batches", "results", "statistics", "curves", "conclusion", "approval"],
    },
    "audit": {
        "name": "质量审计报告", "version": "1.1",
        "description": "给 QA / 审计：流程与 SOP 版本、仪器与校准、分批情况、执行与异常、完整操作记录、数据标记与原始文件",
        "sections": ["method", "samples", "batches", "instruments", "execution", "operation_log", "exclusions",
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


BUILTIN_KEYS = frozenset(TEMPLATES)
KEY_PATTERN = r"^[a-z][a-z0-9_-]{1,31}$"
MAX_SECTIONS = 40
TITLE_LIMIT = 40
BODY_LIMIT = 4000


def section_issues(sections) -> list[str]:
    """自定义模板的章节清单：至少一节；内置章节键要存在、不重复；文字章节要有标题与正文。全部问题一次列出。"""
    if not isinstance(sections, list) or not sections:
        return ["至少要有一个章节"]
    issues: list[str] = []
    if len(sections) > MAX_SECTIONS:
        issues.append(f"最多 {MAX_SECTIONS} 个章节")
    seen: set[str] = set()
    for number, row in enumerate(sections, start=1):
        if not isinstance(row, dict):
            issues.append(f"第 {number} 节格式不正确")
            continue
        title = row.get("title")
        if title is not None and (not isinstance(title, str) or len(title.strip()) > TITLE_LIMIT):
            issues.append(f"第 {number} 节的标题要是不超过 {TITLE_LIMIT} 个字的文字")
        if row.get("kind") == "text":
            key = str(row.get("key") or "")
            if not key.startswith("text:") or len(key) <= 5:
                issues.append(f"第 {number} 节是文字章节，键要写成 text:…")
            if not isinstance(title, str) or not title.strip():
                issues.append(f"第 {number} 节是文字章节，要有标题")
            body = row.get("body")
            if not isinstance(body, str) or not body.strip():
                issues.append(f"第 {number} 节（{title or '文字章节'}）要有正文")
            elif len(body) > BODY_LIMIT:
                issues.append(f"第 {number} 节（{title}）正文超过 {BODY_LIMIT} 个字")
        else:
            key = str(row.get("key") or "")
            if key not in SECTION_TITLES:
                issues.append(f"第 {number} 节 {key or '（未选）'} 不是内置章节")
        if key in seen:
            issues.append(f"章节 {key} 重复")
        seen.add(key)
    return issues


def clean_sections(sections: list[dict]) -> list[dict]:
    """入库前的写法：标题去空白，和内置标题相同就不存；文字章节只留键、标题、正文。"""
    out: list[dict] = []
    for row in sections:
        if row.get("kind") == "text":
            out.append({"kind": "text", "key": row["key"], "title": row["title"].strip(), "body": row["body"].strip()})
            continue
        entry = {"key": row["key"]}
        title = (row.get("title") or "").strip()
        if title and title != SECTION_TITLES[row["key"]]:
            entry["title"] = title
        out.append(entry)
    return out


def snapshot(key: str, name: str, version: int | str, sections: list[dict]) -> dict:
    """写进报告内容的模板快照：章节键列表 + 改过的标题 + 文字章节。"""
    return {
        "key": key, "name": name, "version": str(version),
        "sections": [row["key"] for row in sections],
        "titles": {row["key"]: row["title"] for row in sections if row.get("kind") != "text" and row.get("title")},
        "texts": {row["key"]: {"title": row["title"], "body": row["body"]} for row in sections if row.get("kind") == "text"},
    }


def builtin_sections() -> list[dict]:
    """可选的内置章节（编辑器的候选）。"""
    return [{"key": key, "title": title} for key, title in SECTION_TITLES.items()]


def title_of(content_template: dict, key: str) -> str:
    """一节的标题：文字章节取它自己的，改过标题的取改过的，其余取内置标题。"""
    texts = (content_template or {}).get("texts") or {}
    if key in texts:
        return texts[key].get("title") or key
    return ((content_template or {}).get("titles") or {}).get(key) or SECTION_TITLES.get(key, key)
