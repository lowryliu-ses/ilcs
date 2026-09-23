"""对象的删除与停用判据。

这套系统的审计是只追加的、批次快照是只读的，所以「删除」不能一刀切。分界线只有一条：
**这条记录有没有被别的记录引用过**。没被引用的草稿是纯粹的编辑残留，删掉不丢信息；
一旦被引用——配方出过批次、批号进过投料、工位排过工步——记录本身就是某段历史的一部分，
删了会让审计链、样品谱系或物料台账出现讲不通的空洞，这时只能停用 / 退役 / 报废。

每个判据都返回「不能删的理由」列表，空列表表示可以删。理由直接回给界面，
用户看到的是「R-201 已被 3 个批次引用」而不是一句 409。
"""
from __future__ import annotations

from typing import Any

# 这些状态说明对象已经进入流程，不再是可丢弃的草稿
RECIPE_DELETABLE_STATES = {"draft"}
# 非草稿状态各自该走哪条路，提示要说得准，不能一律让人去「退役」
RECIPE_STATE_ADVICE = {
    "review": "评审中的配方请先撤回或让 QA 批准后再处理",
    "approved": "已批准的配方请先发布或退役",
    "released": "已发布的配方请用「退役」",
    "retired": "已退役的配方保留存档，不删除",
}
PLAN_DELETABLE_STATES = {"draft"}
BATCH_DELETABLE_STATES = {"planned", "scheduled"}  # 未下发：没有物理动作要撤销


def recipe_delete_blockers(state: str, batch_ids: list[str], child_ids: list[str]) -> list[str]:
    """配方草稿可删。出过批次或派生过修订的不行——批次快照会指回这个版本号。"""
    blockers = []
    if state not in RECIPE_DELETABLE_STATES:
        advice = RECIPE_STATE_ADVICE.get(state, "只有草稿可删除")
        blockers.append(f"只有草稿可删除：{advice}")
    if batch_ids:
        blockers.append(f"已被 {len(batch_ids)} 个批次引用：{'、'.join(batch_ids[:5])}")
    if child_ids:
        blockers.append(f"已派生 {len(child_ids)} 个修订草稿：{'、'.join(child_ids[:5])}")
    return blockers


def plan_delete_blockers(state: str, batch_ids: list[str]) -> list[str]:
    """计划草稿可删。已锁定的先解锁——锁定本身是一次有意的冻结，不该被删除绕过。"""
    blockers = []
    if state not in PLAN_DELETABLE_STATES:
        blockers.append("矩阵已锁定，请先解锁再删除")
    if batch_ids:
        blockers.append(f"已被 {len(batch_ids)} 个批次引用：{'、'.join(batch_ids[:5])}")
    return blockers


def batch_delete_blockers(state: str, has_commands: bool) -> list[str]:
    """未下发的批次可删，连同它占的工位时间窗与物料预留一起归还。

    已下发就不行：设备侧已经动过，检查点和指令台账是设备行为的记录，
    删掉等于声称那些动作没发生过。那条路是「终止」。
    """
    blockers = []
    if state not in BATCH_DELETABLE_STATES:
        blockers.append("只有未下发的批次可删除，已下发请用「终止」")
    if has_commands:
        blockers.append("已向设备发过指令，删除会让指令台账失去对应批次")
    return blockers


def capability_delete_blockers(station_ids: list[str], recipe_ids: list[str]) -> list[str]:
    """能力没有工位实现、也没有配方引用时才可删，否则只能停用。"""
    blockers = []
    if station_ids:
        blockers.append(f"{len(station_ids)} 个工位声明实现了它：{'、'.join(station_ids[:5])}")
    if recipe_ids:
        blockers.append(f"{len(recipe_ids)} 个配方的步骤在用它：{'、'.join(recipe_ids[:5])}")
    return blockers


def lot_delete_blockers(reservation_count: int, state: str) -> list[str]:
    """从没被预留过的批号可删（录错了重录）。用过的只能报废。"""
    blockers = []
    if reservation_count:
        blockers.append(f"已有 {reservation_count} 条预留记录，只能报废不能删除")
    if state == "scrapped":
        blockers.append("批号已报废，记录保留以解释历史投料")
    return blockers


def station_retire_blockers(status: str, active_allocations: int) -> list[str]:
    """工位一律停用不删除：历史工步分配与检查点都指向它。"""
    blockers = []
    if status == "running":
        blockers.append("工位正在执行，先等当前步骤结束或终止对应批次")
    if active_allocations:
        blockers.append(f"还有 {active_allocations} 个未完成的工步预约占用它")
    return blockers


def waste_delete_blockers(level_pct: float) -> list[str]:
    """空桶才能移除登记；有液位说明现场还有实物。"""
    return [] if level_pct <= 0 else [f"当前液位 {level_pct:g}%，先换桶清空再移除登记"]


def lot_editable_fields(release: str, state: str) -> set[str]:
    """批号可改哪些字段取决于放行状态。

    已放行的批号是质量结论的载体，数量、有效期、CAS 这些进过放行判断的字段不能再动；
    存放位置、SDS 链接这类事务性信息还可以维护。
    """
    if state == "scrapped":
        return set()
    administrative = {"storage", "sds", "compat", "opened"}
    if release == "已放行":
        return administrative
    return administrative | {"material", "cas", "type", "qty", "unit", "expiry", "ghs"}


def reject_uneditable(changes: dict[str, Any], allowed: set[str]) -> list[str]:
    return [key for key in changes if key not in allowed]
