"""参数的类型与单位、前馈参数（上游结果 → 下游设定值）的纯规则。"""
from decimal import Decimal

from app.domain.bindings import binding_issues, compute, plan_factor_issues
from app.domain.capability import StationSpec, out_of_range, station_fits
from app.domain.matrix import target_issues
from app.domain.params import canonical_unit, clean_specs, convert, convertible, spec_issues, split_ratio
from app.domain.recipe_rules import device_issues
from app.domain.steps import missing_form_values

CAPS = {
    "cap.weigh": {"name": "精密称重选片", "params": {"mass": "单片质量 g"}, "param_specs": {"mass": {"unit": "g"}}},
    "cap.assemble": {
        "name": "扣电组装", "params": {"electrolyte": "注液量 μL"}, "param_specs": {"electrolyte": {"unit": "μL"}},
    },
    "cap.test": {
        "name": "电性能测试", "params": {"rate": "倍率 C", "cycles": "循环圈数"},
        "param_specs": {"rate": {"unit": "C"}, "cycles": {"type": "integer", "required": False}},
    },
}


def _binding(**overrides) -> dict:
    binding = {
        "source_step_id": "s01", "field": "mass", "scope": "sample", "unit": "g",
        "coefficient": {"value": 3900, "unit": "μL/g"}, "expect": [40, 80],
    }
    binding.update(overrides)
    return binding


def _flow(binding: dict | None = None, **fill) -> list[dict]:
    return [
        {"step_id": "s01", "kind": "device", "name": "称重选片", "cap": "cap.weigh", "params": {"mass": 0.0152}, "dur": 5},
        {"step_id": "s02", "kind": "device", "name": "注液封口", "cap": "cap.assemble", "dur": 10,
         "params": fill.get("params", {}), "bindings": {"electrolyte": binding or _binding()}},
    ]


def test_units_normalize_and_convert_within_one_dimension():
    assert canonical_unit(" uL ") == "μL" and canonical_unit("µL") == "μL" and canonical_unit("ml") == "mL"
    assert convert(Decimal("0.0152"), "g", "mg") == Decimal("15.2000")
    assert convert(Decimal("1.5"), "mL", "μL") == Decimal("1500.0")
    assert convertible("℃", "℃") and not convertible("℃", "K"), "温度不按比例换算，只认同名单位"
    assert not convertible("g", "μL"), "不同量纲不能换算"
    assert split_ratio("μL/mg") == ("μL", "mg") and split_ratio("μL") is None


def test_compute_multiplies_coefficient_and_converts_units():
    assert compute(Decimal("0.0152"), "g", "μL", Decimal("3900"), "μL/g") == Decimal("59.280000")
    # 系数按 mg 写：来源先换成 mg 再乘
    assert compute(Decimal("0.0152"), "g", "μL", Decimal("3.9"), "μL/mg") == Decimal("59.280000")
    assert compute(Decimal("15.2"), "mg", "g") == Decimal("0.015200"), "不写系数就是纯单位换算"
    assert compute(Decimal("1"), "g", "μL") is None, "质量换不成体积，必须有系数"


def test_valid_binding_has_no_issues():
    steps = _flow()
    assert binding_issues(steps[1], steps, 1, CAPS) == []
    assert device_issues(steps[1], CAPS) == [], "取自上游结果的参数算已提供，不再要求固定值"


def test_binding_needs_upstream_source_units_coefficient_and_expected_range():
    steps = _flow(_binding(source_step_id="s02"))
    assert any("必须是本步的上游步骤" in text for text in binding_issues(steps[1], steps, 1, CAPS))

    steps = _flow(_binding(unit="", expect=None))
    issues = binding_issues(steps[1], steps, 1, CAPS)
    assert any("来源值的单位" in text for text in issues)
    assert any("预期范围" in text for text in issues)

    steps = _flow(_binding(coefficient={"value": 3900, "unit": "μL"}))
    assert any("目标单位/来源单位" in text for text in binding_issues(steps[1], steps, 1, CAPS))

    steps = _flow(_binding(coefficient=None))
    assert any("不能直接换算" in text for text in binding_issues(steps[1], steps, 1, CAPS))

    steps = _flow(_binding(coefficient={"value": 3900, "unit": "μL/g", "factor": "注液系数"}))
    assert any("二选一" in text for text in binding_issues(steps[1], steps, 1, CAPS))

    steps = _flow(_binding(coefficient={"factor": ""}))
    assert any("必须写明因子名" in text for text in binding_issues(steps[1], steps, 1, CAPS))
    steps = _flow(_binding(coefficient={"value": "", "unit": "μL/g"}))
    assert any("大于 0" in text for text in binding_issues(steps[1], steps, 1, CAPS)), "选了固定系数却没填值"


def test_binding_and_static_value_cannot_coexist_and_target_unit_must_be_registered():
    steps = _flow(params={"electrolyte": 60})
    assert any("不能再写固定值" in text for text in binding_issues(steps[1], steps, 1, CAPS))

    bare = {**CAPS, "cap.assemble": {**CAPS["cap.assemble"], "param_specs": {}}}
    steps = _flow()
    assert any("没有登记单位" in text for text in binding_issues(steps[1], steps, 1, bare))


