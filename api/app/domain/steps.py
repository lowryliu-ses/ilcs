"""步骤的字段契约与校验。

device 有设备命令，manual 有待办与结构化记录表单，wait 是定时或业务事件，
review 要批准才继续。人工、等待、审核步骤不创建假适配器，也不占工位——
除非显式声明「等待期间样本仍留在设备中」。

控制流节点：
- branch（条件分支）按上游测量值、人工记录字段或人工选择走其中一条出边，没走的路径被剪掉；
  分支的某个出口可以声明「回到上游某一步」，就是有上限的循环。
- subflow（子流程）引用一个已发布的方法，建批次时展开进快照；执行器与排程器只看到展开后的步骤。

通用字段：`timeout`（步骤级超时：报警 / 判失败 / 跳过）、`skippable`（允许运行时跳过，
方法作者在设计时就要同意，运行时不能临时把关键步骤跳掉）。
"""
from __future__ import annotations

from typing import Any

DEVICE = "device"
MANUAL = "manual"
WAIT = "wait"
REVIEW = "review"
# 质检关卡：读上游设备步骤回执里的测量值按阈值自动判定，不合格按配置返工 / 报废 / 保持
GATE = "gate"
# 样本拆分：一个样本分出 N 个子样本（如一瓶电解液做 N 个扣电），建立谱系
SPLIT = "split"
# 条件分支：按上游测量值 / 人工记录字段 / 人工选择走一条出边；可带有上限的回环
BRANCH = "branch"
# 子流程：引用一个已发布方法，建批次时展开
SUBFLOW = "subflow"
# 消息通知：发一条 flow.notify 对外事件（Webhook 订阅方收到），立即继续
NOTIFY = "notify"
KINDS = (DEVICE, MANUAL, WAIT, REVIEW, GATE, SPLIT, BRANCH, SUBFLOW, NOTIFY)
KIND_NAMES = {
    DEVICE: "设备", MANUAL: "人工", WAIT: "等待", REVIEW: "审核", GATE: "质检关卡", SPLIT: "样本拆分",
    BRANCH: "条件分支", SUBFLOW: "子流程", NOTIFY: "消息通知",
}
GATE_ON_FAIL = {"rework": "返工", "scrap": "报废", "hold": "保持待人工判断"}
# 系统即时判定 / 登记的节点：没有预定时长，也不占工位
AUTOMATIC_KINDS = {REVIEW, GATE, SPLIT, BRANCH, SUBFLOW, NOTIFY}
BRANCH_MODES = {"measure": "按上游设备测量值", "form": "按上游人工记录字段", "manual": "人工选择"}
TIMEOUT_ACTIONS = {"alarm": "只报警", "fail": "判为失败，进入恢复评估", "skip": "自动跳过"}
# 设备步骤的超时已由指令超时守着（超过硬上限转结果未知、人工核查）：步骤级只允许加报警，
# 不能让计时器替人判定一个物理动作「失败了」或「可以跳过」
TIMEOUT_ACTIONS_BY_KIND = {
    DEVICE: {"alarm"}, MANUAL: set(TIMEOUT_ACTIONS), WAIT: set(TIMEOUT_ACTIONS),
    REVIEW: set(TIMEOUT_ACTIONS), BRANCH: {"alarm", "fail"},
}
SKIPPABLE_KINDS = {DEVICE, MANUAL, WAIT, REVIEW}
MAX_LOOPS = 10

