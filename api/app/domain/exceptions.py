"""异常分类与策略库的纯规则。

异常不是「发个通知」：每一条都要回答是什么类别、影响了谁、系统自动做了什么、人做了什么、
最后怎么收尾。这里只管前两件的判据与「该用哪条策略」。

**自动处理的安全边界**（与执行器「结果未知不盲目重试」一脉相承）：只有能证明设备**从未收到**
这条指令时（指令没离开系统就被拒：失联、心跳超时、适配器停用、不接受动作、没有适配器），
才允许自动重试或改派到等价工位。设备可能已经动过的（回执不明、超时、部分执行）一律转人工——
策略库改不了这条。安全联锁同样只转人工：联锁是现场事实，不该被自动绕开。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

CATEGORIES = {
    "device_fault": "设备故障",
    "communication": "通信异常",
    "sample": "样本异常",
    "reagent": "试剂异常",
    "timeout": "超时",
    "data": "数据异常",
    "robot": "机器人 / 转运异常",
    "path_conflict": "位置与路径冲突",
    "manual": "人工操作异常",
    "safety": "安全异常",
    "schedule": "排程冲突",
    "system": "系统异常",
}

ACTIONS = {
    "retry": "延时后重新下发（同一工位）",
    "reroute": "改派到具备同样能力的工位",
    "skip": "跳过该步骤（流程须标为可跳过）",
    "reschedule": "生成重排建议",
    "hold": "保持，转人工处理",
}
# 这些动作会重新驱动设备：只有指令从未送达设备时才允许
DRIVING_ACTIONS = {"retry", "reroute", "skip"}
# 这些类别从不自动处理：安全联锁是现场事实，结果未知的动作只能由人下结论
MANUAL_ONLY = {"safety"}

STATES = {
    "open": "待处理",
    "auto_resolved": "已自动处理",
    "manual": "人工处理中",
    "resolved": "已恢复",
    "closed": "已关闭",
}


def classify(reason: str, *, command_type: str = "", delivery: str = "", source: str = "") -> str:
    """按故障原因、指令类型与投递状态归类。文案来自执行器与监控，是本系统自己写的，可以依赖。"""
    text = reason or ""
    if "联锁" in text:
        return "safety"
    if command_type == "transfer" or "转运" in text or "载具" in text:
        return "path_conflict" if ("位置" in text or "放置位" in text or "扫码" in text) else "robot"
    if "超过最长" in text or source == "step_timeout":
        return "timeout"
    if "失联" in text or "心跳" in text or "无响应" in text or "回执无法" in text:
        return "communication"
    if "超时" in text:
        return "timeout"
    if "硬时限" in text or "时间窗" in text or source == "schedule":
        return "schedule"
    if "消耗" in text or "物料" in text or "批号" in text or "预留" in text:
        return "reagent"
    if source in {"gate", "sample"} or "质检" in text or "样本" in text:
        return "sample"
    if source == "branch" or "判据" in text:
        return "data"
    if delivery in {"unreachable", "maybe_sent"}:
        return "communication"
    if "适配器" in text or "设备" in text or "校准" in text:
        return "device_fault"
    return "system"


def classify_condition(condition_key: str, message: str = "") -> str:
    """报警的去重键 → 类别。键是系统自己生成的（station:ST-05:heartbeat_stale 这种）。"""
    key = condition_key or ""
    if key.endswith(":interlock"):
        return "safety"
    if key.endswith(":disconnected") or key.endswith(":heartbeat_stale"):
        return "communication"
    if ":calibration" in key:
        return "device_fault"
    if key.endswith(":overdue") or key.endswith(":timeout"):
        return "timeout"
    if key.endswith(":schedule_conflict") or key.endswith(":dependency_conflict"):
        return "schedule"
    if key.startswith("gate:"):
        return "sample"
    if key.startswith("branch:") or key.startswith("data:"):
        return "data"
    if key.startswith("command:"):
        return classify(message)
    if key.startswith("step:") and key.endswith(":stuck"):
        return "system"
    return classify(message) if message else ""


@dataclass(frozen=True)
class Signal:
    """一次异常的上下文：规则按它匹配。"""

    category: str
    batch_id: str = ""
    recipe_id: str = ""
    step_id: str = ""
    step_kind: str = ""
    capability: str = ""
    station_id: str = ""
    # 指令从未离开系统（设备没见过它）：只有这时才允许会重新驱动设备的动作
    never_sent: bool = False
    skippable: bool = False
    attempts: int = 0


@dataclass(frozen=True)
class Rule:
    id: str
    name: str
    category: str
    action: str
    match: dict[str, Any] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)
    priority: int = 100
    enabled: bool = True


@dataclass(frozen=True)
class Decision:
    action: str
    rule: Rule | None
    reason: str

    @property
    def automatic(self) -> bool:
        return self.action in DRIVING_ACTIONS or self.action == "reschedule"


def rule_issues(rule: dict) -> list[str]:
    issues: list[str] = []
    if not str(rule.get("name") or "").strip():
        issues.append("策略必须有名称")
    if rule.get("category") not in CATEGORIES:
        issues.append("异常类别不在可选范围内")
    action = rule.get("action")
    if action not in ACTIONS:
        issues.append("处理动作只能是重试、改派、跳过、重排建议或转人工")
    if rule.get("category") in MANUAL_ONLY and action != "hold":
        issues.append(f"{CATEGORIES.get(rule.get('category'), '')}只能转人工处理，不能配置自动动作")
    params = rule.get("params") or {}
    if action == "retry":
        attempts = params.get("max_attempts")
        if not isinstance(attempts, int) or isinstance(attempts, bool) or not 1 <= attempts <= 10:
            issues.append("重试次数必须是 1–10 的整数")
        delay = params.get("delay_sec", 60)
        if not isinstance(delay, (int, float)) or isinstance(delay, bool) or not 0 <= delay <= 3600:
            issues.append("重试间隔必须在 0–3600 秒之间")
    if action == "reroute":
        attempts = params.get("max_attempts", 1)
        if not isinstance(attempts, int) or isinstance(attempts, bool) or not 1 <= attempts <= 5:
            issues.append("改派次数必须是 1–5 的整数")
    match = rule.get("match") or {}
    unknown = set(match) - {"capability", "station_id", "step_kind", "recipe_id", "step_id"}
    if unknown:
        issues.append(f"未知的匹配字段：{'、'.join(sorted(unknown))}")
    return issues


def _matches(rule: Rule, signal: Signal) -> bool:
    if not rule.enabled or rule.category != signal.category:
        return False
    for key, wanted in (rule.match or {}).items():
        if wanted in (None, "", []):
            continue
        actual = getattr(signal, key, "")
        if isinstance(wanted, list):
            if actual not in wanted:
                return False
        elif actual != wanted:
            return False
    return True


def decide(signal: Signal, rules: list[Rule]) -> Decision:
    """按优先级取第一条匹配的策略，再过安全边界；过不了就转人工，并说明为什么。"""
    if signal.category in MANUAL_ONLY:
        return Decision("hold", None, f"{CATEGORIES[signal.category]}只能由人处理")
    rule = next(
        (row for row in sorted(rules, key=lambda r: (r.priority, r.id)) if _matches(row, signal)), None,
    )
    if rule is None:
        return Decision("hold", None, "没有匹配的处理策略，转人工")
    if rule.action in DRIVING_ACTIONS and not signal.never_sent:
        return Decision("hold", rule, f"策略「{rule.name}」要求重新驱动设备，但设备可能已收到指令：结果未知只能由人核查")
    if rule.action == "skip" and not signal.skippable:
        return Decision("hold", rule, f"策略「{rule.name}」要跳过步骤，但流程没有把这一步标为可跳过")
    limit = int((rule.params or {}).get("max_attempts", 1 if rule.action == "reroute" else 0) or 0)
    if rule.action in {"retry", "reroute"} and signal.attempts >= limit:
        return Decision("hold", rule, f"策略「{rule.name}」已自动处理 {signal.attempts} 次、达到上限 {limit}，转人工")
    return Decision(rule.action, rule, f"按策略「{rule.name}」：{ACTIONS[rule.action]}")
