"""结果统计。

正式统计要同时满足三件事：审核 approved、质量 valid、并且是被显式选定的结果版本。
审核完成不等于质量有效——一条 approved 但 invalid 的结果照样被排除，只是排除原因
从「未审核」变成「已审核判定无效」。探索性范围可以放宽，但必须标出来，
不能直接拿去做正式报告。
"""
import math
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

# 正式统计的纳入条件
OFFICIAL_REVIEW = "approved"
OFFICIAL_QUALITY = "valid"

EXCLUSION_REASONS = {
    "pending_review": "待复核，未纳入正式统计",
    "rejected_review": "复核退回，未纳入正式统计",
    "suspect": "已审核但质量判定可疑",
    "invalid": "已审核但质量判定无效",
    "unassessed": "质量未判定",
    "not_measured": "声明无法测得，不计入也不当作 0",
    "superseded": "已被修订版本取代",
    "not_selected": "不属于选定的检测轮次或结果版本",
    # 只用于闭环训练数据：内置模拟按规则给的示意值能走通审核与报告，但不是实测，不能拿去训练
    "simulated": "模拟设备的示意值，不是实测，不作训练数据",
    # 闭环训练数据一行一个数：曲线不进；要用请在曲线指标上声明派生的数值指标
    "series": "曲线型结果，不作训练数据（用它派生的数值指标）",
}


def mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def stddev(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    average = mean(values) or 0.0
    return math.sqrt(sum((v - average) ** 2 for v in values) / (len(values) - 1))


def cv_percent(values: list[float]) -> float | None:
    """变异系数，按均值的绝对值算：负均值的指标不该得出负的 CV。均值为 0 时没有意义，返回 None。"""
    average, deviation = mean(values), stddev(values)
    if not average or deviation is None:
        return None
    return deviation / abs(average) * 100


def median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


# ---------- 正式数据集 ----------

@dataclass(frozen=True)
class Observation:
    """一条纳入判定的候选记录。

    显式绑定运行分配、检测轮次、指标定义与结果版本：不靠「最后一行」，
    也不靠任意字典覆盖。
    """

    assignment_id: str
    analysis_task_id: str
    round_no: int
    metric_id: str
    result_version: int
    value: float | None
    unit: str
    quality: str
    review_state: str
    superseded: bool = False
    not_measured_reason: str = ""
    condition_group: str = ""
    condition_label: str = ""
    is_control: bool = False
    levels: tuple = ()
    repeat: int = 1
    method_version: str = ""
    # 来自哪个批次：一个方案分多批执行时合并统计，批次是一个要保留的维度
    batch_id: str = ""


def exclusion_reason(row: Observation, official: bool = True) -> str | None:
    """返回排除原因；None 表示纳入。"""
    if row.superseded:
        return "superseded"
    if row.not_measured_reason:
        return "not_measured"
    if row.value is None:
        return "not_measured"
    if not official:
        return None
    if row.review_state != OFFICIAL_REVIEW:
        return "pending_review" if row.review_state == "pending" else "rejected_review"
    if row.quality != OFFICIAL_QUALITY:
        return row.quality if row.quality in EXCLUSION_REASONS else "unassessed"
    return None


@dataclass
class Dataset:
    metric_id: str
    metric_name: str = ""
    unit: str = ""
    official: bool = True
    included: list[Observation] = field(default_factory=list)
    excluded: list[tuple[Observation, str]] = field(default_factory=list)

    @property
    def values(self) -> list[float]:
        return [row.value for row in self.included if row.value is not None]

    def exclusion_counts(self) -> list[dict]:
        counts: dict[str, int] = {}
        for _, reason in self.excluded:
            counts[reason] = counts.get(reason, 0) + 1
        return [
            {"reason": key, "label": EXCLUSION_REASONS.get(key, key), "count": value}
            for key, value in sorted(counts.items(), key=lambda item: -item[1])
        ]


def build_dataset(
    rows: list[Observation], metric_id: str, metric_name: str = "", unit: str = "",
    official: bool = True,
) -> Dataset:
    dataset = Dataset(metric_id=metric_id, metric_name=metric_name, unit=unit, official=official)
    for row in rows:
        if row.metric_id != metric_id:
            continue
        reason = exclusion_reason(row, official)
        if reason is None:
            dataset.included.append(row)
        else:
            dataset.excluded.append((row, reason))
    return dataset


def unit_comparable(datasets: list[Dataset]) -> tuple[bool, str]:
    """跨批次比较的可比性。单位或方法版本不同就不比，条件组编号相同也不算可比。"""
    units = {d.unit for d in datasets if d.unit}
    if len(units) > 1:
        return False, f"单位不一致：{'、'.join(sorted(units))}"
    methods = {row.method_version for d in datasets for row in d.included if row.method_version}
    if len(methods) > 1:
        return False, f"方法版本不一致：{'、'.join(sorted(methods))}"
    return True, ""


def group_observations(rows: list[Observation]) -> "OrderedDict[str, list[Observation]]":
    groups: "OrderedDict[str, list[Observation]]" = OrderedDict()
    for row in rows:
        groups.setdefault(row.condition_group or "C00", []).append(row)
    return groups


def dataset_groups(dataset: Dataset) -> list[dict]:
    """按条件组汇总。纳入与排除数量都列出来，不让排除项消失。"""
    excluded_by_group: dict[str, list[tuple[Observation, str]]] = {}
    for row, reason in dataset.excluded:
        excluded_by_group.setdefault(row.condition_group or "C00", []).append((row, reason))

    rows = []
    for group, members in group_observations(dataset.included).items():
        values = [m.value for m in members if m.value is not None]
        excluded = excluded_by_group.pop(group, [])
        rows.append(
            {
                "group": group,
                "label": members[0].condition_label,
                "is_control": any(m.is_control for m in members),
                "n_included": len(values),
                "n_excluded": len(excluded),
                "n_total": len(values) + len(excluded),
                "mean": mean(values),
                "sd": stddev(values),
                "cv_pct": cv_percent(values),
                "unit": dataset.unit,
                "observations": [
                    {
                        "assignment_id": m.assignment_id,
                        "analysis_task_id": m.analysis_task_id,
                        "round_no": m.round_no,
                        "result_version": m.result_version,
                        "repeat": m.repeat,
                        "value": m.value,
                        "quality": m.quality,
                        "review_state": m.review_state,
                    }
                    for m in members
                ],
                "excluded": [
                    {
                        "assignment_id": m.assignment_id,
                        "analysis_task_id": m.analysis_task_id,
                        "result_version": m.result_version,
                        "reason": reason,
                        "reason_label": EXCLUSION_REASONS.get(reason, reason),
                        "quality": m.quality,
                        "review_state": m.review_state,
                    }
                    for m, reason in excluded
                ],
            }
        )
    # 整组都被排除的条件组也要出现，否则界面上它就凭空消失了
    for group, excluded in excluded_by_group.items():
        rows.append(
            {
                "group": group,
                "label": excluded[0][0].condition_label,
                "is_control": any(m.is_control for m, _ in excluded),
                "n_included": 0,
                "n_excluded": len(excluded),
                "n_total": len(excluded),
                "mean": None, "sd": None, "cv_pct": None, "unit": dataset.unit,
                "observations": [],
                "excluded": [
                    {
                        "assignment_id": m.assignment_id,
                        "analysis_task_id": m.analysis_task_id,
                        "result_version": m.result_version,
                        "reason": reason,
                        "reason_label": EXCLUSION_REASONS.get(reason, reason),
                        "quality": m.quality,
                        "review_state": m.review_state,
                    }
                    for m, reason in excluded
                ],
            }
        )
    return sorted(rows, key=lambda row: row["group"])


def dataset_effects(dataset: Dataset, factors: list[dict], plan_type: str = "matrix") -> list[dict]:
    """因子主效应。非矩阵实验没有因子矩阵，返回空列表而不是编造单水平效应。"""
    if plan_type != "matrix" or not factors:
        return []
    effects = []
    for position, factor in enumerate(factors):
        levels = []
        for level in factor.get("levels") or []:
            values = [
                row.value for row in dataset.included
                if row.value is not None
                and len(row.levels) > position
                and row.levels[position] == level
            ]
            levels.append({"level": level, "mean": mean(values), "n": len(values)})
        means = [row["mean"] for row in levels if row["mean"] is not None]
        effects.append(
            {
                "factor": factor.get("name"),
                "unit": factor.get("unit", ""),
                "levels": levels,
                "range": (max(means) - min(means)) if len(means) > 1 else None,
            }
        )
    return effects


def dataset_summary(dataset: Dataset, groups: list[dict], plan_repeats: int | None) -> dict:
    cvs = [g["cv_pct"] for g in groups if g["cv_pct"] is not None]
    # 只列均值最高与最低的条件组，不判「最佳」：越大越好、越小越好还是越接近目标值越好，取决于指标与方案，
    # 这里不知道（杂质、误差率、黏度这类指标，均值最高恰恰最差）
    ranked = sorted((g for g in groups if g["n_included"] >= 1 and g["mean"] is not None), key=lambda g: g["mean"])
    highest = ranked[-1] if ranked else None
    lowest = ranked[0] if len(ranked) > 1 else None
    return {
        "metric_id": dataset.metric_id,
        "metric_name": dataset.metric_name,
        "unit": dataset.unit,
        "official": dataset.official,
        "groups": len(groups),
        "included": len(dataset.included),
        "excluded": len(dataset.excluded),
        "exclusions": dataset.exclusion_counts(),
        "mean": mean(dataset.values),
        "sd": stddev(dataset.values),
        "cv_pct": cv_percent(dataset.values),
        "median_cv_pct": median(cvs),
        "high_cv_groups": len([c for c in cvs if c > 3]),
        "single_repeat": bool(plan_repeats is not None and plan_repeats < 2),
        "highest_group": highest["group"] if highest else None,
        "highest_mean": highest["mean"] if highest else None,
        "lowest_group": lowest["group"] if lowest else None,
        "lowest_mean": lowest["mean"] if lowest else None,
    }


# ---------- 跨批合并：分批明细与批次差异 ----------

def _betacf(a: float, b: float, x: float) -> float:
    """不完全 Beta 函数的连分式（Lentz 算法）。"""
    tiny, eps = 1e-300, 3e-14
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def regularized_beta(a: float, b: float, x: float) -> float:
    """正则化不完全 Beta 函数 I_x(a, b)。"""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    front = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1 - x))
    if x < (a + 1) / (a + b + 2):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1 - x) / b


