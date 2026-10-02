"""技术（与品牌无关）：程序配置核对、ILCS 指令参数覆盖、和仪器极限核对、测量数据 → 曲线与派生指标。

五个技术，参数一律 SI 单位、键名带单位：

| 技术 | 参数 |
|---|---|
| ocp 开路电位 | duration_s、interval_s |
| lsv 线性扫描 | e_begin_V（数，或 "ocp"：先开路静置 rest_s 秒，从静置最后的开路电位起扫）、e_end_V、scan_rate_V_s、e_step_V；可选 onset_threshold_mA_cm2（要有电极面积）/ onset_threshold_mA、stop_mA_cm2 / stop_mA（电流到这就提前停）、rest_s、rest_interval_s |
| cv 循环伏安 | e_begin_V、e_vertex1_V、e_vertex2_V、e_step_V、scan_rate_V_s、cycles（1–50） |
| ca 计时电流 | e_V、duration_s、interval_s |
| eis 阻抗谱 | freq_start_Hz、freq_end_Hz、points_per_decade、amplitude_Vrms（有效值）、e_dc_V（数或 "ocp"，缺省 0）、rest_s、rest_interval_s |

每个程序还可以写：`current`（{"start_A", "autorange_A": [下限, 上限] 或 null = 固定量程}）、`potential_range_V`
（测开路电位时的电位量程）、`bandwidth_Hz`、`cell`（{"area_cm2", "cell_constant_per_cm"}，覆盖网关级的）。

ILCS 指令能改的参数要在网关配置的 `params` 里登记范围，且只对用得上它的技术有效（OVERRIDES）；别的一律拒绝。
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any

from . import analysis
from .backend import OVERLOAD, TIMING, Limits, Plan, Point, Ranging

TECHNIQUE_NAMES = {"ocp": "开路电位", "lsv": "线性扫描伏安", "cv": "循环伏安", "ca": "计时电流", "eis": "电化学阻抗谱"}
SETTINGS = {
    "ocp": ("duration_s", "interval_s"),
    "lsv": ("e_begin_V", "e_end_V", "scan_rate_V_s", "e_step_V", "onset_threshold_mA_cm2", "onset_threshold_mA",
            "stop_mA_cm2", "stop_mA", "rest_s", "rest_interval_s"),
    "cv": ("e_begin_V", "e_vertex1_V", "e_vertex2_V", "e_step_V", "scan_rate_V_s", "cycles"),
    "ca": ("e_V", "duration_s", "interval_s"),
    "eis": ("freq_start_Hz", "freq_end_Hz", "points_per_decade", "amplitude_Vrms", "e_dc_V", "rest_s",
            "rest_interval_s"),
}
REQUIRED = {
    "ocp": ("duration_s", "interval_s"), "lsv": ("e_begin_V", "e_end_V", "scan_rate_V_s", "e_step_V"),
    "cv": ("e_begin_V", "e_vertex1_V", "e_vertex2_V", "e_step_V", "scan_rate_V_s"),
    "ca": ("e_V", "duration_s", "interval_s"),
    "eis": ("freq_start_Hz", "freq_end_Hz", "points_per_decade", "amplitude_Vrms"),
}
COMMON = ("name", "technique", "current", "potential_range_V", "bandwidth_Hz", "cell")
CELL_KEYS = ("area_cm2", "cell_constant_per_cm")
# ILCS 指令能改的参数（还要在网关配置 params 里登记范围）
OVERRIDES = {
    "ocp": ("duration_s",),
    "lsv": ("e_end_V", "scan_rate_V_s", "area_cm2"),
    "cv": ("scan_rate_V_s", "cycles", "area_cm2"),
    "ca": ("e_V", "duration_s", "area_cm2"),
    "eis": ("e_dc_V", "amplitude_Vrms", "cell_constant_per_cm"),
}
OVERRIDABLE = tuple(sorted({key for keys in OVERRIDES.values() for key in keys}))
INTEGER_KEYS = ("cycles", "points_per_decade")
SIGNED_KEYS = ("e_begin_V", "e_end_V", "e_vertex1_V", "e_vertex2_V", "e_V", "e_dc_V")
MAX_CYCLES = 50            # ILCS 一个结果最多 50 条曲线
MAX_RAW_POINTS = 1_000_000  # 一次测量的点数上限（回报前抽稀到 max_points）
MAX_TOTAL_POINTS = 200_000  # ILCS 一个结果最多 20 万点
DEFAULT_CURRENT = {"start_A": 1e-4, "autorange_A": [1e-9, 1e-2]}
DEFAULT_REST_S = 10.0
MAX_BANDWIDTH = 100.0      # 缺省带宽的上限（EmStat Pico 低速模式最高 100 Hz）


class PlanError(ValueError):
    """程序或参数不成立：仪器没动（invalid）。"""


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _positive(value: Any) -> bool:
    return _number(value) and value > 0


def _integer(value: Any) -> bool:
    return _number(value) and float(value).is_integer()


@dataclass(frozen=True)
class Cell:
    """接在这个通道上的电池：电极面积（cm²，电流换算成电流密度）、电导池常数 K（cm⁻¹，σ = K / R）。"""

    area_cm2: float | None = None
    cell_constant_per_cm: float | None = None

    @classmethod
    def parse(cls, raw: Any, base: "Cell | None" = None, where: str = "cell") -> tuple["Cell", list[str]]:
        base = base or cls()
        if raw is None:
            return base, []
        if not isinstance(raw, dict):
            return base, [f"{where} 要写成 {{\"area_cm2\", \"cell_constant_per_cm\"}}"]
        problems = [f"{where} 不认 {key}" for key in sorted(set(raw) - set(CELL_KEYS))]
        values = {}
        for key in CELL_KEYS:
            value = raw.get(key, getattr(base, key))
            if value is not None and not _positive(value):
                problems.append(f"{where}.{key} 要写正数（或不写）")
                value = getattr(base, key)
            values[key] = float(value) if value is not None else None
        return cls(**values), problems


@dataclass(frozen=True)
class Program:
    code: str
    name: str
    technique: str
    raw: dict[str, Any]                 # 配置里写的技术参数（覆盖之后要重新核对）
    settings: dict[str, Any]            # 核对过、补了派生值的
    ranging: Ranging
    bandwidth_Hz: float | None = None
    cell: Cell = field(default_factory=Cell)


# ---------- 技术参数 ----------

def check_settings(technique: str, raw: dict[str, Any], cell: Cell) -> tuple[dict[str, Any], list[str]]:
    """核对一个技术的参数，补上派生值（EIS 的总点数、LSV 的截止电流 stop_A、静置间隔）。返回 (参数, 问题)。"""
    problems: list[str] = []
    s = {key: raw[key] for key in SETTINGS[technique] if key in raw and raw[key] is not None}
    for key in REQUIRED[technique]:
        if key not in s:
            problems.append(f"缺 {key}")
    for key, value in list(s.items()):
        if value == "ocp" and key in {"e_begin_V", "e_dc_V"} and technique in {"lsv", "eis"}:
            continue
        if key in SIGNED_KEYS:
            if not _number(value):
                problems.append(f"{key} = {value!r} 不是数" + ("（或 \"ocp\"）" if key in {"e_begin_V", "e_dc_V"} else ""))
        elif key in INTEGER_KEYS:
            if not _integer(value) or value < 1:
                problems.append(f"{key} = {value!r} 要是不小于 1 的整数")
        elif not _positive(value):
            problems.append(f"{key} = {value!r} 要是正数")
    if problems:
        return s, problems
    for key in s:
        if key in INTEGER_KEYS:
            s[key] = int(s[key])
        elif s[key] != "ocp":
            s[key] = float(s[key])

    def rest() -> None:
        s.setdefault("rest_s", DEFAULT_REST_S)
        s.setdefault("rest_interval_s", min(1.0, s["rest_s"] / 10))
        if s["rest_interval_s"] > s["rest_s"]:
            problems.append("rest_interval_s 不能大于 rest_s")

    def per_area(base: str) -> float | None:
        """`base`_mA_cm2（要有面积）或 `base`_mA → A。"""
        dense, plain = s.get(f"{base}_mA_cm2"), s.get(f"{base}_mA")
        if dense is not None and plain is not None:
            problems.append(f"{base}_mA_cm2 与 {base}_mA 只能写一个")
            return None
        if dense is not None:
            if cell.area_cm2 is None:
                problems.append(f"{base}_mA_cm2 要有电极面积（cell.area_cm2）；没有面积就写 {base}_mA")
                return None
            return dense * cell.area_cm2 / 1000.0
        return plain / 1000.0 if plain is not None else None

    if technique in {"ocp", "ca"}:
        count = math.floor(s["duration_s"] / s["interval_s"] + 1e-9)
        if count < 2:
            problems.append(f"duration_s / interval_s 只有 {count} 个点：曲线至少要 2 个点")
        elif count > MAX_RAW_POINTS:
            problems.append(f"一次 {count} 个点太多（上限 {MAX_RAW_POINTS}）：加大 interval_s")
    elif technique == "lsv":
        if s["e_begin_V"] == "ocp":
            rest()
        else:
            if "rest_s" in s or "rest_interval_s" in s:
                problems.append("rest_s / rest_interval_s 只在 e_begin_V 为 \"ocp\" 时用")
            steps = abs(s["e_end_V"] - s["e_begin_V"]) / s["e_step_V"]
            if steps < 1:
                problems.append("e_begin_V 到 e_end_V 不够一个 e_step_V")
            elif steps > MAX_RAW_POINTS:
                problems.append(f"一次 {int(steps)} 个点太多（上限 {MAX_RAW_POINTS}）：加大 e_step_V")
        s["onset_threshold_A"] = per_area("onset_threshold")
        s["stop_A"] = per_area("stop")
    elif technique == "cv":
        s.setdefault("cycles", 1)
        if s["cycles"] > MAX_CYCLES:
            problems.append(f"cycles 最多 {MAX_CYCLES}（ILCS 一个结果最多 {MAX_CYCLES} 条曲线）")
        if s["e_vertex1_V"] == s["e_vertex2_V"]:
            problems.append("e_vertex1_V 与 e_vertex2_V 不能相同")
        span = (abs(s["e_vertex1_V"] - s["e_begin_V"]) + abs(s["e_vertex2_V"] - s["e_vertex1_V"])
                + abs(s["e_begin_V"] - s["e_vertex2_V"]))
        if span / s["e_step_V"] * s["cycles"] > MAX_RAW_POINTS:
            problems.append(f"点数超过 {MAX_RAW_POINTS}：加大 e_step_V 或减少 cycles")
    elif technique == "eis":
        s.setdefault("e_dc_V", 0.0)
        if s["e_dc_V"] == "ocp":
            rest()
        elif "rest_s" in s or "rest_interval_s" in s:
            problems.append("rest_s / rest_interval_s 只在 e_dc_V 为 \"ocp\" 时用")
        if s["freq_start_Hz"] == s["freq_end_Hz"]:
            problems.append("freq_start_Hz 与 freq_end_Hz 不能相同")
        else:
            decades = abs(math.log10(s["freq_start_Hz"] / s["freq_end_Hz"]))
            s["points"] = max(2, int(round(decades * s["points_per_decade"])) + 1)
            if s["points"] > 1000:
                problems.append(f"一次 {s['points']} 个频率点太多（上限 1000）")
    return s, problems


def _ranging(raw: Any, where: str) -> tuple[Ranging, list[str]]:
    raw = DEFAULT_CURRENT if raw is None else raw
    if not isinstance(raw, dict):
        return Ranging(1e-4, 1e-9, 1e-2), [f"{where}.current 要写成 {{\"start_A\", \"autorange_A\": [下限, 上限]}}"]
    problems = [f"{where}.current 不认 {key}" for key in sorted(set(raw) - {"start_A", "autorange_A"})]
    start = raw.get("start_A", DEFAULT_CURRENT["start_A"])
    span = raw["autorange_A"] if "autorange_A" in raw else DEFAULT_CURRENT["autorange_A"]
    if not _positive(start):
        problems.append(f"{where}.current.start_A 要写正数（预计最大电流，A）")
        start = 1e-4
    if span is None:  # 固定量程
        return Ranging(float(start), float(start), float(start)), problems
    if not (isinstance(span, (list, tuple)) and len(span) == 2 and all(_positive(v) for v in span)
            and span[0] <= start <= span[1]):
        problems.append(f"{where}.current.autorange_A 要写 [下限, 上限]（A，下限 ≤ start_A ≤ 上限），或 null 固定量程")
        return Ranging(float(start), float(start), float(start)), problems
    return Ranging(float(start), float(span[0]), float(span[1])), problems


def parse_program(code: str, raw: Any, base_cell: Cell) -> tuple[Program | None, list[str]]:
    where = f"程序 {code}"
    if not isinstance(raw, dict):
        return None, [f"{where} 要写成对象"]
    technique = raw.get("technique")
    if technique not in SETTINGS:
        return None, [f"{where} 的 technique 只能是 {' / '.join(SETTINGS)}，不是 {technique!r}"]
    problems = [f"{where}（{technique}）不认 {key}"
                for key in sorted(set(raw) - set(COMMON) - set(SETTINGS[technique]))]
    cell, found = Cell.parse(raw.get("cell"), base_cell, f"{where}.cell")
    problems += found
    settings_raw = {key: raw[key] for key in SETTINGS[technique] if key in raw}
    settings, found = check_settings(technique, settings_raw, cell)
    problems += [f"{where}：{problem}" for problem in found]
    ranging, found = _ranging(raw.get("current"), where)
    problems += found
    potential = raw.get("potential_range_V")
    if potential is not None and not _positive(potential):
        problems.append(f"{where}.potential_range_V 要写正数（V）")
        potential = None
    bandwidth = raw.get("bandwidth_Hz")
    if bandwidth is not None and not _positive(bandwidth):
        problems.append(f"{where}.bandwidth_Hz 要写正数（Hz）")
        bandwidth = None
    ranging = Ranging(ranging.start_A, ranging.min_A, ranging.max_A, float(potential) if potential else None)
    program = Program(code=code, name=str(raw.get("name") or code), technique=technique, raw=settings_raw,
                      settings=settings, ranging=ranging, bandwidth_Hz=float(bandwidth) if bandwidth else None,
                      cell=cell)
    return program, problems


def default_bandwidth(technique: str, s: dict[str, Any]) -> float:
    """缺省带宽：数据点频率的 4 倍，1 Hz–100 Hz 之间。EIS 不用（仪器按频率定）。"""
    if technique in {"ocp", "ca"}:
        rate = 1.0 / s["interval_s"]
    elif technique in {"lsv", "cv"}:
        rate = s["scan_rate_V_s"] / s["e_step_V"]
    else:
        return MAX_BANDWIDTH
    return min(MAX_BANDWIDTH, max(1.0, 4.0 * rate))


def potentials(technique: str, s: dict[str, Any]) -> list[float]:
    """这次测量会加到电池上的电位（开路起扫的起点事先不知道，不算）。"""
    keys = {"ocp": (), "lsv": ("e_begin_V", "e_end_V"), "cv": ("e_begin_V", "e_vertex1_V", "e_vertex2_V"),
            "ca": ("e_V",), "eis": ("e_dc_V",)}[technique]
    return [float(s[key]) for key in keys if _number(s.get(key))]


def make_plan(program: Program, overrides: dict[str, Any], *, allowed: dict[str, tuple[float, float]],
              e_limits: tuple[float | None, float | None] = (None, None)) -> tuple[Plan, Cell]:
    """程序 + ILCS 指令带的参数 → 一次测量。`allowed` 是网关配置 params 登记的范围。参数不成立抛 PlanError。"""
    technique = program.technique
    raw, cell_values, problems = dict(program.raw), {}, []
    for key, value in overrides.items():
        if key not in OVERRIDES[technique]:
            problems.append(f"程序 {program.code}（{TECHNIQUE_NAMES[technique]}）用不上参数 {key}；"
                            f"能改的：{'、'.join(k for k in OVERRIDES[technique] if k in allowed) or '没有'}")
            continue
        if key not in allowed:
            problems.append(f"网关配置的 params 里没有登记 {key}：不接受")
            continue
        low, high = allowed[key]
        if not _number(value) or (key in INTEGER_KEYS and not _integer(value)):
            problems.append(f"{key} = {value!r} 不是{'整数' if key in INTEGER_KEYS else '数'}")
            continue
        if not low <= value <= high:
            problems.append(f"{key} = {value:g} 超出网关允许的 {low:g}–{high:g}")
            continue
        if key in CELL_KEYS:
            cell_values[key] = float(value)
        else:
            raw[key] = int(value) if key in INTEGER_KEYS else float(value)
    if problems:
        raise PlanError("；".join(problems))
    cell = Cell(**{**{key: getattr(program.cell, key) for key in CELL_KEYS}, **cell_values})
    settings, problems = check_settings(technique, raw, cell)
    low, high = e_limits
    for value in potentials(technique, settings) if not problems else []:
        if (low is not None and value < low) or (high is not None and value > high):
            problems.append(f"电位 {value:g} V 超出网关允许的 {low if low is not None else '-∞'}–"
                            f"{high if high is not None else '∞'} V")
    if problems:
        raise PlanError("；".join(problems))
    bandwidth = program.bandwidth_Hz or default_bandwidth(technique, settings)
    return Plan(technique=technique, settings=settings, ranging=program.ranging, bandwidth_Hz=bandwidth,
                label=program.code), cell


def instrument_problems(plan: Plan, limits: Limits | None) -> list[str]:
    """和仪器自己的极限核对（手册附录 B）：电位范围、一次能扫的跨度、EIS 最高频率与最大振幅、电流量程。"""
    if limits is None:
        return []
    s, problems = plan.settings, []
    values = potentials(plan.technique, s)
    for value in values:
        if not limits.e_min_V <= value <= limits.e_max_V:
            problems.append(f"电位 {value:g} V 超出这台仪器的 {limits.e_min_V:g}–{limits.e_max_V:g} V")
    if plan.technique in {"lsv", "cv"} and len(values) >= 2 and max(values) - min(values) > limits.window_V + 1e-9:
        problems.append(f"扫描跨度 {max(values) - min(values):g} V 超过这台仪器一次能扫的 {limits.window_V:g} V")
    if plan.technique == "eis":
        top = max(s["freq_start_Hz"], s["freq_end_Hz"])
        if limits.eis_max_hz and top > limits.eis_max_hz:
            problems.append(f"最高频率 {top:g} Hz 超过这台仪器的 {limits.eis_max_hz:g} Hz")
        if limits.eis_max_vrms and s["amplitude_Vrms"] > limits.eis_max_vrms:
            problems.append(f"振幅 {s['amplitude_Vrms']:g} Vrms 超过这台仪器的 {limits.eis_max_vrms:g} Vrms")
    if limits.i_max_A and plan.ranging.start_A > limits.i_max_A * 1.0001:
        problems.append(f"起始电流量程 {plan.ranging.start_A:g} A 超过这台仪器能测的 {limits.i_max_A:g} A")
    return problems


# ---------- 结果 ----------

class ResultError(ValueError):
    """测完了但数据不成立（点太少）。"""


def _r(value: float) -> float:
    """回报的数和曲线上的点同一个取法（6 位有效数字）：起始电位、峰电位能在曲线上找到同一个点。"""
    return analysis.rounded([value])[0]


def _curve(x: list[float], y: list[float], limit: int) -> dict[str, list[float]]:
    x, y = analysis.binned(x, y, limit)
    return {"x": analysis.rounded(x), "y": analysis.rounded(y)}


def _current(cell: Cell) -> tuple[float, str]:
    """电流的换算：有面积就是电流密度 mA/cm²，没有就是 mA。"""
    if cell.area_cm2:
        return 1000.0 / cell.area_cm2, "mA/cm2"
    return 1000.0, "mA"


def _rest_ocp(rest: list[Point]) -> float | None:
    """开路静置最后一点的电位（扫描 / 阻抗的起点）；没有有效的点是 None（回执里不写 NaN）。"""
    values = [p.values.get("e_V") for p in rest]
    values = [value for value in values if analysis.finite(value)]
    return _r(values[-1]) if values else None


def _pairs(points: list[Point], x_key: str, y_key: str, *, need: int = 2) -> tuple[list[float], list[float]]:
    """取两列（去掉缺测、无效的点）。不到 `need` 个点抛 ResultError。"""
    rows = [(p.values.get(x_key), p.values.get(y_key)) for p in points]
    kept = [(a, b) for a, b in rows if analysis.finite(a, b)]
    if len(kept) < need:
        raise ResultError(f"{x_key} / {y_key} 只有 {len(kept)} 个有效数据点，曲线不成立")
    return [a for a, _ in kept], [b for _, b in kept]


def summarize(program: Program, plan: Plan, cell: Cell, points: list[Point], *, max_points: int) -> dict[str, Any]:
    """一次做完的测量 → 回报的一行（单个电池时就是 delivered；带孔位时放在 delivered.wells[孔位]）。"""
    s, technique = plan.settings, plan.technique
    main = [p for p in points if p.segment == technique]
    rest = [p for p in points if p.segment == "ocp"] if technique != "ocp" else []
    if len(main) < 2:
        raise ResultError(f"只收到 {len(main)} 个 {TECHNIQUE_NAMES[technique]} 数据点，曲线不成立")
    row: dict[str, Any] = {"program": program.code, "technique": technique}
    notes: list[str] = []
    factor, unit = _current(cell)
    if technique == "ocp":
        t, e = _pairs(main, "t_s", "e_V")
        row["ocp_curve"] = _curve(t, e, max_points)
        row["ocp_V"] = _r(analysis.tail_mean(e))
        row["duration_s"] = round(t[-1], 3) if t else None
    elif technique == "lsv":
        e, i = _pairs(main, "e_V", "i_A")
        y = [value * factor for value in i]
        row["lsv"] = _curve(e, y, max_points)
        row["current_unit"] = unit
        row["e_begin_V"], row["e_end_V"] = _r(e[0]), _r(e[-1])
        row["scan_rate_V_s"] = s["scan_rate_V_s"]
        rest_ocp = _rest_ocp(rest)
        if rest_ocp is not None:
            row["rest_ocp_V"] = rest_ocp
        threshold = s.get("onset_threshold_A")
        if threshold:
            limit = threshold * factor
            found = analysis.onset(e, y, limit)
            row["onset_potential_V"] = _r(found) if found is not None else None
            row["onset_threshold"] = _r(limit)
            if found is None:
                notes.append(f"扫到 {e[-1]:.3f} V 电流都没到阈值 {limit:g} {unit}")
        stop = s.get("stop_A")
        if stop and abs(e[-1] - s["e_end_V"]) > s["e_step_V"] / 2:
            row["stopped_at_cutoff"] = True
            notes.append(f"电流到截止值 {stop * factor:g} {unit}，扫到 {e[-1]:.3f} V 提前停")
        if cell.area_cm2:
            row["area_cm2"] = cell.area_cm2
    elif technique == "cv":
        scans = sorted({p.scan for p in main})
        per_trace = max(2, min(max_points, MAX_TOTAL_POINTS // max(1, len(scans))))
        traces, last = [], ([], [])
        for scan in scans:
            e, i = _pairs([p for p in main if p.scan == scan], "e_V", "i_A", need=0)
            if len(e) < 2:
                continue
            y = [value * factor for value in i]
            traces.append({"name": f"第 {scan + 1} 圈", **_curve(e, y, per_trace)})
            last = (e, y)
        if not traces:
            raise ResultError("每一圈都不到 2 个数据点，曲线不成立")
        row["cv"] = {"traces": traces}
        row["current_unit"] = unit
        row["cycles"] = len(traces)
        row["scan_rate_V_s"] = s["scan_rate_V_s"]
        peaks = analysis.extremes(*last)
        if peaks:
            prefix, suffix = ("j", "mA_cm2") if cell.area_cm2 else ("i", "mA")
            row[f"{prefix}pa_{suffix}"], row["epa_V"] = _r(peaks["y_max"]), _r(peaks["e_max_V"])
            row[f"{prefix}pc_{suffix}"], row["epc_V"] = _r(peaks["y_min"]), _r(peaks["e_min_V"])
        if cell.area_cm2:
            row["area_cm2"] = cell.area_cm2
    elif technique == "ca":
        t, i = _pairs(main, "t_s", "i_A")
        y = [value * factor for value in i]
        row["ca_curve"] = _curve(t, y, max_points)
        row["current_unit"] = unit
        row["e_V"] = s["e_V"]
        row["i_end_mA"] = _r(analysis.tail_mean([value * 1000.0 for value in i]))
        if cell.area_cm2:
            row["j_end_mA_cm2"] = _r(analysis.tail_mean(y))
            row["area_cm2"] = cell.area_cm2
    elif technique == "eis":
        rows = [(p.values.get("f_Hz"), p.values.get("z_re_ohm"), p.values.get("z_im_ohm")) for p in main]
        rows = [row_ for row_ in rows if analysis.finite(*row_)]
        dropped = len(main) - len(rows)
        if len(rows) < 2:
            raise ResultError(f"只有 {len(rows)} 个有效的频率点，曲线不成立")
        f = [r[0] for r in rows]
        zr = [r[1] for r in rows]
        zi = [r[2] for r in rows]
        row["nyquist"] = _curve(zr, [-value for value in zi], max_points)
        modulus = [math.hypot(a, b) for a, b in zip(zr, zi)]
        phase = [-math.degrees(math.atan2(b, a)) for a, b in zip(zr, zi)]
        row["bode"] = {"traces": [{"name": "|Z|（Ω）", **_curve(f, modulus, max_points)},
                                  {"name": "−相位（°）", **_curve(f, phase, max_points)}]}
        bulk = analysis.r_bulk(f, zr, zi)
        if bulk is not None:
            row["r_bulk_ohm"] = _r(bulk["r_ohm"])
            row["r_bulk_method"] = bulk["method"]
            row["r_bulk_freq_Hz"] = _r(bulk["freq_Hz"])
            if bulk["method"] != "zero_crossing":
                notes.append("高频端 −Z'' 没有过零：R_b 取高频端 |−Z''| 最小的点的 Z'，会略偏大")
        if cell.cell_constant_per_cm:
            row["cell_constant_per_cm"] = cell.cell_constant_per_cm
            sigma = analysis.conductivity_mS_cm(cell.cell_constant_per_cm, bulk["r_ohm"]) if bulk else None
            row["conductivity_mS_cm"] = _r(sigma) if sigma is not None else None
        row["freq_range_Hz"] = [_r(min(f)), _r(max(f))]
        row["amplitude_Vrms"] = s["amplitude_Vrms"]
        row["e_dc_V"] = _rest_ocp(rest) if s["e_dc_V"] == "ocp" else s["e_dc_V"]
        if dropped:
            notes.append(f"{dropped} 个频率点仪器报了无效值（nan），已去掉")
    row["points"] = len(main)
    row["overload_points"] = sum(1 for p in main if p.status & OVERLOAD)
    row["timing_errors"] = sum(1 for p in main if p.status & TIMING)
    if row["overload_points"]:
        notes.append(f"{row['overload_points']} 个点电流过载（超过量程的 95%）：这些点不可靠，放宽 current.autorange_A 的上限重测")
    if row["timing_errors"]:
        notes.append(f"{row['timing_errors']} 个点时序没跟上：数据点间隔不准")
    if notes:
        row["note"] = "；".join(notes)
    return row


def telemetry(plan: Plan, point: Point | None, count: int, expected: int | None) -> list[dict[str, Any]]:
    """进行中的遥测：已收到的点数（setpoint 是预计点数）与最新一点的读数。不碰仪器。"""
    items = [{"metric": "points", "value": float(count), "setpoint": float(expected) if expected else None}]
    if point is None:
        return items
    values = point.values
    if "f_Hz" in values:
        items.append({"metric": "frequency_Hz", "value": values["f_Hz"], "setpoint": None})
    if "e_V" in values and analysis.finite(values["e_V"]):
        items.append({"metric": "potential_V", "value": values["e_V"], "setpoint": None})
    if "i_A" in values and analysis.finite(values["i_A"]):
        items.append({"metric": "current_mA", "value": values["i_A"] * 1000.0, "setpoint": None})
    return items