# 每类步骤适用哪些字段。不适用的字段即时校验时不提示缺失，服务端也不据此阻塞。
APPLICABLE: dict[str, set[str]] = {
    DEVICE: {"cap", "params", "dur", "hard", "resource", "timeout", "skippable"},
    MANUAL: {"dur", "form", "resource", "requires_signature", "qualification", "hard", "timeout", "skippable"},
    WAIT: {"dur", "wait_for", "hard", "timeout", "skippable"},
    REVIEW: {"review_role", "dur", "timeout", "skippable"},
    GATE: {"gate"},
    SPLIT: {"split"},
    BRANCH: {"branch", "timeout", "requires_signature"},
    SUBFLOW: {"subflow"},
    NOTIFY: {"notify"},
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
    if kind_of(step) in {WAIT, REVIEW, GATE, SPLIT, BRANCH, SUBFLOW, NOTIFY}:
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
        "branch": len([s for s in rows if kind_of(s) == BRANCH]),
        "subflow": len([s for s in rows if kind_of(s) == SUBFLOW]),
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
        # 业务事件由 `POST /batches/{id}/signals` 发出（人或授权的服务身份）；早到的事件先登记，
        # 节点开出时直接消费。计划时长只用于排程，真正的结束由事件决定
        name = str(wait_for.get("event") or "").strip()
        if not name:
            issues.append("业务事件等待必须写明事件名（如 sample_received、qc_released）")
        elif not all(ch.isalnum() or ch in "_-.:" for ch in name) or len(name) > 64:
            issues.append("事件名只能包含字母、数字与 _ - . :，最长 64 个字符")
        minutes = step.get("dur")
        if not isinstance(minutes, (int, float)) or isinstance(minutes, bool) or minutes <= 0:
            issues.append("业务事件等待也要填计划时长（排程按它预留时间）")
    else:
        issues.append("等待方式未选择：固定时长或业务事件")
    return issues


def timeout_issues(step: dict[str, Any]) -> list[str]:
    """步骤级超时。只校验声明了的；设备步骤只允许报警。"""
    timeout = (step or {}).get("timeout")
    if not timeout:
        return []
    if not isinstance(timeout, dict):
        return ["超时配置格式不正确"]
    kind = kind_of(step)
    allowed = TIMEOUT_ACTIONS_BY_KIND.get(kind)
    if allowed is None:
        return [f"{KIND_NAMES.get(kind, kind)}节点由系统即时处理，不支持超时配置"]
    issues: list[str] = []
    minutes = timeout.get("minutes")
    if not isinstance(minutes, (int, float)) or isinstance(minutes, bool) or minutes <= 0:
        issues.append("超时时长必须大于 0 分钟")
    action = timeout.get("action") or "alarm"
    if action not in TIMEOUT_ACTIONS:
        issues.append("超时处理只能是报警、判为失败或自动跳过")
    elif action not in allowed:
        if kind == DEVICE:
            issues.append("设备步骤超时只能报警：物理动作是否完成由设备回执或现场核查决定")
        else:
            issues.append(f"{KIND_NAMES.get(kind, kind)}节点超时不能「{TIMEOUT_ACTIONS[action]}」")
    if action == "skip" and not step.get("skippable"):
        issues.append("超时自动跳过要求本步骤允许跳过（skippable）")
    if kind == WAIT and ((step.get("wait_for") or {}).get("mode") or "duration") == "duration":
        issues.append("固定时长等待到点即结束，不需要超时；业务事件等待才需要")
    return issues


def skippable_issues(step: dict[str, Any]) -> list[str]:
    if not (step or {}).get("skippable"):
        return []
    kind = kind_of(step)
    if kind not in SKIPPABLE_KINDS:
        return [f"{KIND_NAMES.get(kind, kind)}节点不能设为可跳过"]
    return []


def branch_config(step: dict[str, Any]) -> dict[str, Any]:
    return (step or {}).get("branch") or {}


def branch_cases(step: dict[str, Any]) -> list[dict[str, Any]]:
    cases = branch_config(step).get("cases") or []
    return [case for case in cases if isinstance(case, dict)]


def forward_case_keys(step: dict[str, Any]) -> list[str]:
    """分支里「往前走」的出口：不回环的那些。只有它们能出现在后继的 when 上。"""
    return [str(case.get("key")) for case in branch_cases(step) if case.get("key") and not case.get("loop_to")]


def loop_cases(step: dict[str, Any]) -> list[dict[str, Any]]:
    return [case for case in branch_cases(step) if case.get("loop_to")]


def branch_issues(step: dict[str, Any], steps: list[dict[str, Any]], index: int) -> list[str]:
    """条件分支的字段完整性。依赖图上的约束（来源是上游、回环体封闭）由 graph.branch_graph_issues 判。"""
    config = branch_config(step)
    issues: list[str] = []
    mode = config.get("mode") or ""
    if mode not in BRANCH_MODES:
        issues.append("分支依据只能是上游测量值、上游人工记录字段或人工选择")
    ids = [step_id_of(row, position) for position, row in enumerate(steps)]
    if mode in {"measure", "form"}:
        source = str(config.get("source_step_id") or "")
        if source not in ids[:index]:
            issues.append("分支必须指定它之前的一个步骤作为判据来源")
        else:
            source_kind = kind_of(steps[ids.index(source)])
            if mode == "measure" and source_kind != DEVICE:
                issues.append("按测量值分支时来源必须是设备步骤：只有设备回执里有测量值")
            if mode == "form" and source_kind != MANUAL:
                issues.append("按记录字段分支时来源必须是人工步骤")
            if mode == "form":
                keys = {str(f.get("key")) for f in (steps[ids.index(source)].get("form") or []) if isinstance(f, dict)}
                if str(config.get("field") or "") not in keys:
                    issues.append("判据字段不在来源人工步骤的记录表单里")
        if not str(config.get("field") or "").strip():
            issues.append("必须指定判据字段")
    cases = branch_cases(step)
    if len(cases) < 2:
        issues.append("条件分支至少要有两个出口")
    seen: set[str] = set()
    for position, case in enumerate(cases):
        key = str(case.get("key") or "").strip()
        label = f"出口 {key or position + 1}"
        if not key:
            issues.append(f"第 {position + 1} 个出口缺少标识")
        elif key in seen:
            issues.append(f"出口标识 {key} 重复")
        seen.add(key)
        if not str(case.get("label") or "").strip():
            issues.append(f"{label} 缺少显示名称")
        if mode in {"measure", "form"}:
            low, high, equals = case.get("min"), case.get("max"), case.get("equals")
            numeric = [b for b in (low, high) if isinstance(b, (int, float)) and not isinstance(b, bool)]
            has_equals = equals not in (None, "")
            if not numeric and not has_equals and key != str(config.get("default") or ""):
                issues.append(f"{label} 没有判定条件（下限 / 上限 / 等于）；兜底出口请设为默认")
            if len(numeric) == 2 and low > high:
                issues.append(f"{label} 下限不能大于上限")
        target = str(case.get("loop_to") or "")
        if target and target not in ids[:index]:
            issues.append(f"{label} 回环目标必须是分支之前的步骤")
    default = str(config.get("default") or "")
    if default and default not in seen:
        issues.append(f"默认出口 {default} 不存在")
    if default and any(str(c.get("key")) == default and c.get("loop_to") for c in cases):
        issues.append("默认出口不能是回环：判据缺失时不应自动重做上游步骤")
    if loop_cases(step):
        rounds = config.get("max_loops")
        if not isinstance(rounds, int) or isinstance(rounds, bool) or not 1 <= rounds <= MAX_LOOPS:
            issues.append(f"有回环出口时必须设置最多循环次数（1–{MAX_LOOPS}）；超过后转人工选择")
        if not forward_case_keys(step):
            issues.append("至少要有一个不回环的出口，否则流程永远出不了循环")
    return issues


def notify_issues(step: dict[str, Any]) -> list[str]:
    config = (step or {}).get("notify") or {}
    message = str(config.get("message") or "").strip()
    if not message:
        return ["消息通知节点必须写明通知内容"]
    if len(message) > 500:
        return ["通知内容最长 500 个字符"]
    return []


def subflow_issues(step: dict[str, Any]) -> list[str]:
    config = (step or {}).get("subflow") or {}
    if not str(config.get("recipe_id") or "").strip():
        return ["子流程必须选择引用的方法"]
    return []


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


def gate_issues(step: dict[str, Any], steps: list[dict[str, Any]], index: int) -> list[str]:
    """质检关卡的配置。测量来源必须是它之前的设备步骤，返工目标不能晚于测量来源。"""
    gate = (step or {}).get("gate") or {}
    issues: list[str] = []
    ids = [step_id_of(row, position) for position, row in enumerate(steps)]
    source = str(gate.get("source_step_id") or "")
    if source not in ids[:index]:
        issues.append("质检关卡必须指定它之前的一个设备步骤作为测量来源")
    elif kind_of(steps[ids.index(source)]) != DEVICE:
        issues.append("测量来源必须是设备步骤：只有设备回执里有测量值")
    if not str(gate.get("field") or "").strip():
        issues.append("质检关卡必须指定测量字段（设备回执 delivered 里的键）")
    bounds = [gate.get("min"), gate.get("max")]
    numeric = [b for b in bounds if isinstance(b, (int, float)) and not isinstance(b, bool)]
    if not numeric:
        issues.append("质检关卡至少要有下限或上限")
    elif len(numeric) == 2 and gate["min"] > gate["max"]:
        issues.append("质检关卡下限不能大于上限")
    if gate.get("scope", "batch") not in {"batch", "sample"}:
        issues.append("判定范围只能是整批（batch）或逐孔位（sample）")
    on_fail = gate.get("on_fail")
    if on_fail not in GATE_ON_FAIL:
        issues.append("不合格去向必须是返工、报废或保持待人工判断")
    if on_fail == "rework":
        target = str(gate.get("rework_to") or "")
        if target not in ids[:index]:
            issues.append("返工必须回到关卡之前的某一步")
        elif source in ids and ids.index(target) > ids.index(source):
            issues.append("返工目标不能晚于测量来源：否则返工不会重新测量")
        rounds = gate.get("max_rework")
        if not isinstance(rounds, int) or isinstance(rounds, bool) or not 1 <= rounds <= 5:
            issues.append("最多返工次数必须是 1–5 的整数；超过后转人工判断")
    return issues


def split_issues(step: dict[str, Any]) -> list[str]:
    split = (step or {}).get("split") or {}
    count = split.get("count")
    issues: list[str] = []
    if not isinstance(count, int) or isinstance(count, bool) or not 2 <= count <= 96:
        issues.append("拆分份数必须是 2–96 的整数")
    if not str(split.get("child_type") or "").strip():
        issues.append("必须写明子样本类型（如 扣电、极片）")
    return issues


def match_case(step: dict[str, Any], value: Any) -> str | None:
    """按出口顺序取第一个满足条件的出口。值缺失返回 None：取不到判据不等于走默认以外的任何路。"""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    for case in branch_cases(step):
        key = str(case.get("key") or "")
        equals = case.get("equals")
        if equals not in (None, ""):
            if str(value).strip() == str(equals).strip():
                return key
            continue
        low, high = case.get("min"), case.get("max")
        bounded = any(isinstance(b, (int, float)) and not isinstance(b, bool) for b in (low, high))
        if not bounded:
            continue
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        if isinstance(low, (int, float)) and not isinstance(low, bool) and value < low:
            continue
        if isinstance(high, (int, float)) and not isinstance(high, bool) and value > high:
            continue
        return key
    default = str(branch_config(step).get("default") or "")
    return default or None


def case_label(step: dict[str, Any], key: str) -> str:
    for case in branch_cases(step):
        if str(case.get("key")) == key:
            return str(case.get("label") or key)
    return key


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
