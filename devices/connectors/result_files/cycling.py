"""充放电数据：逐点记录 → 每圈的充 / 放电容量、能量、库伦效率 → 整个测试的汇总。纯函数，不依赖 pandas。

逐点记录的列名按 NewareNDA（BSD-3）读 Neware .nda / .ndax 的输出：`Cycle`、`Step`（递增的工步序号）、`Status`、
`Voltage`（V）、`Current(mA)`、`Charge_Capacity(mAh)`、`Discharge_Capacity(mAh)`、`Charge_Energy(mWh)`、
`Discharge_Energy(mWh)`。容量与能量**每个工步从 0 重新累计**，所以一圈的容量 = 这圈各工步的最大值之和
（不看工步类型：不是充电的工步里充电容量本来就是 0）。

汇总（`summary`）：有放电的圈数、首圈充 / 放电容量与首效、基准圈与终圈的放电容量、最大放电容量、平均库伦效率、
容量保持率（终圈 ÷ 基准圈；没跑完的末圈不算）。给了活性物质质量（mg）再算比容量（mAh/g）。
"""
from __future__ import annotations

from collections import OrderedDict
from typing import Any, Iterable

CHARGE = "Charge_Capacity(mAh)"
DISCHARGE = "Discharge_Capacity(mAh)"
CHARGE_ENERGY = "Charge_Energy(mWh)"
DISCHARGE_ENERGY = "Discharge_Energy(mWh)"


