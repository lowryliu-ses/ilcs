"""载具位置与转运的纯规则：要不要转运、放到哪、谁来搬。

不碰数据库也不碰时钟：服务层把位置、占用、承运工位的现状装进来，这里只做判断，
并且每个「不行」都给出能照着处理的原因。
"""
from __future__ import annotations

from dataclasses import dataclass, field

NEST = "nest"
HOTEL = "hotel"
BUFFER = "buffer"
STORAGE = "storage"
KINDS = (NEST, HOTEL, BUFFER, STORAGE)


@dataclass(frozen=True)
class LocationSpec:
    id: str
    kind: str = NEST
    station_id: str = ""
    accepts: tuple[str, ...] = ()
    active: bool = True
    position: int = 0


@dataclass(frozen=True)
class CarrierSpec:
    """能执行转运（cap.transfer）的工位：AGV、机械臂、轨道小车。"""

    id: str
    usable: bool = True
    why_not: str = ""
    busy: bool = False


@dataclass
class TransferPlan:
    needed: bool
    source: str = ""
    destination: str = ""
    carrier: str = ""
    blocked: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.blocked


def accepts(location: LocationSpec, labware_kind: str) -> bool:
    return not location.accepts or labware_kind in location.accepts


def at_station(location: LocationSpec | None, station_id: str) -> bool:
    return location is not None and location.kind == NEST and location.station_id == station_id


def free_destination(
    locations: list[LocationSpec], station_id: str, labware_kind: str, occupied: set[str],
) -> LocationSpec | None:
    """目标工位上第一个空着且收这种载具的放置位。

    「空着」= 没有板在上面，也没有在途转运正要把板送过去——否则两块板会被送到同一个位置。
    """
    candidates = sorted(
        (
            location for location in locations
            if location.active and location.kind == NEST and location.station_id == station_id
            and accepts(location, labware_kind) and location.id not in occupied
        ),
        key=lambda location: (location.position, location.id),
    )
    return candidates[0] if candidates else None


def plan_transfer(
    *,
    current: LocationSpec | None,
    labware_known: bool,
    target_station_id: str,
    labware_kind: str,
    locations: list[LocationSpec],
    occupied: set[str],
    carriers: list[CarrierSpec],
    preferred_carrier: str = "",
) -> TransferPlan:
    """设备步骤开始前：板已经在目标工位上就不转运；否则找放置位和承运工位。"""
    if not labware_known or current is None:
        return TransferPlan(
            needed=True,
            blocked=["载具位置未知：先扫码确认它在哪个位置，系统不按计划推算位置"],
        )
    if at_station(current, target_station_id):
        return TransferPlan(needed=False, source=current.id, destination=current.id)
    plan = TransferPlan(needed=True, source=current.id)
    destination = free_destination(locations, target_station_id, labware_kind, occupied)
    if destination is None:
        nests = [
            location for location in locations
            if location.kind == NEST and location.station_id == target_station_id and location.active
        ]
        plan.blocked.append(
            f"{target_station_id} 没有登记放置位" if not nests
            else f"{target_station_id} 的放置位都被占用（{len(nests)} 个），或不收这种载具"
        )
    else:
        plan.destination = destination.id
    usable = [carrier for carrier in carriers if carrier.usable]
    if not usable:
        reasons = "；".join(f"{c.id}：{c.why_not}" for c in carriers if c.why_not) or "没有登记转运能力的工位"
        plan.blocked.append(f"没有可用的承运工位（{reasons}）")
    else:
        # 排程给这一步分了承运工位就用它；否则挑空闲的，都忙就挑第一个（在它自己的队列里排队）
        chosen = next((c for c in usable if c.id == preferred_carrier), None)
        chosen = chosen or next((c for c in usable if not c.busy), None) or usable[0]
        plan.carrier = chosen.id
    return plan


def manual_move_blockers(
    *, destination: LocationSpec | None, labware_kind: str, occupied_by: str, in_transit: bool,
    labware_state: str,
) -> list[str]:
    """人工放置（扫码）前的检查。"""
    blocked: list[str] = []
    if labware_state == "retired":
        blocked.append("载具已报废，不能再放入产线")
    if in_transit:
        blocked.append("载具有在途转运指令：等转运完成或先核查结果未知的转运")
    if destination is None:
        blocked.append("目标位置不存在")
        return blocked
    if not destination.active:
        blocked.append(f"{destination.id} 已停用")
    if not accepts(destination, labware_kind):
        blocked.append(f"{destination.id} 不收这种载具")
    if occupied_by:
        blocked.append(f"{destination.id} 上已有载具 {occupied_by}")
    return blocked


def container_of(batch_id: str) -> str:
    """批次的虚拟板号：孔位占用按它登记；绑定实体载具后，占用再指向那块载具（`labware_id`）。"""
    return f"PL-{batch_id.replace('B-', '')}"


def well_fits(rows: int, cols: int, well: str) -> bool:
    """孔位名（A1、H12）是否落在载具的行列里。"""
    import re

    match = re.fullmatch(r"([A-Z]+)(\d+)", (well or "").strip().upper())
    if not match:
        return False
    letters, number = match.groups()
    row = 0
    for char in letters:
        row = row * 26 + (ord(char) - 64)
    return 1 <= row <= max(1, rows) and 1 <= int(number) <= max(1, cols)


def physical_wells(rows: int, cols: int, logical: list[str]) -> dict[str, str]:
    """批次布局的孔位（逻辑位）→ 实体载具上的孔位。

    布局全部落在载具行列里就原样对应；否则（如 2×4 的布局放进 1×8 的托盘）按布局顺序逐行填进载具。
    调用方已保证载具位数够用。
    """
    if all(well_fits(rows, cols, well) for well in logical):
        return {well: well for well in logical}
    letters = [chr(65 + index) for index in range(max(1, rows))]
    slots = [f"{letter}{col}" for letter in letters for col in range(1, max(1, cols) + 1)]
    return {well: slots[index] for index, well in enumerate(logical) if index < len(slots)}
