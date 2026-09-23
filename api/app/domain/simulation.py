"""模拟设备的测量模型。仅用于模拟适配器；真实工位接入后整个模块可删除。

数值由条件水平的主效应 + 种子噪声决定，同一批次同一孔位任何时候重放出同一结果。
"""
from ..core.rng import hash_str, seeded


def measure(batch_id: str, well: str, levels: list | None, repeat: int) -> dict[str, float]:
    nxt = seeded(hash_str(batch_id + well))
    for _ in range(3):
        nxt()  # 预热，避免相近种子的首个输出相关
    factors = levels or [2, 2]
    first = float(factors[0]) if len(factors) > 0 else 2.0
    second = float(factors[1]) if len(factors) > 1 else 2.0
    drift = (repeat - 2) * 7 if (first == 4 and second == 3) else 0
    capacity = 191 + 8 * (1 + first) ** 0.5 - 2 * (second - 2) + (nxt() - 0.5) * 4 + drift
    areal_density = 15.0 + 0.15 * second + (nxt() - 0.5) * 0.5
    return {"discharge_capacity": round(capacity, 1), "areal_density": round(areal_density, 2)}


def telemetry_point(setpoint: float, seed_text: str) -> float:
    nxt = seeded(hash_str(seed_text))
    return round(setpoint * (0.985 + nxt() * 0.03), 3)


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
    """首圈充放电曲线。回传 (阶段, 比容量 mAh/g, 电压 V)，与结果页的下载入口对应。"""
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