def test_binding_expected_range_must_sit_inside_method_range():
    steps = _flow()
    steps[1]["method"] = {"params": {"electrolyte": {"min": 50, "max": 70}}}
    assert any("超出设备方法允许" in text for text in binding_issues(steps[1], steps, 1, CAPS))


def test_manual_source_must_be_numeric_and_match_scope():
    steps = [
        {"step_id": "s01", "kind": "manual", "name": "逐片称重", "dur": 5,
         "form": [{"key": "mass", "label": "极片质量", "type": "number"}]},
        _flow()[1],
    ]
    assert any("按样本录入" in text for text in binding_issues(steps[1], steps, 1, CAPS))
    steps[0]["form"][0]["per_sample"] = True
    assert binding_issues(steps[1], steps, 1, CAPS) == []
    steps[0]["form"][0]["type"] = "text"
    assert any("必须是数值字段" in text for text in binding_issues(steps[1], steps, 1, CAPS))


def test_station_matching_uses_expected_range_for_bound_parameters():
    step = _flow()[1]
    wide = StationSpec(id="ST-06", limits={"cap.assemble": {"electrolyte": [10, 200]}})
    narrow = StationSpec(id="ST-09", limits={"cap.assemble": {"electrolyte": [10, 60]}})
    assert station_fits(wide, step)
    assert not station_fits(narrow, step), "预期上限 80 超出 60：排程时值还不知道，按最坏情况挑工位"
    assert any("预期范围" in text for text in out_of_range(narrow, step))


def test_param_specs_allow_optional_and_integer_parameters():
    step = {"cap": "cap.test", "params": {"rate": 0.5}}
    assert device_issues(step, CAPS) == [], "cycles 标了非必填，可以不写"
    step["params"]["cycles"] = 2.5
    assert any("必须是整数" in text for text in device_issues(step, CAPS))
    step["params"].pop("rate")
    assert any("倍率 C 未填写" in text for text in device_issues(step, CAPS))


def test_spec_validation_and_cleaning():
    params = {"electrolyte": "注液量"}
    assert any("不是本能力的参数" in text for text in spec_issues(params, {"volume": {"unit": "mL"}}))
    assert any("不受支持" in text for text in spec_issues(params, {"electrolyte": {"type": "text"}}))
    assert clean_specs(params, {"electrolyte": {"type": "number", "unit": "uL", "required": True}, "gone": {}}) == {
        "electrolyte": {"unit": "μL"}
    }


def test_per_sample_form_field_requires_every_active_sample():
    step = {"form": [{"key": "mass", "label": "极片质量", "type": "number", "per_sample": True}]}
    samples = {"S-1": "A1 S-1", "S-2": "A2 S-2"}
    assert missing_form_values(step, {"mass": {"S-1": 15.1, "S-2": 15.3}}, samples) == []
    problems = missing_form_values(step, {"mass": {"S-1": 15.1}}, samples)
    assert any("A2 S-2" in text for text in problems)
    problems = missing_form_values(step, {"mass": {"S-1": 15.1, "S-2": 15.3, "S-9": 1}}, samples)
    assert any("S-9" in text and "不是本批次" in text for text in problems)
    assert any("要按样本" in text for text in missing_form_values(step, {"mass": 15.1}, samples))


def test_factor_target_units_and_bound_parameter_conflicts():
    steps = _flow()
    stations = [StationSpec(id="ST-06", limits={"cap.assemble": {"electrolyte": [10, 200]}})]
    factors = [{"name": "注液量", "unit": "μL", "levels": [50, 60], "target": {"step_id": "s02", "param": "electrolyte"}}]
    assert any("取自上游结果" in text for text in target_issues(factors, steps, stations, CAPS))

    plain = [{**steps[1], "bindings": {}, "params": {"electrolyte": 60}}]
    plain[0]["step_id"] = "s02"
    factors[0]["unit"] = "mL"
    assert any("单位 mL" in text for text in target_issues(factors, plain, stations, CAPS))
    factors[0]["unit"] = " μL"
    assert target_issues(factors, plain, stations, CAPS) == []


def test_plan_factor_coefficient_checks():
    steps = _flow(_binding(coefficient={"factor": "注液系数"}))
    assert any("没有这个因子" in text for text in plan_factor_issues(steps, [], CAPS))
    factors = [{"name": "注液系数", "unit": "μL/g", "levels": [3800, 4000]}]
    assert plan_factor_issues(steps, factors, CAPS) == []
    factors[0]["levels"] = [0, 4000]
    assert any("大于 0" in text for text in plan_factor_issues(steps, factors, CAPS))
    factors[0].update(levels=[3800, 4000], unit="μL")
    assert any("目标单位/来源单位" in text for text in plan_factor_issues(steps, factors, CAPS))
