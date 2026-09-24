"""多级审批的纯规则。

一次提交带一串审批级别（最多 5 级），每级可以指定审批人（不指定就是任何有审批权限的人）。
按级别顺序逐级审：前一级没通过，后一级不能审；任一级驳回，整次评审结束、方案回到「已驳回」。
职责分离：作者不能审任何一级；同一个人不能审两级（否则多级审批就退化成一个人签两次）。
"""
from __future__ import annotations

from typing import Any

MAX_LEVELS = 5


def build_levels(requested: list[dict] | None, default_label: str = "QA 审批") -> list[dict]:
    rows = [row for row in (requested or []) if isinstance(row, dict)]
    if not rows:
        rows = [{"label": default_label}]
    if len(rows) > MAX_LEVELS:
        raise ValueError(f"审批最多 {MAX_LEVELS} 级")
    return [
        {
            "level": index + 1, "label": str(row.get("label") or f"第 {index + 1} 级审批"),
            "assignee_id": str(row.get("assignee_id") or ""), "decided_by": "", "decided_at": None,
            "conclusion": "", "reason": "", "signature_id": "",
        }
        for index, row in enumerate(rows)
    ]


def current(levels: list[dict]) -> dict | None:
    """下一个待审的级别；全部通过返回 None。"""
    for row in levels or []:
        if not row.get("conclusion"):
            return row
    return None


def blockers(levels: list[dict], user_id: str, author_id: str, conclusion: str = "approved") -> list[str]:
    """这个人现在能不能审当前级别。

    指定审批人只约束「批准」：驳回任何有审批权限的人（作者除外）都可以——指定的人离职、
    调走或失去权限时，评审不至于永远卡住。
    """
    row = current(levels)
    if row is None:
        return ["各级审批都已完成"]
    reasons = []
    if author_id and user_id == author_id:
        reasons.append("不能审批本人编写的方案（职责分离）")
    if conclusion == "approved" and row.get("assignee_id") and row["assignee_id"] != user_id:
        reasons.append(f"第 {row['level']} 级（{row['label']}）指定了其他审批人")
    if any(other.get("decided_by") == user_id for other in levels if other is not row):
        reasons.append("同一个人不能审批两级")
    return reasons


def record(levels: list[dict], user_id: str, conclusion: str, at: str, reason: str = "",
           signature_id: str = "") -> tuple[list[dict], bool]:
    """记下当前级别的结论。返回（新的级别列表, 是否全部通过）。"""
    updated = [dict(row) for row in levels]
    for row in updated:
        if not row.get("conclusion"):
            row.update({"decided_by": user_id, "decided_at": at, "conclusion": conclusion, "reason": reason,
                        "signature_id": signature_id})
            break
    done = conclusion == "approved" and current(updated) is None
    return updated, done


def progress(levels: list[dict]) -> dict[str, Any]:
    passed = sum(1 for row in levels or [] if row.get("conclusion") == "approved")
    return {"passed": passed, "total": len(levels or []), "current": current(levels or [])}
