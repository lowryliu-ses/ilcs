"""资源许可判据：校准、维护预约、容量与退役。

对每个设备步骤预计使用的完整时间区间校验，不只看「今天」——第二个步骤的校准
可能正好在它的执行窗口里失效。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime

    def overlaps(self, other: "Window") -> bool:
        return self.start < other.end and self.end > other.start


@dataclass(frozen=True)
class CalibrationSpec:
    effective_from: datetime
    expires_at: datetime | None
    result: str = "pass"
    capability_scope: tuple[str, ...] = ()

    def covers(self, capability_id: str) -> bool:
        return not self.capability_scope or capability_id in self.capability_scope

    def valid_over(self, window: Window) -> bool:
        """整段执行窗口都要在有效期内。窗口内失效就不算许可。"""
        if self.result != "pass":
            return False
        if self.effective_from > window.start:
            return False
        if self.expires_at is not None and self.expires_at < window.end:
            return False
        return True


@dataclass(frozen=True)
class AssetSpec:
    asset_id: str
    name: str
    state: str = "active"
    capacity: int = 1
    calibration_applicable: bool = True
    calibration_exempt_reason: str = ""
    calibrations: tuple[CalibrationSpec, ...] = ()
    bookings: tuple[Window, ...] = ()


def governing_calibration(
    asset: AssetSpec, capability_id: str, at: datetime
) -> CalibrationSpec | None:
    """在某一时刻起作用的校准记录：生效不晚于该时刻的最近一条。

    后登记的不合格结论压过更早的合格记录——仪器在周期中复校不合格，旧证书就不再作数。
    同一时刻既有合格又有不合格时按不合格处理。
    """
    prior = [c for c in asset.calibrations if c.covers(capability_id) and c.effective_from <= at]
    if not prior:
        return None
    return max(prior, key=lambda c: (c.effective_from, c.result != "pass"))


def calibration_blockers(asset: AssetSpec, capability_id: str, window: Window) -> list[str]:
    if asset.state == "retired":
        return [f"{asset.name} 已退役"]
    if asset.state == "maintenance":
        return [f"{asset.name} 处于维护状态"]
    if not asset.calibration_applicable:
        # 明确「不适用校准」才放行，并要求写明理由；缺失不等同不适用
        if not asset.calibration_exempt_reason.strip():
            return [f"{asset.name} 标记为不适用校准但没有写明理由，视为缺失"]
        return []
    relevant = [c for c in asset.calibrations if c.covers(capability_id)]
    if not relevant:
        # 资产列表这类场合并不针对某个能力提问，此时不要拼出「没有覆盖能力 的校准记录」
        # 这种断句——缺的是有效校准本身。
        return [
            f"{asset.name} 没有覆盖能力 {capability_id} 的校准记录" if capability_id
            else f"{asset.name} 没有有效的校准记录"
        ]
    failing_inside = [
        c for c in relevant
        if c.result != "pass" and window.start < c.effective_from < window.end
    ]
    if failing_inside:
        return [
            f"{asset.name} 在该步骤执行区间内登记了不合格校准"
            f"（{failing_inside[0].effective_from.isoformat(timespec='minutes')}）"
        ]
    latest = governing_calibration(asset, capability_id, window.start)
    if latest is None:
        return [f"{asset.name} 的校准未覆盖该步骤的完整执行区间"]
    if latest.result != "pass":
        return [f"{asset.name} 最近一次校准结果不合格"]
    if latest.expires_at is None:
        return [f"{asset.name} 最近一次合格校准没有有效期，不能作为许可依据"]
    if latest.expires_at < window.end:
        return [
            f"{asset.name} 的校准在 {latest.expires_at.isoformat(timespec='minutes')} 到期，"
            f"早于该步骤的计划结束时间 {window.end.isoformat(timespec='minutes')}"
        ]
    return []


def booking_blockers(asset: AssetSpec, window: Window) -> list[str]:
    """容量按资产算。同一资产映射的不同工位不能各占一份。"""
    overlapping = [b for b in asset.bookings if b.overlaps(window)]
    if len(overlapping) >= max(1, asset.capacity):
        return [
            f"{asset.name} 在 {window.start.isoformat(timespec='minutes')} 起的区间已有 "
            f"{len(overlapping)} 个占用，超出资产容量 {asset.capacity}"
        ]
    return []


@dataclass
class StepResourceCheck:
    step_index: int
    step_id: str
    step_name: str
    applicable: bool
    ok: bool
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "step_index": self.step_index,
            "step_id": self.step_id,
            "step_name": self.step_name,
            "applicable": self.applicable,
            "ok": self.ok,
            "reasons": self.reasons,
        }


def evaluate_steps(
    steps: list[dict],
    windows: dict[int, Window],
    station_assets: dict[str, AssetSpec],
    station_of_step: dict[int, str],
) -> list[StepResourceCheck]:
    from .steps import kind_of, needs_station, step_id_of

    rows: list[StepResourceCheck] = []
    for index, step in enumerate(steps or []):
        step_id = step_id_of(step, index)
        name = step.get("name") or f"第 {index + 1} 步"
        if not needs_station(step):
            rows.append(
                StepResourceCheck(
                    index, step_id, name, applicable=False, ok=True,
                    reasons=[f"{kind_of(step)} 步骤无需占用设备资源"],
                )
            )
            continue
        station_id = station_of_step.get(index, "")
        asset = station_assets.get(station_id)
        window = windows.get(index)
        if not station_id or window is None:
            rows.append(
                StepResourceCheck(index, step_id, name, True, False, ["该步骤还没有工位与时间窗"])
            )
            continue
        if asset is None:
            rows.append(
                StepResourceCheck(
                    index, step_id, name, True, False,
                    [f"工位 {station_id} 没有关联资产档案，无法校验校准与容量"],
                )
            )
            continue
        reasons = calibration_blockers(asset, step.get("cap") or "", window)
        reasons += booking_blockers(asset, window)
        rows.append(StepResourceCheck(index, step_id, name, True, not reasons, reasons))
    return rows
