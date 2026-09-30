"""选项型参数（溶剂种类、测试协议、气氛）：规格、工位极限、方法规则、流程校验、方案因子与设计点、驱动下发。"""
import pytest

from app.adapters.base import AdapterError
from app.adapters.drivers.point_map import PointMapAdapter
from app.domain import matrix
from app.domain.bindings import binding_issues
from app.domain.capability import StationSpec, out_of_range, station_fits
from app.domain.methods import MethodSpec, apply, definition_issues, step_problems
from app.domain.params import clean_specs, limit_issues, spec_issues, spec_of, value_issues, window_fits
from app.domain.recipe_rules import device_issues

REACTOR = {
    "name": "反应", "params": {"temp": "温度", "solvent": "溶剂"},
    "param_specs": {"temp": {"unit": "℃"}, "solvent": {"type": "enum", "options": ["THF", "DMF", "Toluene"]}},
}
CAPS = {"cap.react": REACTOR}
R1 = StationSpec(id="R1", limits={"cap.react": {"temp": [0, 150], "solvent": ["THF", "DMF"]}})
R2 = StationSpec(id="R2", limits={"cap.react": {"temp": [0, 80], "solvent": ["Toluene"]}})
STEP = {"step_id": "s01", "name": "偶联反应", "cap": "cap.react", "params": {"temp": 60, "solvent": "THF"}, "dur": 60}


def test_enum_specs_are_validated_and_cleaned():
    params = {"solvent": "溶剂", "temp": "温度"}
    assert spec_issues(params, {"solvent": {"type": "enum", "options": ["THF", "DMF"]}}) == []
    assert "至少要有一个选项" in spec_issues(params, {"solvent": {"type": "enum"}})[0]
    assert "重复" in spec_issues(params, {"solvent": {"type": "enum", "options": ["THF", "THF"]}})[0]
    assert "没有单位" in spec_issues(params, {"solvent": {"type": "enum", "options": ["THF"], "unit": "mL"}})[-1]
    assert "不写选项" in spec_issues(params, {"temp": {"options": ["a"]}})[0]
    assert "数值、整数或选项" in spec_issues(params, {"temp": {"type": "text"}})[0]
    cleaned = clean_specs(params, {"solvent": {"type": "enum", "options": [" THF ", "DMF", ""]}, "temp": {"unit": "°C"}})
    assert cleaned == {"solvent": {"type": "enum", "options": ["THF", "DMF"]}, "temp": {"unit": "℃"}}


def test_values_windows_and_limits():
    rule = spec_of(REACTOR, "solvent")
    assert rule["type"] == "enum" and rule["unit"] == "" and rule["options"] == ["THF", "DMF", "Toluene"]
    assert value_issues(rule, "DMF") == []
    assert "只能是 THF、DMF、Toluene 之一" in value_issues(rule, "水")[0]
    assert value_issues(rule, 3)
    assert "必须是数值" in value_issues(spec_of(REACTOR, "temp"), "六十")[0]

    assert window_fits("THF", ["THF", "DMF"]) and not window_fits("Toluene", ["THF", "DMF"])
    assert window_fits(60, [0, 150]) and not window_fits("60", [0, 150]) and not window_fits(60, ["THF", "DMF"])
    assert limit_issues(rule, ["THF"], "solvent") == []
    assert "不是能力登记的选项" in limit_issues(rule, ["THF", "水"], "solvent")[0]
    assert "要写允许的选项" in limit_issues(rule, [0, 1], "solvent")[0]
    assert "下限必须小于上限" in limit_issues(spec_of(REACTOR, "temp"), ["a", "b"], "temp")[0]


def test_station_matching_by_option():
    assert station_fits(R1, STEP) and not station_fits(R2, STEP)
    toluene = {**STEP, "params": {"temp": 60, "solvent": "Toluene"}}
    assert station_fits(R2, toluene) and not station_fits(R1, toluene)
    assert out_of_range(R2, STEP) == ["R2 solvent=THF 不在允许的选项 Toluene 内"]


def test_recipe_step_accepts_option_values_only():
    assert device_issues(STEP, CAPS) == []
    wrong = device_issues({**STEP, "params": {"temp": 60, "solvent": "乙醚"}}, CAPS)
    assert any("只能是 THF、DMF、Toluene 之一" in issue for issue in wrong)
    assert "溶剂 未填写" in device_issues({**STEP, "params": {"temp": 60}}, CAPS)
    dosing = device_issues({**STEP, "material": "底物 A", "consumes_materials": True, "material_param": "solvent"}, CAPS)
    assert "用量参数 solvent 不是数值参数，不能当投料量" in dosing


