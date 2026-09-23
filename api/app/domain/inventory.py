"""库存核算的纯算术。

三个量分开算，不能互相替代：
- 账面库存 balance：组织仍持有且未消耗的总量，包含已领用未消耗部分。
- 未耗用占用 outstanding = 授权预留 − 已消耗 − 已核销损耗 − 已释放。
- 可用量 available = 账面库存 − 全部未耗用占用。

领用与归还只改保管位置，不动账面库存也不动占用总量；实际消耗或损耗才扣账面库存，
并同额减少占用。库存与占用都不能为负。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

ZERO = Decimal("0.000000")
QUANTUM = Decimal("0.000001")

# 不影响账面库存的事件：只改占用或保管位置
BALANCE_NEUTRAL = {"reserve", "issue", "return", "release"}
# 减少账面库存的事件
BALANCE_DOWN = {"consume", "loss"}
# 增加账面库存的事件
BALANCE_UP = {"receive"}
EVENT_TYPES = BALANCE_NEUTRAL | BALANCE_DOWN | BALANCE_UP | {"adjust", "reverse"}

EVENT_NAMES = {
    "receive": "入库", "reserve": "预留", "issue": "领用", "consume": "消耗",
    "loss": "损耗核销", "return": "归还", "release": "释放", "adjust": "盘点调整",
    "reverse": "冲正",
}


class QuantityError(ValueError):
    pass


def q(value) -> Decimal:
    """把请求里的字符串/数字统一成 6 位小数。

    走字符串构造 Decimal，不经过 float：浮点误差正是账实差额被掩盖的入口。
    """
    if value is None:
        return ZERO
    if isinstance(value, float):
        # 明确拒绝二进制浮点：请求里请用字符串或整数写数量
        value = repr(value)
    try:
        return Decimal(str(value)).quantize(QUANTUM)
    except (InvalidOperation, ArithmeticError) as exc:
        raise QuantityError(f"数量 {value!r} 不是合法的十进制数") from exc


def positive(value, label: str = "数量") -> Decimal:
    amount = q(value)
    if amount <= ZERO:
        raise QuantityError(f"{label}必须大于 0")
    return amount


@dataclass(frozen=True)
class ReservationState:
    authorized: Decimal
    consumed: Decimal = ZERO
    loss: Decimal = ZERO
    released: Decimal = ZERO
    issued: Decimal = ZERO
    returned: Decimal = ZERO

    @property
    def outstanding(self) -> Decimal:
        """未耗用占用。"""
        value = self.authorized - self.consumed - self.loss - self.released
        return value if value > ZERO else ZERO

    @property
    def issued_outstanding(self) -> Decimal:
        """已领用但未消耗、未归还的量。终止时这部分不能直接变回可用库存。"""
        value = self.issued - self.consumed - self.loss - self.returned
        return value if value > ZERO else ZERO

    @property
    def unissued_outstanding(self) -> Decimal:
        """未领用的占用。终止只自动释放这部分。"""
        value = self.outstanding - self.issued_outstanding
        return value if value > ZERO else ZERO


def available(balance, outstanding_total) -> Decimal:
    value = q(balance) - q(outstanding_total)
    return value if value > ZERO else ZERO


def balance_delta(event_type: str, quantity: Decimal) -> Decimal:
    if event_type in BALANCE_DOWN:
        return -quantity
    if event_type in BALANCE_UP:
        return quantity
    return ZERO


def check_consume(state: ReservationState, quantity: Decimal, balance: Decimal) -> list[str]:
    """消耗前的校验。超预留要先以明确动作追加预留，不在消耗里默默放大占用。"""
    problems: list[str] = []
    if quantity > state.outstanding:
        problems.append(
            f"实际用量 {quantity:f} 超出剩余预留 {state.outstanding:f}，"
            f"请先追加预留并校验可用量"
        )
    if quantity > balance:
        problems.append(f"实际用量 {quantity:f} 超出账面库存 {balance:f}")
    return problems


def check_release(state: ReservationState, quantity: Decimal) -> list[str]:
    problems: list[str] = []
    if quantity > state.unissued_outstanding:
        problems.append(
            f"可释放 {state.unissued_outstanding:f}，已领用未归还的 "
            f"{state.issued_outstanding:f} 需要先归还或处置确认"
        )
    return problems


def check_issue(state: ReservationState, quantity: Decimal) -> list[str]:
    remaining = state.outstanding - state.issued_outstanding
    if quantity > remaining:
        return [f"可领用 {remaining:f}，本次请求 {quantity:f}"]
    return []


def check_return(state: ReservationState, quantity: Decimal) -> list[str]:
    if quantity > state.issued_outstanding:
        return [f"已领用未归还仅 {state.issued_outstanding:f}，本次归还 {quantity:f} 超出"]
    return []


def convert(quantity: Decimal, unit: str, base_unit: str, conversions: dict) -> Decimal:
    """单位换算。只接受基础单位与登记过的精确换算，不匹配一律拒绝。"""
    if unit == base_unit:
        return quantity
    factor = (conversions or {}).get(unit)
    if factor is None:
        raise QuantityError(
            f"单位 {unit} 与物料基础单位 {base_unit} 不一致，且未登记精确换算，已拒绝"
        )
    return (quantity * q(factor)).quantize(QUANTUM)


def effective_expiry(expiry: str, open_expiry: str) -> tuple[str, str]:
    """有效截止时间 = 生产有效期与适用开封有效期中的较早者。

    返回（截止日期, 依据）。开封期限没录入就只按生产有效期判，不编造天数。
    """
    candidates = [(value, label) for value, label in ((expiry, "生产有效期"), (open_expiry, "开封有效期")) if value]
    if not candidates:
        return "", "未录入有效期"
    chosen = min(candidates, key=lambda row: row[0])
    if len(candidates) == 2 and candidates[0][0] == candidates[1][0]:
        return chosen[0], "生产有效期与开封有效期同日"
    return chosen[0], chosen[1]
