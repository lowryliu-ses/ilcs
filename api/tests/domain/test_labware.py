"""载具转运计划的纯规则。"""
from app.domain.labware import CarrierSpec, LocationSpec, manual_move_blockers, plan_transfer

HOTEL = LocationSpec(id="H/S1", kind="hotel")
NESTS = [
    LocationSpec(id="ST-A/N1", kind="nest", station_id="ST-A", position=1),
    LocationSpec(id="ST-A/N2", kind="nest", station_id="ST-A", position=2, accepts=("plate",)),
    LocationSpec(id="ST-B/N1", kind="nest", station_id="ST-B", position=1),
]
AGV = [CarrierSpec(id="AGV-1"), CarrierSpec(id="AGV-2", busy=True)]


def plan(**overrides):
    args = dict(
        current=HOTEL, labware_known=True, target_station_id="ST-A", labware_kind="tray",
        locations=[HOTEL, *NESTS], occupied=set(), carriers=AGV,
    )
    args.update(overrides)
    return plan_transfer(**args)


def test_labware_already_on_the_station_needs_no_transfer():
    result = plan(current=NESTS[0])
    assert result.needed is False and result.ok


def test_picks_first_free_nest_that_accepts_the_labware_kind_and_an_idle_carrier():
    result = plan(occupied={"ST-A/N1"})
    assert result.blocked == ["ST-A 的放置位都被占用（2 个），或不收这种载具"], "N2 只收板，不收托盘"
    result = plan()
    assert (result.destination, result.carrier) == ("ST-A/N1", "AGV-1")


def test_scheduled_carrier_is_preferred_even_when_busy():
    assert plan(preferred_carrier="AGV-2").carrier == "AGV-2"


def test_unknown_location_is_never_guessed():
    result = plan(current=None, labware_known=False)
    assert not result.ok and "位置未知" in result.blocked[0]


def test_missing_nest_and_carrier_are_both_reported():
    result = plan(target_station_id="ST-X", carriers=[CarrierSpec(id="AGV-1", usable=False, why_not="失联")])
    assert result.blocked == ["ST-X 没有登记放置位", "没有可用的承运工位（AGV-1：失联）"]


def test_manual_move_blockers():
    assert manual_move_blockers(
        destination=NESTS[1], labware_kind="tray", occupied_by="P-1", in_transit=True, labware_state="idle",
    ) == ["载具有在途转运指令：等转运完成或先核查结果未知的转运", "ST-A/N2 不收这种载具", "ST-A/N2 上已有载具 P-1"]
    assert manual_move_blockers(
        destination=NESTS[0], labware_kind="tray", occupied_by="", in_transit=False, labware_state="idle",
    ) == []
