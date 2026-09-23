"""方法（配方）校验规则。提交、批准、创建批次前都用同一份判据。

判据按步骤类型分支：设备步骤校验能力匹配与参数，人工步骤校验记录表单，
等待步骤校验等待方式，审核步骤校验审核角色。不适用的字段不提示缺失——
否则一个纯人工流程会被「没有可承接工位」挡住。

图形化编辑器在本地复算同一套规则做即时反馈，但能不能提交由服务端这份说了算。
"""
from typing import Any

from .capability import StationSpec, out_of_range, stations_for_step
from .steps import (
    DEVICE, KIND_NAMES, KINDS, MANUAL, REVIEW, WAIT, consumes_materials, kind_of,
    manual_issues, needs_station, resource_demand, review_issues, step_id_of, wait_issues,
)

EDITABLE_STATES = {"draft"}
LIFECYCLE = {
    "draft": "review",
    "review": "approved",
    "approved": "released",
    "released": "retired",
}

CapabilitySpecs = dict[str, dict[str, Any]]


def capability_name(capabilities: CapabilitySpecs, capability_id: str) -> str:
    return (capabilities.get(capability_id) or {}).get("name") or capability_id


def device_issues(step: dict[str, Any], capabilities: CapabilitySpecs) -> list[str]:
    """设备步骤的完整性：能力已登记、参数齐全且属于该能力。"""
    issues: list[str] = []
    capability_id = step.get("cap") or ""
    spec = capabilities.get(capability_id)
    if spec is None:
        issues.append(f"能力 {capability_id or '未选择'} 未登记")
        defined: dict[str, str] = {}
    else:
        if spec.get("retired"):
            issues.append(f"能力「{spec.get('name') or capability_id}」已停用，不能用于新步骤")
        defined = spec.get("params") or {}

    params = step.get("params") or {}
    for key, label in defined.items():
        value = params.get(key)
        if value is None or value == "" or not isinstance(value, (int, float)) or isinstance(value, bool):
            issues.append(f"{label or key} 未填写")
    for key in params:
        if defined and key not in defined:
            issues.append(f"参数 {key} 不属于该能力")
    return issues


def step_issues(step: dict[str, Any], capabilities: CapabilitySpecs) -> list[str]:
    """与工位无关的完整性问题。返回空列表表示这一步本身填写完整。"""
    issues: list[str] = []
    if not str(step.get("name") or "").strip():
        issues.append("步骤名称为空")
    kind = kind_of(step)
    if step.get("kind") and step["kind"] not in KINDS:
        issues.append(f"步骤类型 {step['kind']} 不受支持")

    if kind == DEVICE:
        issues.extend(device_issues(step, capabilities))
    elif kind == MANUAL:
        issues.extend(manual_issues(step))
    elif kind == WAIT:
        issues.extend(wait_issues(step))
    elif kind == REVIEW:
        issues.extend(review_issues(step))

    # 时长：审核节点没有预定时长，其余三类都要
    if kind != REVIEW:
        dur = step.get("dur")
        if not isinstance(dur, (int, float)) or isinstance(dur, bool) or dur <= 0:
            issues.append("计划时长必须大于 0")

    hard = step.get("hard")
    if hard:
        if not str(hard.get("from") or "").strip():
            issues.append("硬时限缺少起算事件")
        gap = hard.get("maxGapMin")
        if not isinstance(gap, (int, float)) or isinstance(gap, bool) or gap <= 0:
            issues.append("硬时限最长间隔必须大于 0")
    return issues


