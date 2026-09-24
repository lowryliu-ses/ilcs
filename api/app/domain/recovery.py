"""恢复评估：四项前置 + 能力规则决定可用策略。

策略可用性由能力的恢复规则决定，流程不能覆盖。不可逆投料步骤不提供重试。
"""
from dataclasses import dataclass, field

RESUME = "resume"
RETRY = "retry"
ABORT = "abort"


@dataclass
class RecoveryContext:
    capability_id: str
    capability_name: str
    recovery: dict
    step_name: str
    step_duration_min: float
    elapsed_min: float
    held_min: float
    unresolved_alarm_ids: list[str] = field(default_factory=list)
    downstream_steps: list[dict] = field(default_factory=list)
    next_station: dict | None = None
    lane_mate_batch_ids: list[str] = field(default_factory=list)
    unfinished_sample_count: int = 0


@dataclass(frozen=True)
class Option:
    id: str
    label: str
    allowed: bool
    reason: str
    impact: str

    def as_dict(self) -> dict:
        return {"id": self.id, "label": self.label, "allowed": self.allowed, "reason": self.reason, "impact": self.impact}


def preconditions(context: RecoveryContext) -> list[dict]:
    recovery = context.recovery or {}
    max_hold = float(recovery.get("maxHoldMin", 0) or 0)
    verify = recovery.get("verify") or []
    next_station = context.next_station
    return [
        {
            "key": "cause_cleared",
            "label": "触发原因已解除",
            "ok": not context.unresolved_alarm_ids,
            "detail": (
                f"{'、'.join(context.unresolved_alarm_ids)} 异常条件仍持续。确认报警不能解除该阻断，需设备侧条件恢复事件。"
                if context.unresolved_alarm_ids
                else "已收到设备条件恢复事件。"
            ),
        },
        {
            "key": "checkpoint",
            "label": "检查点已核对",
            "ok": True,
            "detail": (
                f"检查点保留实际交付量与累计执行时间（本步 {context.elapsed_min:.0f}/{context.step_duration_min:.0f} min）。"
                f"恢复前必须核实：{'、'.join(verify) or '无'}。"
            ),
        },
        {
            "key": "hold_window",
            "label": "保持时限",
            "ok": context.held_min <= max_hold,
            "detail": (
                f"已保持 {context.held_min:.0f} min，能力允许最长 {max_hold:.0f} min。"
                + ("已超时，只能终止或人工核查。" if context.held_min > max_hold else "")
            ),
        },
        {
            "key": "downstream",
            "label": "下游资源已确认",
            "ok": next_station is None or next_station.get("status") != "fault",
            "detail": (
                f"下一步工位 {next_station['id']} {'故障' if next_station.get('status') == 'fault' else '可用'}；"
                "保留本批次原工位占用，不重排化学步骤。"
                if next_station
                else "本步为最后一步。"
            ),
        },
    ]


def options(context: RecoveryContext, precondition_rows: list[dict]) -> list[Option]:
    recovery = context.recovery or {}
    ready = all(row["ok"] for row in precondition_rows)
    remaining = max(0.0, context.step_duration_min - context.elapsed_min)
    downstream = context.downstream_steps
    next_hard = next((s for s in downstream if (s.get("hard") or {}).get("maxGapMin") is not None), None)
    immediate_hard = (downstream[0].get("hard") if downstream else None) or {}

    hard_note = ""
    if immediate_hard.get("maxGapMin") is not None:
        limit = float(immediate_hard["maxGapMin"])
        hard_note = (
            f"下一步「{downstream[0]['name']}」硬时限 {limit:.0f} min 从本步结束起算，"
            f"{'仍可满足' if context.held_min <= limit else '已被触碰，需重排下游'}。"
        )

    lane_note = (
        f"，影响同工位已排程批次 {'、'.join(context.lane_mate_batch_ids)}" if context.lane_mate_batch_ids else ""
    )
    return [
        Option(
            RESUME, "从检查点续跑",
            bool(recovery.get("pausable")) and ready,
            recovery.get("sideEffect", "") if not recovery.get("pausable") else "前置条件未满足",
            f"补齐本步剩余 {remaining:.0f} min。下游 {len(downstream)} 步整体后移 {context.held_min:.0f} min。{hard_note}",
        ),
        Option(
            RETRY, "重试当前步骤",
            bool(recovery.get("retryable")) and ready,
            recovery.get("sideEffect", "") if not recovery.get("retryable") else "前置条件未满足",
            f"清空本步进度，重新执行 {context.step_duration_min:.0f} min。"
            f"下游后移 {context.step_duration_min + context.held_min:.0f} min{lane_note}。{recovery.get('sideEffect', '')}",
        ),
        Option(
            ABORT, "安全终止并清退", True, "",
            f"按流程终止，{context.unfinished_sample_count} 个未完成样品待隔离处置。"
            + (f"跳过下游硬时限步骤「{next_hard['name']}」。" if next_hard else ""),
        ),
    ]


def pick(option_rows: list[Option], requested: str) -> Option:
    for option in option_rows:
        if option.id == requested:
            return option
    raise KeyError(requested)
