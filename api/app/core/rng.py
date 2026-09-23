"""可复现随机数。孔位随机化必须在任意进程、任意时刻重放出同一结果。"""
from collections.abc import Callable


def seeded(seed: int) -> Callable[[], float]:
    state = seed & 0xFFFFFFFF or 1

    def nxt() -> float:
        nonlocal state
        state = (state * 1664525 + 1013904223) & 0xFFFFFFFF
        return state / 4294967296

    return nxt


def hash_str(text: str) -> int:
    value = 7
    for char in str(text):
        value = (value * 31 + ord(char)) & 0xFFFFFFFF
    return value