def test_method_rules_for_options():
    good = {"solvent": {"options": ["THF", "DMF"], "default": "THF"}, "temp": {"min": 20, "max": 120, "default": 60}}
    assert definition_issues("cap.react", good, [], CAPS, name="偶联") == []
    bad = definition_issues("cap.react", {"solvent": {"options": ["THF", "水"], "default": "Toluene", "min": 1}},
                            [], CAPS, name="偶联")
    assert any("不写上下限" in issue for issue in bad)
    assert any("水 不是能力登记的选项" in issue for issue in bad)
    assert any("缺省值 'Toluene' 不在允许的选项" in issue for issue in bad)

    spec = MethodSpec(id="M1", code="M-REACT", version=1, name="偶联", capability_id="cap.react", state="released",
                      params=good, outputs=[], program="REACT")
    assert step_problems({**STEP, "method": {"id": "M1"}}, spec) == []
    problems = step_problems({**STEP, "params": {"temp": 60, "solvent": "Toluene"}, "method": {"id": "M1"}}, spec)
    assert problems == ["参数 solvent=Toluene 不在设备方法允许的选项 THF、DMF 里"]
    resolved, _ = apply([{**STEP, "params": {}, "method": {"id": "M1"}}], lambda ref: spec)
    assert resolved[0]["params"] == {"solvent": "THF", "temp": 60}, "选项型的缺省值同样补进步骤"


def test_feedforward_cannot_target_an_option():
    step = {**STEP, "params": {"temp": 60},
            "bindings": {"solvent": {"source_step_id": "s00", "field": "x", "unit": "g", "expect": [1, 2]}}}
    issues = binding_issues(step, [{"step_id": "s00", "name": "称量", "cap": "cap.react", "params": {}}, step], 1, CAPS)
    assert issues == ["溶剂 是选项型参数：前馈只做「来源值 × 系数」的数值换算，不能作用于它"]


def test_factor_levels_and_design_points_for_options():
    factors = [{"name": "溶剂", "unit": "", "levels": ["THF", "Toluene"], "target": {"step_id": "s01", "param": "solvent"}}]
    assert matrix.target_issues(factors, [STEP], [R1, R2], CAPS) == []
    wrong = [{**factors[0], "levels": ["THF", "水"]}]
    assert "不是参数 偶联反应.solvent 的选项" in matrix.target_issues(wrong, [STEP], [R1, R2], CAPS)[0]
    only_r1 = matrix.target_issues([{**factors[0], "levels": ["Toluene"]}], [{**STEP, "params": {"temp": 120}}],
                                   [R1, R2], CAPS)
    assert "超出所有可承接" in only_r1[0], "Toluene 只有 R2 能做，而 R2 做不了 120 ℃"

    space = {"bounds": {"溶剂": {"options": ["THF", "DMF"]}, "温度": {"min": 20, "max": 100}}}
    names = [{"name": "溶剂"}, {"name": "温度"}]
    assert matrix.point_issues(names, [["THF", 40], ["DMF", 90]], space) == []
    issues = matrix.point_issues(names, [["Toluene", 40], [1, 120]], space)
    assert any("Toluene" in issue and "不在设计空间允许的选项" in issue for issue in issues)
    assert any("高于设计空间上限 100" in issue for issue in issues)
    assert matrix.ordered_levels(["THF", 3, "DMF", 1, "THF"]) == [1, 3, "DMF", "THF"]
    assert matrix.material_demand([{**factors[0], "material": {"name": "x", "unit": "g", "per": 1}}], 1) == []


class _Map(PointMapAdapter):
    DRIVER = "test_map"


def test_point_map_writes_option_codes():
    item = {"point": "sp_solvent", "map": {"THF": 1, "DMF": 2}}
    assert _Map._coded("solvent", item, "DMF") == 2
    assert _Map._coded("temp", "sp_temp", 60) == 60
    with pytest.raises(AdapterError) as error:
        _Map._coded("solvent", item, "Toluene")
    assert "没有在 write.solvent.map 里登记" in str(error.value)
