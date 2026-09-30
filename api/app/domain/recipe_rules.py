"""流程校验规则。提交、批准、创建批次前都用同一份判据。

判据按步骤类型分支：设备步骤校验能力匹配与参数，人工步骤校验记录表单，
等待步骤校验等待方式，审核步骤校验审核角色。不适用的字段不提示缺失——
否则一个纯人工流程会被「没有可承接工位」挡住。

图形化编辑器在本地复算同一套规则做即时反馈，但能不能提交由服务端这份说了算。
"""
from typing import Any

from .bindings import binding_issues, bindings_of
from .capability import StationSpec, out_of_range, stations_for_step
from .environment import requirement_issues
from .graph import ancestors, critical_path_min, graph_issues, graph_mode
from .params import spec_of, value_issues
from .steps import (
    AUTOMATIC_KINDS, BRANCH, DEVICE, GATE, KIND_NAMES, KINDS, MANUAL, NOTIFY, REVIEW, SPLIT, SUBFLOW, WAIT,
    applies_to_issues, assist_issues, branch_issues, notify_issues,
    consumes_materials, gate_issues, kind_of, manual_issues, material_issues, needs_station, resource_demand,
    review_issues, step_material,
    skippable_issues, split_issues, step_id_of, subflow_issues, timeout_issues, wait_issues,
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
    """设备步骤的完整性：能力已登记、参数齐全且属于该能力、值符合参数规格。

    取自上游结果的参数（前馈）算已提供，它的配置由 `bindings.binding_issues` 另行核对；
    能力里标了非必填的参数可以不写，设备按自己的缺省值执行。
    """
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
    bound = set(bindings_of(step))
    for key in defined:
        rule = spec_of(spec, key)
        if key in bound:
            continue
        value = params.get(key)
        if value is None or value == "" or not isinstance(value, (int, float)) or isinstance(value, bool):
            if rule["required"]:
                issues.append(f"{rule['label']} 未填写")
            continue
        issues.extend(value_issues(rule, value))
    for key in params:
        if defined and key not in defined:
            issues.append(f"参数 {key} 不属于该能力")
    # 用量参数：执行器按它从下发参数里取这一步的投料量，再按物料单位对账，所以必须是登记了单位的能力参数
    material_param = step.get("material_param")
    if material_param not in (None, "") and spec is not None:
        if not isinstance(material_param, str) or material_param not in defined:
            issues.append(f"用量参数 {material_param} 不是该能力的参数")
        elif not spec_of(spec, material_param)["unit"]:
            issues.append(f"用量参数 {material_param} 没有登记单位，无法与物料单位对账")
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
        issues.extend(assist_issues(step, capabilities))
    elif step.get("assist"):
        issues.append("只有设备步骤可以声明协同资源")
    elif kind == MANUAL:
        issues.extend(manual_issues(step))
    elif kind == WAIT:
        issues.extend(wait_issues(step))
    elif kind == REVIEW:
        issues.extend(review_issues(step))
    elif kind == SPLIT:
        issues.extend(split_issues(step))
    elif kind == SUBFLOW:
        issues.extend(subflow_issues(step))
    elif kind == NOTIFY:
        issues.extend(notify_issues(step))
    issues.extend(material_issues(step))
    issues.extend(timeout_issues(step))
    issues.extend(skippable_issues(step))
    issues.extend(requirement_issues(step, needs_zone=not needs_station(step)))

    # 时长：审核、质检关卡、样本拆分、条件分支是即时判定 / 登记，子流程的时长来自它引用的方法
    if kind not in AUTOMATIC_KINDS:
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
    subflow_problems: dict[str, list[str]] | None = None,
    method_problems: dict[str, list[str]] | None = None,
) -> list[dict]:
    """逐步校验。`subflow_problems` 是服务层展开子流程引用得到的问题（按步骤标识），
    这里不碰数据库，所以引用的方法存不存在、有没有发布由调用方查好传进来。"""
    rows = []
    seen_ids: dict[str, int] = {}
    dependency = graph_issues(steps or [])
    ids = [step_id_of(item, position) for position, item in enumerate(steps or [])]
    for index, step in enumerate(steps or []):
        kind = kind_of(step)
        step_id = step_id_of(step, index)
        issues = step_issues(step, capabilities)
        issues.extend(dependency.get(index, []))
        if kind == GATE:
            # 关卡要看前后步骤，只能在整条流程上校验
            issues.extend(gate_issues(step, steps, index))
            target = ((step.get("gate") or {}).get("rework_to") or "")
            if graph_mode(steps) and target in ids and ids.index(target) not in ancestors(steps, index):
                issues.append("返工目标必须是本关卡的上游步骤（依赖链上的前驱）")
        if kind == BRANCH:
            issues.extend(branch_issues(step, steps, index))
        # 前馈来源、按瓶限定引用的投料步骤都要看上下游步骤，同样只能在整条流程上校验
        issues.extend(binding_issues(step, steps, index, capabilities))
        issues.extend(applies_to_issues(step, steps, index))
        if kind == SUBFLOW:
            issues.extend((subflow_problems or {}).get(step_id, []))
        issues.extend((method_problems or {}).get(step_id, []))
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
                "bindings": step.get("bindings") or {},
                "dur": step.get("dur"),
                "hard": step.get("hard"),
                "form": step.get("form") or [],
                "wait_for": step.get("wait_for") or {},
                "review_role": step.get("review_role", ""),
                "gate": step.get("gate") or {},
                "split": step.get("split") or {},
                "assist": step.get("assist") or [],
                "labware": step.get("labware") or "",
                "branch": step.get("branch") or {},
                "subflow": step.get("subflow") or {},
                "method": step.get("method") or {},
                "when": step.get("when") or {},
                "timeout": step.get("timeout") or None,
                "skippable": bool(step.get("skippable")),
                "after": step.get("after") if "after" in step else None,
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
    sop_version_id: str = "", sop_label: str = "", expanded_critical_min: float | None = None,
    sop_problems: list[str] | None = None,
) -> list[dict]:
    """流程级检查清单。编辑器与详情页显示同一份，提交评审按前五项裁决。"""
    no_station = [
        row["index"] + 1 for row in validation if row["needs_station"] and not row["fits"]
    ]
    incomplete = [row["index"] + 1 for row in validation if row["issues"]]
    total = sum(float(step.get("dur") or 0) for step in recipe_steps or [])
    parallel = graph_mode(recipe_steps or [])
    # 有子流程时关键路径按展开后的步骤算：子流程节点本身没有时长
    critical = expanded_critical_min if expanded_critical_min is not None else critical_path_min(recipe_steps or [])
    hard_steps = [s for s in recipe_steps or [] if s.get("hard")]
    hard_ok = all(str((s.get("hard") or {}).get("from") or "").strip() for s in hard_steps)
    demand = resource_demand(recipe_steps or [])
    # 只看显式声明消耗物料的步骤；没有声明就是「无需物料」
    material_steps = [step for step in (recipe_steps or []) if consumes_materials(step)]
    bom_ok, bom_detail = _bom_check(material_steps, bom or [])
    return [
        {
            "key": "steps",
            "label": "至少一个步骤",
            "ok": bool(recipe_steps),
            "detail": (
                f"{demand['total']} 步（设备 {demand['device']}、人工 {demand['manual']}、"
                f"等待 {demand['wait']}、审核 {demand['review']}"
                + (f"、分支 {demand['branch']}" if demand["branch"] else "")
                + (f"、子流程 {demand['subflow']}" if demand["subflow"] else "")
                + "），"
                + (f"关键路径 {critical:g} min（各步合计 {total:g} min，含并行与分支）" if parallel
                   else f"总时长 {total:g} min")
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
            "ok": bom_ok,
            "detail": bom_detail,
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
            # 不关联允许；关联了就得可用：同编号有生效版本，设备能力在适用范围内
            "ok": not sop_problems,
            # 显示编号与版本，不显示版本 UUID：清单是给人看的
            "detail": "；".join([
                sop_label or sop_version_id or "未关联：允许保存草稿；需要受控作业指导的流程请补上",
                *(sop_problems or []),
            ]),
        },
    ]


def manual_materials_outside_bom(steps: list[dict[str, Any]], bom: list[dict] | None) -> str:
    """人工步骤投的物料没列在 BOM 里的，逐步给出原因（整句，「；」连接）；都列了返回空串。

    「用量由方案给出」只对设备步骤成立：方案因子只能作用在设备参数上，人工步骤没有可下发的用量，
    消耗也不会由设备回报——放过去的话流程能发布，基于它的方案却永远锁不上。所以人工步骤的料只能按 BOM 预留。
    提交评审、批准都按这里的文字拦，前端 rules.ts 照抄同一句。
    """
    listed = {str(item.get("material") or "") for item in bom or []}
    rows = []
    for step in steps or []:
        name = step_material(step)
        if kind_of(step) == MANUAL and name and name not in listed:
            rows.append(
                f"人工步骤「{step.get('name') or step.get('step_id') or ''}」投的 {name} 不在 BOM 里："
                f"人工步骤的用量只能按 BOM 预留，请把它加进 BOM"
            )
    return "；".join(rows)


def _bom_check(material_steps: list[dict[str, Any]], bom: list[dict]) -> tuple[bool, str]:
    """BOM 项。合法空 BOM 有两种：没有消耗物料的步骤；或每个消耗步骤都是设备步骤、写明了投哪种料——
    这时每批的量随样本变（配方表逐瓶给出），由实验方案的因子给出，建批次时按本批样本预留。
    人工步骤投的料不管 BOM 空不空，都必须列在 BOM 里（见 manual_materials_outside_bom）。
    """
    manual = manual_materials_outside_bom(material_steps, bom)
    if manual:
        return False, manual
    declared = list(dict.fromkeys(filter(None, (step_material(step) for step in material_steps))))
    if bom:
        detail = "、".join(f"{i.get('material')} {i.get('qty')}{i.get('unit')}" for i in bom)
        listed = {str(item.get("material") or "") for item in bom}
        outside = [name for name in declared if name not in listed]
        if outside:
            detail += f"；{'、'.join(outside)} 不在 BOM 里，用量由实验方案给出"
        return True, detail
    if not material_steps:
        return True, "无需物料：本流程没有消耗物料的步骤"
    if all(step_material(step) for step in material_steps):
        return True, f"{'、'.join(declared)} 的用量由实验方案按样本给出"
    return False, "存在消耗物料的步骤但未定义 BOM，排程前无法预留"


def next_state(current: str) -> str | None:
    return LIFECYCLE.get(current)
