"""派生指标与曲线抽稀（纯函数，只用标准库）。公式与取法写在 README「派生指标」，这里和那边一一对应。

- 体相电阻 R_b（EIS 高频端与实轴的交点）：从最高频往低频找 −Z'' 由 ≤ 0 变成 > 0 的那一对相邻点，在两点之间对 −Z''
  线性插值到 0（`zero_crossing`）；高频端没有过零（没有感抗段，阻塞电极常见）就取最高频起 |−Z''| 一路变小、到第一个
  局部最小的那个点的 Z'（`min_imag_hf`，多半就是最高频点；会比真值略大，偏大多少看那一点的 −Z''）；
- 电导率 σ（mS/cm）= 1000 × K（cm⁻¹）/ R_b（Ω），K 是电导池常数；
- 起始电位：沿扫描方向第一个 |j| ≥ 阈值的数据点的电位（不插值）；一直没到阈值是 None；
- 开路电位：最后 10% 数据点的平均（至少 1 点）；计时电流的末电流同样取最后 10% 的平均；
- 点数超过上限就把相邻的点按组取平均（x、y 都取组内平均），不挑点。
"""
from __future__ import annotations

import math
from typing import Sequence

TAIL = 0.1


def finite(*values: float) -> bool:
    return all(isinstance(value, (int, float)) and math.isfinite(value) for value in values)


def tail_mean(values: Sequence[float], fraction: float = TAIL) -> float | None:
    """最后 `fraction` 的点的平均（至少 1 点）。没有点返回 None。"""
    kept = [value for value in values if finite(value)]
    if not kept:
        return None
    count = max(1, int(math.ceil(len(kept) * fraction - 1e-9)))
    tail = kept[-count:]
    return sum(tail) / len(tail)


def r_bulk(freq: Sequence[float], z_re: Sequence[float], z_im: Sequence[float]) -> dict | None:
    """高频端与实轴的交点。返回 {"r_ohm", "method", "freq_Hz"}；点不够返回 None。`z_im` 是物理符号的虚部（容抗为负）。"""
    points = sorted(((f, re, -im) for f, re, im in zip(freq, z_re, z_im) if finite(f, re, im) and f > 0),
                    key=lambda point: -point[0])
    if len(points) < 2:
        return None
    first_positive = next((index for index, point in enumerate(points) if point[2] > 0), None)
    if first_positive is not None and first_positive > 0:
        (f1, x1, y1), (f2, x2, y2) = points[first_positive - 1], points[first_positive]
        if y1 == 0:
            return {"r_ohm": x1, "method": "zero_crossing", "freq_Hz": f1}
        r = x1 + (0 - y1) * (x2 - x1) / (y2 - y1)
        # 交点的频率按 log f 插值，只作参考
        ratio = (0 - y1) / (y2 - y1)
        f0 = 10 ** (math.log10(f1) + ratio * (math.log10(f2) - math.log10(f1)))
        return {"r_ohm": r, "method": "zero_crossing", "freq_Hz": f0}
    index = 0
    while index + 1 < len(points) and abs(points[index + 1][2]) < abs(points[index][2]):
        index += 1
    f, x, _ = points[index]
    return {"r_ohm": x, "method": "min_imag_hf", "freq_Hz": f}


def conductivity_mS_cm(cell_constant_per_cm: float, r_ohm: float) -> float | None:
    """σ（mS/cm）= 1000 × K / R。"""
    if not finite(cell_constant_per_cm, r_ohm) or r_ohm <= 0 or cell_constant_per_cm <= 0:
        return None
    return 1000.0 * cell_constant_per_cm / r_ohm


def onset(potentials: Sequence[float], currents: Sequence[float], threshold: float) -> float | None:
    """沿扫描顺序第一个 |y| ≥ 阈值的点的电位。"""
    for e, y in zip(potentials, currents):
        if finite(e, y) and abs(y) >= threshold:
            return e
    return None


def extremes(potentials: Sequence[float], currents: Sequence[float]) -> dict | None:
    """最大（氧化峰）与最小（还原峰）电流及其电位。不扣基线。"""
    pairs = [(e, i) for e, i in zip(potentials, currents) if finite(e, i)]
    if not pairs:
        return None
    high = max(pairs, key=lambda pair: pair[1])
    low = min(pairs, key=lambda pair: pair[1])
    return {"e_max_V": high[0], "y_max": high[1], "e_min_V": low[0], "y_min": low[1]}


def binned(x: Sequence[float], y: Sequence[float], limit: int) -> tuple[list[float], list[float]]:
    """点数超过 limit 就把相邻的点按组取平均（按采集顺序分组，CV 这种 x 来回走的也成立）。"""
    count = len(x)
    if count <= limit:
        return list(x), list(y)
    size = math.ceil(count / limit)
    out_x, out_y = [], []
    for start in range(0, count, size):
        group_x, group_y = x[start:start + size], y[start:start + size]
        out_x.append(sum(group_x) / len(group_x))
        out_y.append(sum(group_y) / len(group_y))
    return out_x, out_y


def rounded(values: Sequence[float], digits: int = 6) -> list[float]:
    """有效数字取到 digits 位（回执别带着 17 位的浮点尾巴）。"""
    return [float(f"{value:.{digits}g}") for value in values]
