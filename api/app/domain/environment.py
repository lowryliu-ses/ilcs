"""环境条件：步骤对环境的要求与读数核对。

步骤可以声明 `environment: [{metric, min, max, zone}]`（如手套箱水含量 ≤ 0.1 ppm、干燥间露点 ≤ −40 ℃）。
`zone` 不写时取这一步分到的工位；不占工位的人工步骤必须写明区域。
核对取该区域该指标的最新读数：没有读数、读数过期（超过 max_age_min）、超出范围都不放行——
「没测」不等于「合格」。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

METRICS = {
    "temperature": ("温度", "℃"), "humidity": ("相对湿度", "%RH"), "dew_point": ("露点", "℃"),
    "h2o_ppm": ("水含量", "ppm"), "o2_ppm": ("氧含量", "ppm"), "pressure_diff": ("压差", "Pa"),
    "particles": ("洁净度", "个/m³"),
}


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def requirements(step: dict) -> list[dict]:
    rows = step.get("environment") if isinstance(step.get("environment"), list) else []
    return [row for row in rows if isinstance(row, dict) and row.get("metric")]


def requirement_issues(step: dict, needs_zone: bool) -> list[str]:
    issues = []
    raw = step.get("environment")
    if raw is None:
        return issues
    if not isinstance(raw, list):
        return ["环境要求格式不对"]
    for index, row in enumerate(raw, start=1):
        if not isinstance(row, dict) or not str(row.get("metric") or "").strip():
            issues.append(f"环境要求第 {index} 项没有指标")
            continue
        low, high = _num(row.get("min")), _num(row.get("max"))
        if low is None and high is None:
            issues.append(f"环境要求 {row['metric']} 没有上下限")
        if low is not None and high is not None and low > high:
            issues.append(f"环境要求 {row['metric']} 下限大于上限")
        if needs_zone and not str(row.get("zone") or "").strip():
            issues.append(f"环境要求 {row['metric']}：不占工位的步骤要写明区域")
    return issues


@dataclass(frozen=True)
class Reading:
    value: float
    measured_at: datetime
    unit: str = ""


def check(requirement: dict, zone: str, reading: Reading | None, now: datetime, max_age_min: float) -> str | None:
    """返回不放行的原因；满足返回 None。"""
    metric = requirement["metric"]
    label = METRICS.get(metric, (metric, ""))[0]
    if not zone:
        return f"{label}：不知道在哪个区域测（步骤没有分到工位，也没写区域）"
    if reading is None:
        return f"{zone} 没有{label}读数"
    age = (now - reading.measured_at).total_seconds() / 60
    if age > max_age_min:
        return f"{zone} {label}读数已 {age:.0f} min 未更新（超过 {max_age_min:g} min）"
    low, high = _num(requirement.get("min")), _num(requirement.get("max"))
    if low is not None and reading.value < low:
        return f"{zone} {label} {reading.value:g}{reading.unit} 低于要求 {low:g}"
    if high is not None and reading.value > high:
        return f"{zone} {label} {reading.value:g}{reading.unit} 高于要求 {high:g}"
    return None


def overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    return a_start < b_end and b_start < a_end


def step_windows(steps: list[dict], starts: dict[int, datetime], ends: dict[int, datetime], begin: datetime,
                 predecessors: list[list[int]]) -> dict[int, tuple[datetime, datetime]]:
    """每一步的计划时间窗：占工位的步骤用预约时间窗，其余步骤接在最晚结束的前驱之后、按时长推。"""
    windows: dict[int, tuple[datetime, datetime]] = {}
    for index, step in enumerate(steps):
        if index in starts and index in ends:
            windows[index] = (starts[index], ends[index])
            continue
        parents = [windows[parent][1] for parent in predecessors[index] if parent in windows]
        start = max(parents, default=begin)
        windows[index] = (start, start + timedelta(minutes=float(step.get("dur") or 0)))
    return windows
