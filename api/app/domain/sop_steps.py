"""数字 SOP：SOP 里的结构化步骤，以及把它生成方法草稿。

SOP 步骤写的是「人照着做什么」：标题、说明、类型（设备 / 人工 / 等待 / 审核）、设备步骤的能力与参数、
时长、逐项核对清单。生成方法草稿时：
- 设备步骤 → 设备节点（能力、参数、时长照抄；要不要引用设备方法由方法作者在编辑器里再定）；
- 人工步骤 → 人工节点，说明与核对清单变成结构化记录表单（每条核对一个勾选项，说明作为备注字段的提示）；
- 等待 → 定时等待节点；审核 → 审核节点（默认 QA）。
生成的只是草稿：照常走方法校验、仿真、评审与批准，SOP 改了不会回头改已经生成的方法。
"""
from __future__ import annotations

from typing import Any

KINDS = ("device", "manual", "wait", "review")


def issues(steps: list[dict], capabilities: dict[str, dict]) -> list[str]:
    found: list[str] = []
    for index, step in enumerate(steps or [], start=1):
        if not isinstance(step, dict):
            found.append(f"第 {index} 步格式不对")
            continue
        if not str(step.get("title") or "").strip():
            found.append(f"第 {index} 步没有标题")
        kind = step.get("kind") or "manual"
        if kind not in KINDS:
            found.append(f"第 {index} 步类型 {kind} 不受支持")
        if kind == "device":
            capability = capabilities.get(step.get("capability") or "")
            if capability is None:
                found.append(f"第 {index} 步设备能力 {step.get('capability') or '（未选）'} 不存在")
            else:
                unknown = [key for key in (step.get("params") or {}) if key not in (capability.get("params") or {})]
                if unknown:
                    found.append(f"第 {index} 步参数 {'、'.join(unknown)} 不属于能力 {step.get('capability')}")
        try:
            if float(step.get("duration_min") or 0) < 0:
                found.append(f"第 {index} 步时长不能为负")
        except (TypeError, ValueError):
            found.append(f"第 {index} 步时长不是数字")
    return found


def to_recipe_steps(steps: list[dict]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for index, step in enumerate(steps or [], start=1):
        kind = step.get("kind") or "manual"
        title = str(step.get("title") or f"第 {index} 步")
        duration = float(step.get("duration_min") or 0)
        row: dict[str, Any] = {"step_id": f"s{index:02d}", "name": title, "dur": duration or 5}
        if kind == "device":
            row.update({"kind": "device", "cap": step.get("capability") or "", "params": dict(step.get("params") or {})})
        elif kind == "wait":
            row.update({"kind": "wait", "cap": "", "params": {}, "wait_for": {"mode": "duration"}})
        elif kind == "review":
            row.update({"kind": "review", "cap": "", "params": {}, "review_role": "qa"})
        else:
            checks = [str(item) for item in step.get("checks") or [] if str(item).strip()]
            form = [
                {"key": f"check_{position}", "label": text, "type": "bool", "required": True}
                for position, text in enumerate(checks, start=1)
            ]
            form.append({"key": "note", "label": "记录", "type": "text", "required": False,
                         "hint": str(step.get("instructions") or "")[:200]})
            row.update({"kind": "manual", "cap": "", "params": {}, "form": form})
        if step.get("instructions"):
            row["sop_instructions"] = str(step["instructions"])
        out.append(row)
    return out
