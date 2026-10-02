"""谱图处理（纯函数，只用标准库）：几张谱取平均 → 波长换算成拉曼位移 → 裁到位移范围 → 点数超了合并相邻像素 → 取整。

输出是 ILCS 曲线型检测值的写法 `{"x": [...], "y": [...]}`（api/app/domain/series.py）：x 是拉曼位移（cm-1，严格递增），
y 是计数。不在这里做基线扣除、宇宙射线去除、强度校正——回报的是光谱仪读到的计数（见 README「还没做」）。
"""
from __future__ import annotations

import math


class SpectrumError(Exception):
    """谱图不成立（波长标定不单调、像素数对不上、范围里没有像素、读数不是有限数）。"""


def average(scans: list[list[float]]) -> list[float]:
    """逐像素取平均。"""
    if not scans:
        raise SpectrumError("没有采到谱")
    width = len(scans[0])
    if any(len(scan) != width for scan in scans):
        raise SpectrumError("几张谱的像素数不一样")
    return [sum(column) / len(scans) for column in zip(*scans)]


def raman_shift(wavelengths_nm: list[float], laser_nm: float) -> list[float]:
    """拉曼位移（cm-1）= 1e7 / 激光波长 − 1e7 / 像素波长（nm）。"""
    if any(not math.isfinite(value) or value <= 0 for value in wavelengths_nm):
        raise SpectrumError("光谱仪报的波长里有非正数或非有限数：核对光谱仪的波长标定")
    base = 1e7 / laser_nm
    return [base - 1e7 / value for value in wavelengths_nm]


def ascending(x: list[float], y: list[float]) -> tuple[list[float], list[float]]:
    """按 x 严格递增排好：倒序的翻过来；不单调（波长标定坏了）就报错，不悄悄重排。"""
    if len(x) != len(y):
        raise SpectrumError(f"波长有 {len(x)} 个像素、计数有 {len(y)} 个，对不上")
    pairs = list(zip(x, x[1:]))
    if all(right > left for left, right in pairs):
        return list(x), list(y)
    if all(right < left for left, right in pairs):
        return list(reversed(x)), list(reversed(y))
    raise SpectrumError("光谱仪报的波长不单调：核对光谱仪 EEPROM 里的波长标定系数")


def crop(x: list[float], y: list[float], low: float, high: float) -> tuple[list[float], list[float]]:
    kept = [(a, b) for a, b in zip(x, y) if low <= a <= high]
    return [a for a, _ in kept], [b for _, b in kept]


def coverage(wavelengths_nm: list[float], laser_nm: float, low: float, high: float) -> int:
    """光谱仪的像素里有几个落在位移范围内（开始采谱前核对激光波长与范围配得对不对）。"""
    return sum(1 for value in raman_shift(wavelengths_nm, laser_nm) if low <= value <= high)


def binned(x: list[float], y: list[float], limit: int) -> tuple[list[float], list[float]]:
    """点数超过 limit 就把相邻像素按组取平均（x、y 都取组内平均）。谱图用合并而不是挑点：
    挑点（如 LTTB）专挑极值，会把噪声当峰留下来；合并保留峰面积与噪声统计。"""
    count = len(x)
    if count <= limit:
        return list(x), list(y)
    size = math.ceil(count / limit)
    xs, ys = [], []
    for start in range(0, count, size):
        group_x, group_y = x[start:start + size], y[start:start + size]
        xs.append(sum(group_x) / len(group_x))
        ys.append(sum(group_y) / len(group_y))
    return xs, ys


def process(wavelengths_nm: list[float], counts: list[float], *, laser_nm: float, shift_range: tuple[float, float],
            max_points: int) -> dict[str, list[float]]:
    """一张（平均过的）谱 → `{"x": 拉曼位移 cm-1, "y": 计数}`，x 严格递增、落在范围内、点数不超过 max_points。"""
    if any(not math.isfinite(value) for value in counts):
        raise SpectrumError("光谱仪报的计数里有非有限数（NaN / 无穷）")
    x, y = ascending(raman_shift(wavelengths_nm, laser_nm), list(counts))
    low, high = shift_range
    x, y = crop(x, y, low, high)
    if len(x) < 2:
        raise SpectrumError(f"拉曼位移 {low:g}–{high:g} cm-1 里只有 {len(x)} 个像素：核对激光波长与 shift_range_cm1")
    x, y = binned(x, y, max_points)
    # x 取到 0.01 cm-1、y 取到 0.1 计数，远小于像素间距与噪声；取整后再核一次严格递增（极密的像素可能重合）
    xs, ys = [round(x[0], 2)], [round(y[0], 1)]
    for a, b in zip(x[1:], y[1:]):
        a = round(a, 2)
        if a > xs[-1]:
            xs.append(a)
            ys.append(round(b, 1))
    return {"x": xs, "y": ys}