def validate_steps(
    steps: list[dict[str, Any]],
    stations: list[StationSpec],
    capabilities: CapabilitySpecs,
) -> list[dict]:
    rows = []
    seen_ids: dict[str, int] = {}
    for index, step in enumerate(steps or []):
        kind = kind_of(step)
        step_id = step_id_of(step, index)
        issues = step_issues(step, capabilities)
        if step_id in seen_ids:
            issues.append(f"步骤标识 {step_id} 与第 {seen_ids[step_id] + 1} 步重复")
        seen_ids[step_id] = index

        requires_station = needs_station(step)
        fits = stations_for_step(stations, step) if requires_station else []
        blockers: list[str] = list(issues)
        if requires_station and not fits:
            for station in stations:
                blockers.extend(out_of_range(station, step))
        rows.append(
            {
                "index": index,
                "step_id": step_id,
                "kind": kind,
                "kind_label": KIND_NAMES.get(kind, kind),
                "name": step.get("name"),
                "cap": step.get("cap"),
                "cap_name": capability_name(capabilities, step.get("cap", "")),
                "params": step.get("params") or {},
                "dur": step.get("dur"),
                "hard": step.get("hard"),
                "form": step.get("form") or [],
                "wait_for": step.get("wait_for") or {},
                "review_role": step.get("review_role", ""),
                "needs_station": requires_station,
                "fits": [s.id for s in fits],
                # 不需要工位的步骤：没有可承接工位不算问题
                "ok": (not requires_station or bool(fits)) and not issues,
                "issues": issues,
                "blockers": blockers[:6],
            }
        )
    return rows


def is_valid(validation: list[dict]) -> bool:
    return all(row["ok"] for row in validation)


def recipe_checks(
    recipe_steps: list[dict[str, Any]], validation: list[dict], bom: list[dict], risk: str,
    sop_version_id: str = "", sop_label: str = "",
) -> list[dict]:
    """方法级检查清单。编辑器与详情页显示同一份，提交评审按前五项裁决。"""
    no_station = [
        row["index"] + 1 for row in validation if row["needs_station"] and not row["fits"]
    ]
    incomplete = [row["index"] + 1 for row in validation if row["issues"]]
    total = sum(float(step.get("dur") or 0) for step in recipe_steps or [])
    hard_steps = [s for s in recipe_steps or [] if s.get("hard")]
    hard_ok = all(str((s.get("hard") or {}).get("from") or "").strip() for s in hard_steps)
    demand = resource_demand(recipe_steps or [])
    # 只看显式声明消耗物料的步骤；没有声明就是「无需物料」
    material_steps = [step for step in (recipe_steps or []) if consumes_materials(step)]
    return [
        {
            "key": "steps",
            "label": "至少一个步骤",
            "ok": bool(recipe_steps),
            "detail": (
                f"{demand['total']} 步（设备 {demand['device']}、人工 {demand['manual']}、"
                f"等待 {demand['wait']}、审核 {demand['review']}），总时长 {total:g} min"
            ),
        },
        {
            "key": "stations",
            "label": "需要占用的步骤都有可承接工位",
            "ok": not no_station,
            "detail": f"第 {'、'.join(map(str, no_station))} 步参数超出全部工位极限" if no_station
                      else (
                          f"{demand['needs_station']} 个步骤需要工位，参数均在某一工位极限内"
                          if demand["needs_station"] else "本流程没有需要工位的步骤"
                      ),
        },
        {
            "key": "complete",
            "label": "各类节点的适用字段完整",
            "ok": not incomplete,
            "detail": f"第 {'、'.join(map(str, incomplete))} 步填写不完整" if incomplete else "无缺失",
        },
        {
            "key": "hard",
            "label": "硬时限步骤有起算事件",
            "ok": hard_ok,
            "detail": f"{len(hard_steps)} 步定义了硬时限" if hard_ok else "存在硬时限步骤未填写起算事件",
        },
        {
            "key": "bom",
            "label": "物料需求（BOM）",
            # 合法空 BOM：没有消耗物料的步骤就不需要 BOM
            "ok": bool(bom) or not material_steps,
            "detail": "、".join(f"{i.get('material')} {i.get('qty')}{i.get('unit')}" for i in bom or [])
                      or ("无需物料：本流程没有消耗物料的步骤" if not material_steps
                          else "存在消耗物料的步骤但未定义 BOM，排程前无法预留"),
        },
        {
            "key": "risk",
            "label": "风险评估编号",
            "ok": True,
            "detail": risk or "缺失：允许保存草稿，发布前必须补齐",
        },
        {
            "key": "sop",
            "label": "关联 SOP 版本",
            "ok": True,
            # 显示编号与版本，不显示版本 UUID：清单是给人看的
            "detail": (
                sop_label or sop_version_id
                or "未关联：允许保存草稿；需要受控作业指导的方法请补上"
            ),
        },
    ]


def next_state(current: str) -> str | None:
    return LIFECYCLE.get(current)
