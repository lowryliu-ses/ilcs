"""曲线（序列）型检测值：充放电曲线、循环曲线、谱图、粒径分布这类「一组 x–y 点」的结果。纯函数。

写法（回传、人工录入、设备回报都认）：
- `{"x": [...], "y": [...]}`：一条曲线；
- `{"traces": [{"name": "第 1 圈", "x": [...], "y": [...]}, ...]}`：几条（按圈、按充 / 放电分开）；
- `[[x, y], ...]`：点对列表，等同一条曲线。

入库统一成 `{"traces": [{"name", "x", "y"}]}`。值不成立（不是数、长短不一、点太少或太多）整次拒收，与数值指标
「不是数值」同一口径；曲线不进数值统计、逻辑规则与闭环训练数据——要统计就由设备或解析器另报数值指标
（如放电比容量），或在指标上声明 `derived` 从曲线派生（见 `derive`）。

列表里只给缩略（`preview`，按 LTTB 抽到几百点，形状不变），完整的点按需另取，不让结果列表变成几兆的响应。
"""
from __future__ import annotations

import math
from typing import Any

MAX_POINTS = 20000  # 每条曲线缺省上限；指标规则 max_points 可以放宽到 HARD_MAX_POINTS
HARD_MAX_POINTS = 100000
MAX_TRACES = 50
MAX_TOTAL_POINTS = 200000
MIN_POINTS = 2
PREVIEW_POINTS = 160
NAME_LIMIT = 40

# 从曲线派生数值：在指标规则 `derived` 里声明 [{"metric": 数值指标代码, "of": 取法}]，见 `derive`
REDUCERS = {
    "last_y": "最后一点的 y",
    "first_y": "第一点的 y",
    "max_y": "y 的最大值",
    "min_y": "y 的最小值",
    "last_x": "最后一点的 x（如截止时的容量）",
    "max_x": "x 的最大值",
    "area": "曲线下面积（梯形法，按 x 顺序）",
}


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _trace(raw: Any, index: int) -> tuple[dict | None, list[str]]:
    label = f"第 {index + 1} 条曲线"
    if not isinstance(raw, dict):
        return None, [f"{label}要写成 {{\"x\": [...], \"y\": [...]}}"]
    x, y = raw.get("x"), raw.get("y")
    if not isinstance(x, list) or not isinstance(y, list):
        return None, [f"{label}的 x、y 都要是数值列表"]
    name = raw.get("name")
    problems = []
    if name is not None and (not isinstance(name, (str, int, float)) or len(str(name)) > NAME_LIMIT):
        problems.append(f"{label}的名称要是不超过 {NAME_LIMIT} 个字的文字")
    if len(x) != len(y):
        problems.append(f"{label}的 x 有 {len(x)} 个点、y 有 {len(y)} 个，长短不一")
    else:
        bad = [value for value in [*x, *y] if not _finite(value)]
        if bad:
            problems.append(f"{label}有 {len(bad)} 个不是有限数值的点（如 {bad[0]!r}）：缺测的点不要写 null / NaN，直接去掉")
    if problems:
        return None, problems
    return {"name": "" if name is None else str(name), "x": [float(v) for v in x], "y": [float(v) for v in y]}, []


def normalize(value: Any) -> tuple[dict | None, list[str]]:
    """三种写法统一成 `{"traces": [...]}`；不成立返回 (None, 问题)。不查点数上下限（那是 `issues` 的事）。"""
    if isinstance(value, list):
        if not all(isinstance(row, (list, tuple)) and len(row) == 2 for row in value):
            return None, ["点对列表要写成 [[x, y], ...]"]
        value = {"x": [row[0] for row in value], "y": [row[1] for row in value]}
    if not isinstance(value, dict):
        return None, ["曲线要写成 {\"x\": [...], \"y\": [...]}、{\"traces\": [...]} 或 [[x, y], ...]"]
    raw_traces = value.get("traces") if "traces" in value else [value]
    if not isinstance(raw_traces, list) or not raw_traces:
        return None, ["traces 要是至少一条曲线的列表"]
    traces, problems = [], []
    for index, raw in enumerate(raw_traces):
        trace, found = _trace(raw, index)
        problems.extend(found)
        if trace is not None:
            traces.append(trace)
    if problems:
        return None, problems
    return {"traces": traces}, []


def issues(value: Any, rules: dict | None = None) -> list[str]:
    """回传值能不能当曲线入库：写法、点数、曲线条数。"""
    series, problems = normalize(value)
    if problems:
        return problems
    rules = rules or {}
    limit = int(rules.get("max_points") or MAX_POINTS)
    traces = series["traces"]
    if len(traces) > MAX_TRACES:
        problems.append(f"一个结果最多 {MAX_TRACES} 条曲线，这里有 {len(traces)} 条")
    for index, trace in enumerate(traces):
        points = len(trace["x"])
        label = trace["name"] or f"第 {index + 1} 条曲线"
        if points < MIN_POINTS:
            problems.append(f"{label}只有 {points} 个点，曲线至少要 {MIN_POINTS} 个点")
        elif points > limit:
            problems.append(f"{label}有 {points} 个点，超过指标允许的 {limit} 点：先在解析器里抽稀，原件作为原始文件上传")
    total = sum(len(trace["x"]) for trace in traces)
    if total > MAX_TOTAL_POINTS:
        problems.append(f"一个结果最多 {MAX_TOTAL_POINTS} 个点，这里有 {total} 个")
    return problems


