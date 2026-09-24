"""运行指标的统一口径。纯函数：输入区间与记录，输出数字。

- **设备利用率** = 统计窗口内设备实际在执行指令的时长 ÷（窗口时长 × 并行通道数）。「在执行」按指令
  被设备接受到给出结论的区间算，不按排程时间窗算——排程是计划，利用率要的是实际。计划负荷另算。
- **自动化成功率** = 窗口内完成的批次里，全程没有人工介入的比例。人工介入：人请求保持、恢复评估、
  结果未知指令的现场核查、人工跳过、从指定节点重做，或留下了需要人处理的异常（自动处理成功的不算）。
- **平均恢复时长（MTTR）** = 窗口内已恢复的异常，从登记到恢复的平均分钟数。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

# 这些审计动作说明「人插手了」
# 人工选择模式的分支出口是方法设计好的人工节点，不算介入；判据缺失而保持的分支会留下异常事件，按异常算
INTERVENTION_ACTIONS = ("请求保持", "指令人工核查", "跳过步骤", "从指定节点重做", "质检关卡人工判定")
INTERVENTION_PREFIXES = ("恢复：",)


@dataclass(frozen=True)
class Span:
    start: datetime
    end: datetime


def clipped_minutes(spans: list[Span], window_start: datetime, window_end: datetime) -> float:
    """区间落在窗口里的分钟数之和。"""
    total = 0.0
    for span in spans:
        start = max(span.start, window_start)
        end = min(span.end, window_end)
        if end > start:
            total += (end - start).total_seconds() / 60
    return total


def utilization(spans: list[Span], window_start: datetime, window_end: datetime, channels: int = 1) -> float:
    capacity = (window_end - window_start).total_seconds() / 60 * max(1, channels)
    if capacity <= 0:
        return 0.0
    return min(1.0, clipped_minutes(spans, window_start, window_end) / capacity)


def is_intervention(action: str) -> bool:
    return action in INTERVENTION_ACTIONS or any(action.startswith(prefix) for prefix in INTERVENTION_PREFIXES)


def ratio(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


def mean_minutes(pairs: list[tuple[datetime, datetime]]) -> float | None:
    values = [(end - start).total_seconds() / 60 for start, end in pairs if end and start and end >= start]
    return round(sum(values) / len(values), 1) if values else None
