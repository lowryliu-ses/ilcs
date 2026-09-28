"""数字 SOP：SOP 里的结构化步骤，以及把它生成方法草稿。

SOP 步骤写的是「人照着做什么」：标题、说明、类型（设备 / 人工 / 等待 / 审核）、设备步骤的能力与参数、
时长、逐项核对清单。生成方法草稿时：
- 设备步骤 → 设备节点（能力、参数、时长照抄；要不要引用设备方法由方法作者在编辑器里再定）；
- 人工步骤 → 人工节点，说明与核对清单变成结构化记录表单（每条核对一个勾选项，说明作为备注字段的提示）；
- 等待 → 定时等待节点；审核 → 审核节点（默认 QA）。
每个生成的节点记下 `sop_step_key`（SOP 步骤的稳定标识）与 `sop_step`（序号，从 1 起，只作显示与旧数据回退），
批次页据此把 SOP 说明带到执行人面前；手写的流程也可以在编辑器里给节点选对应的 SOP 步骤。

步骤标识不随位置变：新版本在前面插入一步，旧流程节点仍然对得上原来那一步。解析顺序见 `resolve_step`。
生成的只是草稿：照常走方法校验、仿真、评审与批准，SOP 改了不会回头改已经生成的方法。
"""
from __future__ import annotations

from typing import Any
from uuid import uuid4

from .steps import kind_of

# 解析结果：流程节点引用的 SOP 步骤在批次采用的版本里找不到（或对不上唯一的一步）
BROKEN = "broken"

KINDS = ("device", "manual", "wait", "review")


def scope_outside(steps: list[dict], scope) -> list[str]:
    """设备步骤里不在 SOP 适用能力范围内的能力。SOP 没写范围就不限制。"""
    allowed = set(scope or [])
    if not allowed:
        return []
    return sorted({
        str(step.get("cap")) for step in steps or []
        if kind_of(step) == "device" and step.get("cap") and step.get("cap") not in allowed
    })


def governed_steps(steps: list[dict], group_id: str | None, sop_groups: set[str]) -> list[dict]:
    """某一版 SOP 管哪些步骤（批次快照里子流程已经展开，步骤带 groups 归属）。

    主流程的 SOP（`group_id` 为空）管不属于任何子流程的步骤；子流程的 SOP 管最近一层带 SOP 的归属
    就是它的那些步骤。子流程没有自己的 SOP 时，它的步骤不归主流程 SOP 管，与流程发布时的口径一致。
    """
    governed = []
    for step in steps or []:
        groups = [str(group.get("step_id") or "") for group in step.get("groups") or []]
        if group_id is None:
            if not groups:
                governed.append(step)
            continue
        nearest = next((group for group in reversed(groups) if group in sop_groups), None)
        if nearest == group_id:
            governed.append(step)
    return governed


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
        # 记下对应的 SOP 步骤：批次页按它从固化的 SOP 快照里取说明与核对项给执行人看。标识不随位置变，
        # 序号只作显示与没有标识的旧数据回退
        row["sop_step"] = index
        if step.get("key"):
            row["sop_step_key"] = str(step["key"])
        if step.get("instructions"):
            row["sop_instructions"] = str(step["instructions"])
        out.append(row)
    return out


def with_keys(steps: list[dict]) -> list[dict]:
    """给结构化步骤补稳定标识：已有的原样保留，缺的生成；复制出来的重复标识给后一个换新的。"""
    seen: set[str] = set()
    keyed: list[dict] = []
    for step in steps or []:
        row = dict(step)
        key = str(row.get("key") or "").strip()
        while not key or key in seen:
            key = uuid4().hex[:8]
        seen.add(key)
        row["key"] = key
        keyed.append(row)
    return keyed


def compact(steps: list[dict]) -> list[dict]:
    """解析映射用的步骤摘要：标识、类型、能力、标题。"""
    return [
        {"key": str(row.get("key") or ""), "kind": row.get("kind") or "manual",
         "capability": row.get("capability") or "", "title": str(row.get("title") or "")}
        for row in steps or []
    ]


def _position(step: dict) -> int:
    try:
        return int(step.get("sop_step") or 0)
    except (TypeError, ValueError):
        return 0


def _same_kind(row: dict, step: dict) -> bool:
    kind = kind_of(step)
    if (row.get("kind") or "manual") != kind:
        return False
    return kind != "device" or (row.get("capability") or "") == (step.get("cap") or "")


def resolve_step(step: dict, snapshot: dict | None):
    """流程节点在批次采用的 SOP 版本里对应哪一步：返回 (序号, 步骤)；没有引用返回 None；对不上返回 BROKEN。

    依次按：
    1. 稳定标识（`sop_step_key`）在采用版本里找；
    2. 没有标识的旧节点：采用版本就是流程关联的版本时按序号；采用了新版本时，经关联版本那一步的标识找；
    3. 以上都对不上：采用版本里类型（设备步骤还要能力）相同的只有一步，就是它；有几步时再按标题区分；
    4. 仍对不上就是映射失效——不能按数组位置硬套，那会把别的步骤的说明给执行人看。
    """
    snapshot = snapshot or {}
    rows = snapshot.get("steps") or []
    key = str(step.get("sop_step_key") or "")
    position = _position(step)
    if not key and not position:
        return None
    by_key = {str(row.get("key") or ""): (at, row) for at, row in enumerate(rows, start=1) if row.get("key")}
    if key and key in by_key:
        return by_key[key]
    if position and not snapshot.get("linked_version_id") and not key:
        if 1 <= position <= len(rows):
            return position, rows[position - 1]
    linked = snapshot.get("linked_steps") or []
    if position and 1 <= position <= len(linked):
        linked_key = str(linked[position - 1].get("key") or "")
        if linked_key and linked_key in by_key:
            return by_key[linked_key]
    candidates = [(at, row) for at, row in enumerate(rows, start=1) if _same_kind(row, step)]
    if len(candidates) > 1:
        titled = [(at, row) for at, row in candidates if str(row.get("title") or "") == str(step.get("name") or "")]
        candidates = titled
    if len(candidates) == 1:
        return candidates[0]
    return BROKEN


def mapping_issues(steps: list[dict], snapshot: dict | None) -> list[str]:
    """流程节点里引用了 SOP 步骤、却在这一版里对不上的节点名称。"""
    return [
        str(step.get("name") or step.get("step_id") or "")
        for step in steps or [] if resolve_step(step, snapshot) == BROKEN
    ]


def step_guide(step: dict, snapshot: dict | None) -> dict | None:
    """批次里某个节点对应的 SOP 指导：从批次固化的 SOP 快照里取（解析规则见 `resolve_step`）。

    对不上时明示「映射失效」，不给别的步骤的说明；只剩生成时抄下的说明时照样给出，并注明它来自生成时。
    """
    resolved = resolve_step(step, snapshot)
    if resolved not in (None, BROKEN):
        position, row = resolved
        return {
            "index": position, "title": str(row.get("title") or ""),
            "instructions": str(row.get("instructions") or ""),
            "checks": [str(item) for item in row.get("checks") or []],
        }
    if resolved == BROKEN:
        version = (snapshot or {}).get("version") or ""
        return {
            "index": 0, "title": "", "instructions": str(step.get("sop_instructions") or ""), "checks": [],
            "mapping_broken": True,
            "message": f"这一步对应的 SOP 步骤在批次采用的 {version} 里找不到，说明可能已过时，请核对 SOP 原文",
        }
    if step.get("sop_instructions"):
        return {"index": 0, "title": "", "instructions": str(step["sop_instructions"]), "checks": []}
    return None
