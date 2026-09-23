"""四类步骤的字段契约与校验。

device 有设备命令，manual 有待办与结构化记录表单，wait 是定时或业务事件，
review 要批准才继续。人工、等待、审核步骤不创建假适配器，也不占工位——
除非显式声明「等待期间样本仍留在设备中」。
"""
from __future__ import annotations

from typing import Any

DEVICE = "device"
MANUAL = "manual"
WAIT = "wait"
REVIEW = "review"
KINDS = (DEVICE, MANUAL, WAIT, REVIEW)
KIND_NAMES = {DEVICE: "设备", MANUAL: "人工", WAIT: "等待", REVIEW: "审核"}

# 每类步骤适用哪些字段。不适用的字段即时校验时不提示缺失，服务端也不据此阻塞。
APPLICABLE: dict[str, set[str]] = {
    DEVICE: {"cap", "params", "dur", "hard", "resource"},
    MANUAL: {"dur", "form", "resource", "requires_signature", "qualification", "hard"},
    WAIT: {"dur", "wait_for", "hard"},
    REVIEW: {"review_role", "dur"},
}


def kind_of(step: dict[str, Any]) -> str:
    """旧步骤没有 kind，一律按设备步骤解释——它们本来就是。"""
    kind = (step or {}).get("kind") or DEVICE
    return kind if kind in KINDS else DEVICE


def step_id_of(step: dict[str, Any], index: int) -> str:
    """稳定步骤标识。缺失时按位置补一个，供历史快照读取使用。"""
    return str((step or {}).get("step_id") or f"s{index + 1:02d}")