def rule_issues(rules: dict) -> list[str]:
    """曲线指标的规则：x 轴名称与单位、点数上限、从曲线派生的数值指标。"""
    problems = []
    for key in ("x_label", "x_unit"):
        if rules.get(key) is not None and not isinstance(rules.get(key), str):
            problems.append(f"规则 {key} 要是文字")
    limit = rules.get("max_points")
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or not MIN_POINTS <= limit <= HARD_MAX_POINTS):
        problems.append(f"规则 max_points 要是 {MIN_POINTS}–{HARD_MAX_POINTS} 的整数")
    derived = rules.get("derived")
    if derived is not None:
        if not isinstance(derived, list):
            problems.append("规则 derived 要是 [{\"metric\": 数值指标代码, \"of\": 取法}] 列表")
        else:
            seen = set()
            for index, row in enumerate(derived, start=1):
                if not isinstance(row, dict) or not str(row.get("metric") or "").strip():
                    problems.append(f"派生第 {index} 项要写数值指标代码 metric")
                    continue
                if row.get("of") not in REDUCERS:
                    problems.append(f"派生第 {index} 项的取法 of 只能是 {'、'.join(REDUCERS)}")
                if row["metric"] in seen:
                    problems.append(f"派生的指标 {row['metric']} 重复")
                seen.add(row["metric"])
    return problems


def _range(values: list[float]) -> list[float] | None:
    return [min(values), max(values)] if values else None


def summary(series: dict | None) -> dict:
    traces = (series or {}).get("traces") or []
    xs = [value for trace in traces for value in trace["x"]]
    ys = [value for trace in traces for value in trace["y"]]
    return {"trace_count": len(traces), "points": len(xs), "x_range": _range(xs), "y_range": _range(ys)}


def _fmt(value: float) -> str:
    return f"{value:.4g}"


def display(series: dict | None, unit: str = "", x_unit: str = "") -> str:
    info = summary(series)
    if not info["points"]:
        return "曲线（无数据）"
    head = f"曲线 {info['trace_count']} 条 {info['points']} 点" if info["trace_count"] > 1 else f"曲线 {info['points']} 点"
    x_lo, x_hi = info["x_range"]
    y_lo, y_hi = info["y_range"]
    return (f"{head}（x {_fmt(x_lo)}–{_fmt(x_hi)}{(' ' + x_unit) if x_unit else ''}，"
            f"y {_fmt(y_lo)}–{_fmt(y_hi)}{(' ' + unit) if unit else ''}）")


def downsample(x: list[float], y: list[float], limit: int) -> tuple[list[float], list[float]]:
    """LTTB（Largest-Triangle-Three-Buckets）抽稀：保留首尾，每个桶里留与相邻桶构成最大三角形的点——
    峰、拐点、平台的形状都留得住，比等间隔抽样可靠。点数不超过 limit 原样返回。"""
    count = len(x)
    if limit >= count or limit < 3:
        return list(x), list(y)
    picked_x, picked_y = [x[0]], [y[0]]
    bucket = (count - 2) / (limit - 2)
    anchor = 0
    for index in range(limit - 2):
        start = int(math.floor(index * bucket)) + 1
        end = min(int(math.floor((index + 1) * bucket)) + 1, count - 1)
        next_start = end
        next_end = min(int(math.floor((index + 2) * bucket)) + 1, count)
        span = max(1, next_end - next_start)
        mean_x = sum(x[next_start:next_end]) / span if next_end > next_start else x[-1]
        mean_y = sum(y[next_start:next_end]) / span if next_end > next_start else y[-1]
        best, best_area = start, -1.0
        for candidate in range(start, max(start + 1, end)):
            area = abs((x[anchor] - mean_x) * (y[candidate] - y[anchor]) - (x[anchor] - x[candidate]) * (mean_y - y[anchor]))
            if area > best_area:
                best, best_area = candidate, area
        picked_x.append(x[best])
        picked_y.append(y[best])
        anchor = best
    picked_x.append(x[-1])
    picked_y.append(y[-1])
    return picked_x, picked_y


def preview(series: dict | None, limit: int = PREVIEW_POINTS) -> list[dict]:
    """每条曲线抽到最多 limit 点；曲线多时每条分到的点数按条数摊薄（至少 24 点）。"""
    traces = (series or {}).get("traces") or []
    if not traces:
        return []
    each = max(24, limit // len(traces)) if len(traces) > 1 else limit
    out = []
    for trace in traces:
        x, y = downsample(trace["x"], trace["y"], each)
        out.append({"name": trace["name"], "x": [round(value, 6) for value in x], "y": [round(value, 6) for value in y]})
    return out


def derive(series: dict | None, reducer: str) -> float | None:
    """从曲线取一个数（第一条曲线；`area` 按 x 排序后梯形积分）。没有点返回 None。"""
    traces = (series or {}).get("traces") or []
    if not traces or not traces[0]["x"]:
        return None
    x, y = traces[0]["x"], traces[0]["y"]
    if reducer == "last_y":
        return y[-1]
    if reducer == "first_y":
        return y[0]
    if reducer == "max_y":
        return max(y)
    if reducer == "min_y":
        return min(y)
    if reducer == "last_x":
        return x[-1]
    if reducer == "max_x":
        return max(x)
    if reducer == "area":
        points = sorted(zip(x, y))
        return sum((x2 - x1) * (y1 + y2) / 2 for (x1, y1), (x2, y2) in zip(points, points[1:]))
    raise ValueError(f"未知的取法 {reducer}")