def _number(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if number == number else 0.0  # NaN 当 0


def cycles(records: Iterable[dict[str, Any]]) -> list[dict[str, float]]:
    """每圈一行：cycle、charge_mAh、discharge_mAh、charge_mWh、discharge_mWh、ce_pct（放 ÷ 充 × 100，没充电的圈为空）。
    充放电都是 0 的圈（只有静置）不列。"""
    steps: "OrderedDict[tuple[int, int], dict[str, float]]" = OrderedDict()
    for row in records:
        key = (int(_number(row.get("Cycle"))), int(_number(row.get("Step"))))
        step = steps.setdefault(key, {"charge": 0.0, "discharge": 0.0, "charge_e": 0.0, "discharge_e": 0.0})
        step["charge"] = max(step["charge"], _number(row.get(CHARGE)))
        step["discharge"] = max(step["discharge"], _number(row.get(DISCHARGE)))
        step["charge_e"] = max(step["charge_e"], _number(row.get(CHARGE_ENERGY)))
        step["discharge_e"] = max(step["discharge_e"], _number(row.get(DISCHARGE_ENERGY)))
    totals: "OrderedDict[int, dict[str, float]]" = OrderedDict()
    for (cycle, _), step in steps.items():
        total = totals.setdefault(cycle, {"charge": 0.0, "discharge": 0.0, "charge_e": 0.0, "discharge_e": 0.0})
        for name, value in step.items():
            total[name] += value
    rows = []
    for cycle in sorted(totals):
        total = totals[cycle]
        if total["charge"] <= 0 and total["discharge"] <= 0:
            continue
        rows.append({
            "cycle": cycle, "charge_mAh": round(total["charge"], 6), "discharge_mAh": round(total["discharge"], 6),
            "charge_mWh": round(total["charge_e"], 6), "discharge_mWh": round(total["discharge_e"], 6),
            "ce_pct": round(total["discharge"] / total["charge"] * 100, 4) if total["charge"] > 0 else None,
        })
    return rows


def summary(rows: list[dict[str, Any]], *, active_mass_mg: float | None = None, reference_cycle: int | None = None,
            incomplete_ratio: float = 0.5) -> dict[str, float | None]:
    """整个测试的汇总。取不到的项是 None（不当 0）。

    - 首圈（first_*）：第一个有容量的圈，首效 = 它的放电 ÷ 充电。
    - 基准圈（reference_*）：算保持率的分母。缺省取第一个充、放电都有的圈（开头只有半圈放电的不算）；可以指定圈号。
    - 末圈：`last_*` 是文件里最后一个有放电的圈，原样给；它的放电容量不到前一圈的 `incomplete_ratio`（缺省一半）
      时当作没跑完（测试中途停了、还在跑），`last_cycle_partial` = 1，不进保持率与平均库伦效率。
    - 终圈（final_*）：去掉没跑完的末圈后的最后一圈；保持率 = 终圈放电 ÷ 基准圈放电 × 100。
    - 平均库伦效率：基准圈之后、终圈为止的各圈。
    给了活性物质质量（mg）再算比容量（`*_mAh_g`）。
    """
    discharged = [row for row in rows if row["discharge_mAh"] > 0]
    complete = list(discharged)
    partial = bool(incomplete_ratio) and len(complete) >= 2 and \
        complete[-1]["discharge_mAh"] < incomplete_ratio * complete[-2]["discharge_mAh"]
    if partial:
        complete = complete[:-1]
    if reference_cycle is not None:
        reference = next((row for row in rows if row["cycle"] == int(reference_cycle)), None)
    else:
        reference = next((row for row in rows if row["charge_mAh"] > 0 and row["discharge_mAh"] > 0), None) \
            or (discharged[0] if discharged else None)
    final = complete[-1] if complete else None
    first = rows[0] if rows else None
    last = discharged[-1] if discharged else None
    efficiencies = [row["ce_pct"] for row in complete
                    if reference is not None and row["cycle"] > reference["cycle"] and row.get("ce_pct") is not None]
    ratio = None
    if reference is not None and final is not None and reference["discharge_mAh"] > 0:
        ratio = round(final["discharge_mAh"] / reference["discharge_mAh"] * 100, 4)
    values: dict[str, float | None] = {
        "cycle_count": float(len(discharged)),
        "first_charge_mAh": first["charge_mAh"] if first else None,
        "first_discharge_mAh": first["discharge_mAh"] if first else None,
        "first_ce_pct": first.get("ce_pct") if first else None,
        "reference_cycle": float(reference["cycle"]) if reference else None,
        "reference_discharge_mAh": reference["discharge_mAh"] if reference else None,
        "last_cycle": float(last["cycle"]) if last else None,
        "last_discharge_mAh": last["discharge_mAh"] if last else None,
        "last_cycle_partial": 1.0 if partial else 0.0,
        "final_cycle": float(final["cycle"]) if final else None,
        "final_discharge_mAh": final["discharge_mAh"] if final else None,
        "final_ce_pct": final.get("ce_pct") if final else None,
        "max_discharge_mAh": max((row["discharge_mAh"] for row in discharged), default=None),
        "mean_ce_pct": round(sum(efficiencies) / len(efficiencies), 4) if efficiencies else None,
        "retention_pct": ratio,
    }
    if active_mass_mg and active_mass_mg > 0:
        grams = active_mass_mg / 1000
        for key in ("first_charge_mAh", "first_discharge_mAh", "reference_discharge_mAh", "last_discharge_mAh",
                    "final_discharge_mAh", "max_discharge_mAh"):
            value = values[key]
            values[key.replace("_mAh", "_mAh_g")] = round(value / grams, 4) if value is not None else None
    return values


def select(records: Iterable[dict[str, Any]], *, cycles_wanted: Iterable[int] | None = None,
           statuses: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """取一部分逐点记录画曲线：指定的圈、指定的工步类型（如只要 CC_DChg）。"""
    wanted = {int(value) for value in cycles_wanted} if cycles_wanted else None
    kinds = set(statuses) if statuses else None
    return [row for row in records
            if (wanted is None or int(_number(row.get("Cycle"))) in wanted)
            and (kinds is None or str(row.get("Status")) in kinds)]


def read_neware(path: str) -> tuple[list[dict[str, Any]], float | None]:
    """读 Neware .nda / .ndax：返回 (逐点记录, 文件里登记的活性物质质量 mg)。要装 NewareNDA（pip install NewareNDA）。"""
    try:
        import NewareNDA  # noqa: N813  包名就是这样
    except ImportError as exc:
        raise RuntimeError("读 Neware .nda / .ndax 要装 NewareNDA：pip install NewareNDA==2026.6.11") from exc
    frame = NewareNDA.read(path)
    records = frame.to_dict("records")
    mass = None
    try:
        metadata = NewareNDA.read_metadata(path)
        raw = metadata.get("active_mass_mg") if isinstance(metadata, dict) else None
        mass = float(raw) if raw not in (None, "") and float(raw) > 0 else None
    except Exception:  # noqa: BLE001  元数据读不到不影响容量本身（.ndax 的元数据按内嵌文件名分组，没有这一项）
        mass = None
    return records, mass