def f_survival(f: float, df1: int, df2: int) -> float:
    """F 分布的上尾概率 P(F > f)。"""
    if f <= 0 or df1 <= 0 or df2 <= 0:
        return 1.0
    return regularized_beta(df2 / 2, df1 / 2, df2 / (df2 + df1 * f))


def batch_breakdown(dataset: Dataset) -> list[dict]:
    """按批次汇总纳入正式统计的数值：每批 n、均值、SD、CV。批次按首次出现的顺序。"""
    order: list[str] = []
    values: dict[str, list[float]] = {}
    excluded: dict[str, int] = {}
    for row in dataset.included:
        if row.value is None:
            continue
        if row.batch_id not in values:
            order.append(row.batch_id)
            values[row.batch_id] = []
        values[row.batch_id].append(row.value)
    for row, _ in dataset.excluded:
        excluded[row.batch_id] = excluded.get(row.batch_id, 0) + 1
        if row.batch_id not in values:
            order.append(row.batch_id)
            values[row.batch_id] = []
    return [
        {
            "batch_id": batch_id, "n_included": len(values[batch_id]), "n_excluded": excluded.get(batch_id, 0),
            "mean": mean(values[batch_id]), "sd": stddev(values[batch_id]), "cv_pct": cv_percent(values[batch_id]),
        }
        for batch_id in order
    ]


def batch_effect(dataset: Dataset, alpha: float = 0.05) -> dict | None:
    """批次之间有没有系统差异：单因素方差分析，矩阵方案先按条件组校正。

    每个数值先减去它所在条件组（全部批次合在一起）的均值，再比较各批次的均值——矩阵方案每批都包含
    全部条件，这样看到的是批次本身的差异，不是各批条件搭配不同。残差自由度扣掉条件组数：
    N − 批次数 − (条件组数 − 1)。样本缺失使各批条件不再严格成比例时结果是近似的，照样标出来。
    少于两个批次、或自由度不够时返回 None。
    """
    rows = [row for row in dataset.included if row.value is not None]
    batches = sorted({row.batch_id for row in rows})
    groups = sorted({row.condition_group or "C00" for row in rows})
    if len(batches) < 2:
        return None
    group_mean = {
        group: mean([row.value for row in rows if (row.condition_group or "C00") == group]) or 0.0 for group in groups
    }
    centered: dict[str, list[float]] = {batch: [] for batch in batches}
    for row in rows:
        centered[row.batch_id].append(row.value - group_mean[row.condition_group or "C00"])
    total = len(rows)
    df1 = len(batches) - 1
    df2 = total - len(batches) - (len(groups) - 1)
    if df2 < 1:
        return None
    grand = mean([value for values in centered.values() for value in values]) or 0.0
    between = sum(len(values) * ((mean(values) or 0.0) - grand) ** 2 for values in centered.values())
    within = sum(sum((value - (mean(values) or 0.0)) ** 2 for value in values) for values in centered.values())
    if within <= 0:
        return {
            "method": "anova_centered" if len(groups) > 1 else "anova",
            "f": None, "df1": df1, "df2": df2, "p": None, "significant": between > 0,
            "note": "批内没有波动，无法做方差检验" + ("；各批均值不同" if between > 0 else ""),
        }
    f_value = (between / df1) / (within / df2)
    p_value = f_survival(f_value, df1, df2)
    return {
        "method": "anova_centered" if len(groups) > 1 else "anova",
        "f": f_value, "df1": df1, "df2": df2, "p": p_value, "alpha": alpha,
        "significant": p_value < alpha,
        "note": (
            ("各条件组先减去组均值后比较批次；" if len(groups) > 1 else "")
            + (f"批次之间差异显著（p = {p_value:.3g} < {alpha}）：合并统计前先确认原因（如物料批号、设备与通道、操作日期）"
               if p_value < alpha else f"没有发现批次之间的显著差异（p = {p_value:.3g}）")
        ),
    }


