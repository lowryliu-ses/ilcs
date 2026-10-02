"""模拟设备共用的「物理世界」：秤盘上的东西、Quantos 的加样头、注射泵各端口的储液。

假天平、假 Quantos、假注射泵各说各的协议（`sics_server.py`、`cavro_server.py`），状态都在这里：
泵从出液口推出去的液体按密度落到秤盘上，Quantos 加的粉也落到秤盘上，天平读的就是秤盘。
读数变化之后 `settle_sec` 秒内不稳定（MT-SICS 的 `S` 要等稳定、`SI` 报动态）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
import random
import threading
import time


@dataclass
class Reservoir:
    material: str
    density: float  # g/mL
    volume_ul: float = 500_000.0


@dataclass
class Head:
    substance: str
    lot: str = "SIM-LOT-01"
    remaining_doses: int = 999
    content_g: float = 50.0


@dataclass
class World:
    settle_sec: float = 0.2
    capacity_g: float = 220.0
    vessel_g: float = 25.0  # 秤上放着一个空西林瓶
    seed: int = 7
    pan_g: float = 0.0
    tare_g: float = 0.0
    changed_at: float = field(default_factory=time.monotonic)
    reservoirs: dict[int, Reservoir] = field(default_factory=dict)
    heads: list[Head] = field(default_factory=list)
    mounted: int | None = None
    door_open: bool = False
    lock: threading.RLock = field(default_factory=threading.RLock)

    def __post_init__(self) -> None:
        self.pan_g = self.vessel_g
        self.random = random.Random(self.seed)

    # ---------- 秤盘 ----------

    def add(self, grams: float) -> None:
        with self.lock:
            self.pan_g += grams
            self.changed_at = time.monotonic()

    def net(self) -> float:
        with self.lock:
            return self.pan_g - self.tare_g

    def stable(self) -> bool:
        return time.monotonic() - self.changed_at >= self.settle_sec

    def tare(self) -> float:
        with self.lock:
            self.tare_g = self.pan_g
            return self.tare_g

    def zero(self) -> None:
        with self.lock:
            self.tare_g = self.pan_g

    def place_vessel(self, grams: float | None = None) -> None:
        """换一个空瓶（测试与模拟验收用）：秤盘上只剩这个瓶子，皮重清零。"""
        with self.lock:
            self.pan_g = self.vessel_g if grams is None else grams
            self.tare_g = 0.0
            self.changed_at = time.monotonic()

    # ---------- 加样头 ----------

    def head(self) -> Head | None:
        with self.lock:
            return self.heads[self.mounted] if self.mounted is not None else None

    def mount(self, substance: str) -> None:
        with self.lock:
            index = next((i for i, head in enumerate(self.heads) if head.substance == substance), None)
            if index is None:
                self.heads.append(Head(substance))
                index = len(self.heads) - 1
            self.mounted = index
