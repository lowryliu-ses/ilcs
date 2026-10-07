"""内置模拟用的确定性数据：同一种子任何时候重放出同一结果。

- `telemetry_series`：内置模拟适配器与执行器给模拟步骤补的过程遥测；
- `raw_curve`：历史固定三指标批次的模拟充放电曲线下载（`/samples/{id}/raw`）。
"""
from ..core.rng import hash_str, seeded


def telemetry_series(setpoint: float, seed_text: str, count: int) -> list[float]:
    """一步之内的采样序列：先爬坡到设定值再带噪声保持。真实适配器接入后由设备流替代。"""
    nxt = seeded(hash_str(seed_text))
    ramp = max(1, count // 4)
    values = []
    for index in range(count):
        approach = min(1.0, (index + 1) / ramp)
        drift = (nxt() - 0.5) * 0.03
        values.append(round(setpoint * (0.35 + 0.65 * approach) * (1 + drift), 3))
    return values


def raw_curve(sample_id: str, capacity: float, points: int = 60) -> list[tuple[str, float, float]]:
    """首圈充放电曲线。回传 (阶段, 比容量 mAh/g, 电压 V)，供历史批次的模拟原始曲线下载。"""
    nxt = seeded(hash_str(sample_id + "raw"))
    rows: list[tuple[str, float, float]] = []
    for index in range(points + 1):
        fraction = index / points
        charge = 3.55 + 0.75 * fraction ** 1.6 + (nxt() - 0.5) * 0.01
        rows.append(("charge", round(capacity * fraction, 2), round(charge, 4)))
    for index in range(points + 1):
        fraction = index / points
        discharge = 4.25 - 1.45 * fraction ** 2.2 + (nxt() - 0.5) * 0.01
        rows.append(("discharge", round(capacity * fraction, 2), round(discharge, 4)))
    return rows
