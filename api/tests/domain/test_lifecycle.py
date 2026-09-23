"""删除与停用的判据。分界线只有一条：这条记录有没有被别的记录引用过。"""
from app.domain.lifecycle import (
    batch_delete_blockers,
    capability_delete_blockers,
    lot_delete_blockers,
    lot_editable_fields,
    plan_delete_blockers,
    recipe_delete_blockers,
    station_retire_blockers,
    waste_delete_blockers,
)


def test_unreferenced_draft_recipe_is_deletable():
    assert recipe_delete_blockers("draft", [], []) == []


def test_released_recipe_is_never_deletable():
    blockers = recipe_delete_blockers("released", [], [])

    assert len(blockers) == 1
    assert "退役" in blockers[0]


def test_each_non_draft_state_gets_its_own_advice():
    """评审中的配方不该被告知「请用退役」——那是已发布版本的路。"""
    assert "撤回" in recipe_delete_blockers("review", [], [])[0]
    assert "发布或退役" in recipe_delete_blockers("approved", [], [])[0]
    assert "存档" in recipe_delete_blockers("retired", [], [])[0]


def test_recipe_that_produced_batches_names_them():
    blockers = recipe_delete_blockers("draft", ["B-1", "B-2"], [])

    assert blockers == ["已被 2 个批次引用：B-1、B-2"]


def test_recipe_with_revision_children_is_blocked():
    """派生过修订就不能删：子版本的 parent 会指向一个不存在的 id。"""
    blockers = recipe_delete_blockers("draft", [], ["R-201-r1"])

    assert "已派生 1 个修订草稿" in blockers[0]


def test_locked_plan_must_be_unlocked_first():
    assert plan_delete_blockers("locked", []) == ["矩阵已锁定，请先解锁再删除"]


def test_plan_with_bound_batches_is_blocked_even_as_draft():
    blockers = plan_delete_blockers("draft", ["B-1"])

    assert blockers == ["已被 1 个批次引用：B-1"]


def test_undispatched_batch_is_deletable():
    assert batch_delete_blockers("planned", has_commands=False) == []
    assert batch_delete_blockers("scheduled", has_commands=False) == []


def test_running_batch_must_be_aborted_not_deleted():
    blockers = batch_delete_blockers("running", has_commands=True)

    assert any("终止" in b for b in blockers)


def test_a_batch_that_issued_commands_is_blocked_whatever_its_state():
    """状态可能因为对账被改写，指令台账才是设备动过的铁证。"""
    blockers = batch_delete_blockers("planned", has_commands=True)

    assert blockers == ["已向设备发过指令，删除会让指令台账失去对应批次"]


def test_capability_without_implementations_or_recipes_is_deletable():
    assert capability_delete_blockers([], []) == []


def test_capability_lists_both_kinds_of_reference():
    blockers = capability_delete_blockers(["ST-01"], ["R-201", "R-205"])

    assert len(blockers) == 2
    assert "ST-01" in blockers[0]
    assert "R-201、R-205" in blockers[1]


def test_never_reserved_lot_is_deletable():
    assert lot_delete_blockers(0, "active") == []


def test_used_lot_can_only_be_scrapped():
    blockers = lot_delete_blockers(3, "active")

    assert blockers == ["已有 3 条预留记录，只能报废不能删除"]


def test_scrapped_lot_stays_on_file():
    assert lot_delete_blockers(0, "scrapped") == ["批号已报废，记录保留以解释历史投料"]


def test_running_station_cannot_be_retired():
    blockers = station_retire_blockers("running", 0)

    assert len(blockers) == 1 and "正在执行" in blockers[0]


def test_station_with_open_allocations_cannot_be_retired():
    assert station_retire_blockers("idle", 2) == ["还有 2 个未完成的工步预约占用它"]


def test_idle_station_with_no_allocations_can_be_retired():
    assert station_retire_blockers("idle", 0) == []


def test_only_an_empty_tank_can_be_removed():
    assert waste_delete_blockers(0) == []
    assert "先换桶清空" in waste_delete_blockers(40)[0]


def test_unreleased_lot_allows_editing_quantity_and_expiry():
    fields = lot_editable_fields("待复验", "active")

    assert {"qty", "expiry", "material", "cas"} <= fields


def test_released_lot_only_allows_administrative_fields():
    fields = lot_editable_fields("已放行", "active")

    assert fields == {"storage", "sds", "compat", "opened"}
    assert "qty" not in fields, "数量进过放行判断，偏差要走盘点调整而不是直接改"


def test_scrapped_lot_is_frozen():
    assert lot_editable_fields("已放行", "scrapped") == set()