# ---------- 历史固定三指标（旧批次分析页仍在用） ----------

def valid_values(samples: list[dict], metric: str) -> list[float]:
    return [
        s["metrics"][metric]
        for s in samples
        if s.get("quality") == "valid" and (s.get("metrics") or {}).get(metric) is not None
    ]


def group_samples(samples: list[dict]) -> "OrderedDict[str, list[dict]]":
    groups: "OrderedDict[str, list[dict]]" = OrderedDict()
    for sample in samples:
        groups.setdefault(sample.get("condition_group") or "C00", []).append(sample)
    return groups


def group_statistics(
    samples: list[dict], golden_samples: list[dict] | None = None,
    metric: str = "discharge_capacity",
) -> list[dict]:
    golden_means = {}
    if golden_samples:
        for group, rows in group_samples(golden_samples).items():
            golden_means[group] = mean(valid_values(rows, metric))

    rows = []
    for group, group_rows in group_samples(samples).items():
        values = valid_values(group_rows, metric)
        golden_mean = golden_means.get(group)
        group_mean = mean(values)
        rows.append(
            {
                "group": group,
                "label": group_rows[0].get("condition_label", ""),
                "is_control": any(r.get("is_control") for r in group_rows),
                "n_valid": len(values),
                "n_total": len(group_rows),
                "mean": group_mean,
                "sd": stddev(values),
                "cv_pct": cv_percent(values),
                "areal_density": mean(valid_values(group_rows, "areal_density")),
                "golden_mean": golden_mean,
                "delta": (group_mean - golden_mean) if (golden_mean is not None and group_mean is not None) else None,
                "samples": [
                    {"id": r["id"], "well": r["well"], "repeat": r.get("repeat"), "state": r.get("state"),
                     "quality": r.get("quality"), "value": (r.get("metrics") or {}).get(metric)}
                    for r in group_rows
                ],
            }
        )
    return rows


def factor_effects(samples: list[dict], factors: list[dict], metric: str = "discharge_capacity") -> list[dict]:
    effects = []
    for position, factor in enumerate(factors):
        levels = []
        for level in factor.get("levels") or []:
            values = [
                s["metrics"][metric]
                for s in samples
                if s.get("quality") == "valid"
                and s.get("levels")
                and position < len(s["levels"])
                and s["levels"][position] == level
                and (s.get("metrics") or {}).get(metric) is not None
            ]
            levels.append({"level": level, "mean": mean(values), "n": len(values)})
        means = [row["mean"] for row in levels if row["mean"] is not None]
        effects.append(
            {
                "factor": factor.get("name"),
                "unit": factor.get("unit", ""),
                "levels": levels,
                "range": (max(means) - min(means)) if len(means) > 1 else None,
            }
        )
    return effects


def summary(groups: list[dict], plan_repeats: int | None) -> dict[str, Any]:
    cvs = [g["cv_pct"] for g in groups if g["cv_pct"] is not None]
    eligible = [g for g in groups if g["n_valid"] >= min(2, max(1, g["n_total"]))]
    best = max(eligible, key=lambda g: g["mean"] if g["mean"] is not None else -1, default=None)
    deltas = [g["delta"] for g in groups if g["delta"] is not None]
    return {
        "groups": len(groups),
        "valid_samples": sum(g["n_valid"] for g in groups),
        "total_samples": sum(g["n_total"] for g in groups),
        "median_cv_pct": median(cvs),
        "high_cv_groups": len([c for c in cvs if c > 3]),
        "single_repeat": bool(plan_repeats is not None and plan_repeats < 2),
        "best_group": best["group"] if best else None,
        "best_mean": best["mean"] if best else None,
        "delta_vs_golden": mean(deltas),
    }
