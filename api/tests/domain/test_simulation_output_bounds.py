"""内置模拟生成的示意检测值必须落在规则自己的范围内：负的单边界、很窄很小的区间都不能被判超限。"""
import pytest

from app.adapters.base import CommandRequest
from app.adapters.drivers.simulation import SimulationAdapter, sample_output
from app.domain.dataquality import output_flags

RULES = [
    {"lo": -20}, {"hi": -5}, {"lo": 1e-5, "hi": 4e-5}, {"lo": -1e-5, "hi": -5e-6}, {"lo": 0}, {"hi": 0},
    {"lo": 5}, {"hi": 10}, {"lo": 0.1, "hi": 30}, {"lo": 3.0, "hi": 3.00001},
]


@pytest.mark.parametrize("bounds", RULES)
def test_sample_output_stays_inside_its_rule(bounds):
    rule = {"key": "t", **bounds}
    for seed in ("a", "b", "cmd-1:t:A1"):
        value = sample_output(rule, seed)
        assert output_flags([rule], {"t": value}) == [], (bounds, value)


def test_one_sided_bounds_step_inward():
    assert sample_output({"lo": -20}, "x") == -19.0
    assert sample_output({"hi": -5}, "x") == -5.25
    assert sample_output({"lo": 5}, "x") == 5.25 and sample_output({"hi": 10}, "x") == 9.5
    assert sample_output({"lo": 0}, "x") == 1.0


def test_well_mean_stays_inside_a_tiny_range():
    rule = {"key": "t", "lo": 1e-5, "hi": 4e-5}
    done = SimulationAdapter("ST-X", config={"simulate_outputs": True}).submit(CommandRequest(
        command_id="cmd-tiny", station_id="ST-X", capability="cap.x", type="dispatch", batch_id="B-1",
        step_index=0, params={"wells": {"A1": {}, "B1": {}}}, outputs=(rule,),
    ))
    assert output_flags([rule], done.delivered) == []
    assert 1e-5 <= done.delivered["t"] <= 4e-5
