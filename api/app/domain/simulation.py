"""内置模拟用的确定性数据：同一种子任何时候重放出同一结果。内置模拟适配器与执行器给模拟步骤补的过程遥测。"""
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

