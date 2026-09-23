"""步骤运行的状态机与推进判据。

设备成功回执只完成对应步骤，下一步由推进器决定——这条分离是为了让「设备动了」和
「流程往前了」不再是同一件事：设备回执可以重放，流程推进不能。
"""
from __future__ import annotations

from dataclasses import dataclass

from .steps import DEVICE, GATE, MANUAL, REVIEW, SPLIT, WAIT, kind_of

PENDING = "pending"
READY = "ready"
RUNNING = "running"
WAITING = "waiting"
COMPLETED = "completed"
FAILED = "failed"
UNKNOWN = "unknown"
CANCELLED = "cancelled"
# 被质检返工作废的一次执行：物理上做过，但结论不再算数，流程会重新做这一步
SUPERSEDED = "superseded"

STATES = (PENDING, READY, RUNNING, WAITING, COMPLETED, FAILED, UNKNOWN, CANCELLED, SUPERSEDED)
OPEN_STATES = {PENDING, READY, RUNNING, WAITING}
TERMINAL_STATES = {COMPLETED, FAILED, CANCELLED, SUPERSEDED}

STATE_LABEL = {
    PENDING: "待开始", READY: "待办", RUNNING: "执行中", WAITING: "等待中",
    COMPLETED: "已完成", FAILED: "失败", UNKNOWN: "结果未知", CANCELLED: "已取消",
    SUPERSEDED: "已被返工取代",
}

# 每类步骤允许的转换。任意 PATCH 目标状态的入口不存在，只能通过对应事件。
ALLOWED: dict[str, dict[str, set[str]]] = {
    DEVICE: {
        PENDING: {READY, CANCELLED},
        READY: {RUNNING, CANCELLED},
        RUNNING: {COMPLETED, FAILED, UNKNOWN},
        UNKNOWN: {COMPLETED, FAILED, CANCELLED},
    },
    MANUAL: {
        PENDING: {READY, CANCELLED},
        READY: {RUNNING, COMPLETED, CANCELLED},
        RUNNING: {COMPLETED, FAILED, CANCELLED},
    },
    WAIT: {
        PENDING: {WAITING, CANCELLED},
        WAITING: {COMPLETED, CANCELLED},
    },
    REVIEW: {
        PENDING: {READY, CANCELLED},
        READY: {COMPLETED, FAILED, CANCELLED},
    },
    GATE: {
        PENDING: {READY, CANCELLED},
        READY: {COMPLETED, FAILED, CANCELLED},
    },
    SPLIT: {
        PENDING: {READY, CANCELLED},
        READY: {COMPLETED, CANCELLED},
    },
}

INITIAL = {DEVICE: READY, MANUAL: READY, WAIT: WAITING, REVIEW: READY, GATE: READY, SPLIT: READY}


def judge(value, minimum=None, maximum=None) -> bool | None:
    """阈值判定。取不到数值返回 None：无法判定不等于合格。"""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    if isinstance(minimum, (int, float)) and value < minimum:
        return False
    if isinstance(maximum, (int, float)) and value > maximum:
        return False
    return True


def initial_state(step: dict) -> str:
    return INITIAL[kind_of(step)]


def can_transition(step_kind: str, current: str, target: str) -> bool:
    return target in ALLOWED.get(step_kind, {}).get(current, set())


@dataclass(frozen=True)
class Advance:
    """一次推进的结论。"""

    completed_step_id: str
    next_step_index: int | None
    next_step_id: str | None
    batch_done: bool
    reason: str = ""


def next_step(steps: list[dict], completed_index: int) -> tuple[int | None, str | None]:
    from .steps import step_id_of

    following = completed_index + 1
    if following >= len(steps or []):
        return None, None
    return following, step_id_of(steps[following], following)


def review_rejection_is_new_attempt(steps: list[dict], review_index: int) -> tuple[bool, str]:
    """审核退回落在哪里。

    退回形成上一个人工步骤的新尝试；如果上游是设备步骤，就不能自动回退重跑——
    物理动作已经发生过，重做要走恢复评估或新建运行。
    """
    from .steps import step_id_of

    for index in range(review_index - 1, -1, -1):
        kind = kind_of(steps[index])
        if kind == MANUAL:
            return True, step_id_of(steps[index], index)
        if kind == DEVICE:
            return False, step_id_of(steps[index], index)
    return False, ""


def hold_blocks_device_action(batch_state: str) -> bool:
    """保持中可以记录定时到期事件，但恢复前不得推进设备动作。"""
    return batch_state in {"paused", "fault", "aborting", "aborted"}
