"""组织报告模板的章节规则：内置章节键、文字章节、去重、快照与标题。"""
from app.domain import report_templates as rules


def test_section_rules_and_snapshot():
    sections = [{"key": "plan", "title": "方案与目的"}, {"key": "results", "title": " 检测结果 "},
                {"kind": "text", "key": "text:scope", "title": "适用范围", "body": " 只用于内部研发 "}]
    assert rules.section_issues(sections) == []
    cleaned = rules.clean_sections(sections)
    assert cleaned == [{"key": "plan"}, {"key": "results", "title": "检测结果"},
                       {"kind": "text", "key": "text:scope", "title": "适用范围", "body": "只用于内部研发"}], "和内置标题相同的不存"
    snap = rules.snapshot("lab", "内部报告", 3, cleaned)
    assert snap == {"key": "lab", "name": "内部报告", "version": "3", "sections": ["plan", "results", "text:scope"],
                    "titles": {"results": "检测结果"}, "texts": {"text:scope": {"title": "适用范围", "body": "只用于内部研发"}}}
    assert rules.title_of(snap, "results") == "检测结果" and rules.title_of(snap, "plan") == "方案与目的"
    assert rules.title_of(snap, "text:scope") == "适用范围" and rules.title_of({}, "results") == "结果表"

    assert rules.section_issues([]) == ["至少要有一个章节"]
    issues = rules.section_issues([{"key": "plan"}, {"key": "plan"}, {"kind": "text", "key": "scope", "title": "x", "body": "y"},
                                   {"kind": "text", "key": "text:a", "title": " ", "body": ""}, {"key": "x" * 3}, "bad"])
    assert "章节 plan 重复" in issues
    assert "第 3 节是文字章节，键要写成 text:…" in issues
    assert "第 4 节是文字章节，要有标题" in issues and any("要有正文" in item for item in issues)
    assert "第 5 节 xxx 不是内置章节" in issues and "第 6 节格式不正确" in issues
