"""假仪器上接的电池（只用标准库）。各技术各用各的模型，不追求跨技术的物理一致：

- **EIS**：电导池（两片阻塞电极夹着电解液）——体相电阻 R_b 串联双电层的常相位元件（CPE，Nyquist 上是一根斜线），
  可选再串一个界面半圆（R_int ‖ CPE_int，扣电的 SEI / 电荷转移），再加引线电感 L：
  Z(ω) = jωL + R_b + R_int / (1 + R_int·Q_int·(jω)^α_int) + 1 / (Q_dl·(jω)^α_dl)，相对噪声 0.2%；
- **LSV / CV / CA**：Li | 电解液 | 不锈钢扣电，电极面积 `area_cm2`——
  双电层充电电流 C_dl·A·dE/dt；电解液氧化分解 j = j_ref·10^((E − E_ref)/b)，再被 j_lim 限住（缺省 4.5 V vs Li
  时 0.01 mA/cm²、每 0.25 V 涨十倍）；一个可逆氧化还原对（像二茂铁内标，E½ = 3.25 V），按半积分关系
  m(t) = M / (1 + exp(−F(E − E½)/RT))、i = d^½m/dt^½（Grünwald–Letnikov 数值半微分）算，CV 是标准的「鸭子」形，
  慢扫的 LSV 上它只有 µA/cm² 级、到不了起始电位的阈值；
- **OCP**：E(t) = E∞ + (E0 − E∞)·exp(−t/τ) + 噪声，缓慢漂移。

噪声用固定种子的随机数：同样的调用顺序得到同样的数。
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
from operator import mul
import random

F_OVER_RT = 96485.332 / (8.314462 * 298.15)  # 25 ℃，1/V


@dataclass
class CellModel:
    # EIS：电导池
    r_bulk_ohm: float = 80.0
    q_dl: float = 2e-6           # 双电层 CPE 的 Q（S·s^α）
    alpha_dl: float = 0.88
    inductance_H: float = 0.0    # 引线电感：大到一定程度高频端 −Z'' 会过零
    r_interface_ohm: float = 0.0  # 界面半圆（0 = 没有）
    q_interface: float = 1e-6
    alpha_interface: float = 0.9
    eis_noise: float = 0.002     # 相对噪声
    # 直流：Li | 电解液 | 不锈钢 扣电
    area_cm2: float = 2.01
    c_dl_F_cm2: float = 20e-6
    e_ref_V: float = 4.5         # 氧化电流到 j_ref 的电位
    j_ref_A_cm2: float = 1e-5    # = 0.01 mA/cm²
    tafel_V: float = 0.25        # 每涨十倍要的电位
    j_lim_A_cm2: float = 2e-3
    e_half_V: float = 3.25       # 可逆对的半波电位
    redox_M: float = 8e-5        # 半积分的极限值 nFAC√D·A（A·s^½）
    current_noise_A_cm2: float = 2e-9
    # 开路电位
    ocp0_V: float = 3.05
    ocp_inf_V: float = 2.98
    ocp_tau_s: float = 600.0
    ocp_noise_V: float = 1e-4
    seed: int = 7
    rng: random.Random = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.rng = random.Random(self.seed)

    # ---------- EIS ----------

    def impedance(self, f_hz: float) -> complex:
        jw = 1j * 2 * math.pi * f_hz
        z = self.inductance_H * jw + self.r_bulk_ohm + 1 / (self.q_dl * jw ** self.alpha_dl)
        if self.r_interface_ohm > 0:
            z += self.r_interface_ohm / (1 + self.r_interface_ohm * self.q_interface * jw ** self.alpha_interface)
        return z

    def measured_impedance(self, f_hz: float) -> complex:
        z = self.impedance(f_hz)
        gauss = self.rng.gauss
        return complex(z.real * (1 + gauss(0, self.eis_noise)), z.imag * (1 + gauss(0, self.eis_noise)))

    # ---------- 开路电位 ----------

    def ocp(self, t_s: float) -> float:
        return (self.ocp_inf_V + (self.ocp0_V - self.ocp_inf_V) * math.exp(-t_s / self.ocp_tau_s)
                + self.rng.gauss(0, self.ocp_noise_V))

    # ---------- 直流 ----------

    def oxidation_A(self, e_V: float) -> float:
        """电解液氧化分解电流（A），被 j_lim 限住。"""
        j = self.j_ref_A_cm2 * 10 ** ((e_V - self.e_ref_V) / self.tafel_V)
        return self.area_cm2 * j / (1 + j / self.j_lim_A_cm2)

    def redox_fraction(self, e_V: float) -> float:
        """可逆对在电极表面被氧化的比例（能斯特）。"""
        x = F_OVER_RT * (e_V - self.e_half_V)
        return 1 / (1 + math.exp(-x)) if x > -700 else 0.0

    def noise_A(self) -> float:
        return self.rng.gauss(0, self.current_noise_A_cm2 * self.area_cm2)

    def sweep(self, dt_s: float) -> "Sweep":
        return Sweep(self, dt_s)


class Sweep:
    """一次扫描（LSV / CV / CA）的电流：每一步给电位，算电流。可逆对的电流要整段历史（半微分）。"""

    def __init__(self, cell: CellModel, dt_s: float):
        self.cell = cell
        self.dt = dt_s
        self.history: list[float] = []   # m(t)，最新的在后面
        self.weights: list[float] = [1.0]
        self.previous: float | None = None

    def _weight(self, index: int) -> None:
        while len(self.weights) <= index:
            j = len(self.weights)
            self.weights.append(self.weights[-1] * (j - 1.5) / j)

    def current(self, e_V: float, *, de_dt: float | None = None) -> float:
        cell = self.cell
        if de_dt is None:
            de_dt = 0.0 if self.previous is None else (e_V - self.previous) / self.dt
        self.previous = e_V
        self.history.append(cell.redox_M * cell.redox_fraction(e_V))
        self._weight(len(self.history) - 1)
        redox = sum(map(mul, self.weights, reversed(self.history))) / math.sqrt(self.dt)
        capacitive = cell.c_dl_F_cm2 * cell.area_cm2 * de_dt
        return redox + capacitive + cell.oxidation_A(e_V) + cell.noise_A()