def normalize(steps: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """读取方向的兜底：给历史快照补上 step_id 与 kind，不改库里的原文。"""
    rows = []
    for index, step in enumerate(steps or []):
        if not isinstance(step, dict):
            continue
        row = dict(step)
        row["kind"] = kind_of(step)
        row["step_id"] = step_id_of(step, index)
        rows.append(row)
    return rows


def assign_step_ids(steps: list[dict[str, Any]], used: set[str]) -> list[dict[str, Any]]:
    """写入方向：给新步骤分配从未用过的 step_id。

    已发布版本里的 step_id 不因显示顺序变化而复用，所以新 ID 从「用过的最大序号 + 1」起，
    不按当前下标算。
    """
    taken = set(used)
    counter = 0
    for existing in taken:
        if existing.startswith("s") and existing[1:].isdigit():
            counter = max(counter, int(existing[1:]))
    rows = []
    for step in steps or []:
        row = dict(step)
        row["kind"] = kind_of(step)
        current = str(row.get("step_id") or "")
        if not current or (current in taken and current not in {str(s.get("step_id")) for s in rows}):
            if not current:
                counter += 1
                current = f"s{counter:02d}"
                while current in taken:
                    counter += 1
                    current = f"s{counter:02d}"
        row["step_id"] = current
        taken.add(current)
        rows.append(row)
    return rows


def needs_station(step: dict[str, Any]) -> bool:
    """这一步要不要占工位。

    设备步骤当然要；人工步骤只有声明了工位资源才参与工位冲突检查；
    等待步骤默认不占，除非声明样本仍留在设备中（holds_station）。
    """
    kind = kind_of(step)
    resource = (step or {}).get("resource") or {}
    if kind == DEVICE:
        return True
    if kind == MANUAL:
        return bool(resource.get("station") or resource.get("capability"))
    if kind == WAIT:
        return bool(resource.get("holds_station"))
    return False


def consumes_materials(step: dict[str, Any]) -> bool:
    """只有显式声明消耗物料的步骤才要投料许可。

    默认是「不消耗」。反过来（设备步骤一律算消耗）会让纯人工或纯设备的空 BOM 流程
    被物料项永久拦住——这正是需求 DEV-07.3 点名的误拦。方法级是否要 BOM 由
    `recipe_rules.recipe_checks` 综合 BOM 与这些声明来判断。
    """
    if kind_of(step) in {WAIT, REVIEW}:
        return False
    return bool((step or {}).get("consumes_materials", False))


def resource_demand(steps: list[dict[str, Any]]) -> dict[str, int]:
    """按步骤资源需求计数，不再要求每一步都有设备工位。"""
    rows = normalize(steps)
    return {
        "total": len(rows),
        "needs_station": len([s for s in rows if needs_station(s)]),
        "device": len([s for s in rows if kind_of(s) == DEVICE]),
        "manual": len([s for s in rows if kind_of(s) == MANUAL]),
        "wait": len([s for s in rows if kind_of(s) == WAIT]),
        "review": len([s for s in rows if kind_of(s) == REVIEW]),
    }


def wait_issues(step: dict[str, Any]) -> list[str]:
    wait_for = (step or {}).get("wait_for") or {}
    mode = wait_for.get("mode") or ("duration" if step.get("dur") else "")
    issues: list[str] = []
    if mode == "duration":
        minutes = step.get("dur")
        if not isinstance(minutes, (int, float)) or isinstance(minutes, bool) or minutes <= 0:
            issues.append("等待时长必须大于 0")
    elif mode == "event":
        # 还没有任何入口会发出业务事件：这种等待节点一旦开跑就永远不会被唤醒。
        # 在事件接口落地前一律拒绝，而不是让批次停在一个等不来的节点上。
        issues.append("「业务事件」等待暂不支持：当前没有可发出该事件的入口，请改用固定时长")
    else:
        issues.append("等待方式未选择（当前只支持固定时长）")
    return issues


def manual_issues(step: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    fields = (step or {}).get("form") or []
    if not isinstance(fields, list) or not fields:
        issues.append("人工步骤必须定义结构化记录表单，至少一个字段")
        return issues
    seen: set[str] = set()
    for index, field in enumerate(fields):
        if not isinstance(field, dict):
            issues.append(f"表单第 {index + 1} 项格式不正确")
            continue
        key = str(field.get("key") or "").strip()
        if not key:
            issues.append(f"表单第 {index + 1} 项缺少字段标识")
        elif key in seen:
            issues.append(f"表单字段标识 {key} 重复")
        else:
            seen.add(key)
        if not str(field.get("label") or "").strip():
            issues.append(f"表单字段 {key or index + 1} 缺少显示名称")
        if field.get("type") not in {"number", "text", "bool", "enum", None}:
            issues.append(f"表单字段 {key} 的类型 {field.get('type')} 不受支持")
        if field.get("type") == "enum" and not (field.get("options") or []):
            issues.append(f"表单字段 {key} 是枚举但没有可选值")
    return issues


def review_issues(step: dict[str, Any]) -> list[str]:
    role = str((step or {}).get("review_role") or "").strip()
    if not role:
        return ["审核步骤必须指定审核角色"]
    if role not in {"qa", "researcher", "admin"}:
        return [f"审核角色 {role} 不在可选范围内"]
    return []


def missing_form_values(step: dict[str, Any], values: dict[str, Any]) -> list[str]:
    """人工提交的必填校验。缺项不推进，返回缺了哪些字段。"""
    missing: list[str] = []
    for field in (step or {}).get("form") or []:
        if not isinstance(field, dict) or not field.get("required", True):
            continue
        key = str(field.get("key") or "")
        label = field.get("label") or key
        value = (values or {}).get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(f"{label} 未填写")
            continue
        kind = field.get("type") or "text"
        if kind == "number" and not isinstance(value, (int, float)) or (kind == "number" and isinstance(value, bool)):
            missing.append(f"{label} 必须是数值")
        if kind == "enum" and value not in (field.get("options") or []):
            missing.append(f"{label} 取值不在可选范围内")
        if kind == "bool" and value is not True:
            missing.append(f"{label} 需要确认勾选")
    return missing
