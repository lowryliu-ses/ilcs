"""合成用量：mmol / 当量经物料登记的摩尔质量、密度、浓度换成设备单位。"""
from decimal import Decimal

from app.domain import amounts, matrix
from app.domain.params import canonical_unit, convert

SOLID = {"base_unit": "g", "conversions": {"mmol": "0.1529", "mL": "1.2"}}  # 152.9 g/mol，1.2 g/mL
SOLUTION = {"base_unit": "mL", "conversions": {"mmol": "2"}}  # 0.5 mol/L


def test_amount_units_scale_within_their_dimension():
    assert canonical_unit("umol") == "μmol" and canonical_unit("mmol") == "mmol"
    assert convert(Decimal("1.5"), "mol", "mmol") == Decimal("1500")
    assert amounts.amount_ratio("μmol") == Decimal("0.001") and amounts.amount_ratio("mg") is None


def test_material_ratio_bridges_dimensions_through_registered_conversions():
    assert amounts.material_ratio(SOLID, "mmol", "mg") == Decimal("152.9")
    assert round(amounts.material_ratio(SOLID, "mmol", "μL"), 4) == Decimal("127.4167"), "经质量再按密度换成体积"
    assert amounts.material_ratio(SOLUTION, "mmol", "μL") == Decimal("2000"), "溶液按浓度：1 mmol = 2 mL"
    assert amounts.material_ratio(None, "mL", "μL") == Decimal("1000"), "同量纲不需要物料"
    assert amounts.material_ratio(None, "mmol", "mg") is None and amounts.material_ratio({"base_unit": "g"}, "mmol", "mg") is None


def test_dose_specs_for_amounts_and_equivalents():
    factors = [
        {"name": "底物", "unit": "mmol", "levels": [0.5, 1.0], "material": {"name": "A"}},
        {"name": "碱", "unit": "eq", "levels": [1.1, 1.5], "material": {"name": "B"}, "basis": {"factor": "底物"}},
        {"name": "配体", "unit": "eq", "levels": [0.1], "material": {"name": "B"}, "basis": {"amount": 500, "unit": "μmol"}},
    ]
    materials = {"A": SOLID, "B": SOLID}
    plain, problem = amounts.dose_spec(factors[0], factors, "mg", materials)
    assert problem == "" and plain["ratio"] == "152.9" and amounts.describe(plain) == "1 mmol = 152.9 mg"
    eq, problem = amounts.dose_spec(factors[1], factors, "mg", materials)
    assert problem == "" and eq["basis"] == 0 and eq["basis_ratio"] == "1"
    fixed, problem = amounts.dose_spec(factors[2], factors, "mg", materials)
    assert problem == "" and Decimal(fixed["basis_amount"]) == Decimal("0.5")
    dosed = [{**factors[0], "dose": plain}, {**factors[1], "dose": eq}, {**factors[2], "dose": fixed}]
    assert amounts.dosed_level(dosed[0], [0.5, 1.5, 0.1], 0) == 76.45
    assert amounts.dosed_level(dosed[1], [0.5, 1.5, 0.1], 1) == 114.675, "1.5 eq × 0.5 mmol × 152.9 mg/mmol"
    assert amounts.dosed_level(dosed[2], [0.5, 1.5, 0.1], 2) == 7.645, "0.1 eq × 0.5 mmol × 152.9"
    assert amounts.dosed_level(factors[0], [0.5], 0) == 0.5, "没冻结换算的水平原样下发"


def test_unconvertible_doses_say_what_is_missing():
    factors = [{"name": "底物", "unit": "mmol", "levels": [1], "material": {"name": "A"}}]
    _, problem = amounts.dose_spec(factors[0], factors, "mg", {"A": {"base_unit": "g", "conversions": {}}})
    assert "没有登记 mmol 与 mg 之间的换算" in problem and "摩尔质量" in problem
    loose = [{"name": "碱", "unit": "eq", "levels": [1.1], "material": {"name": "A"}}]
    assert "要指明基准" in amounts.dose_spec(loose[0], loose, "mg", {"A": SOLID})[1]
    mass_basis = [{"name": "底物", "unit": "g", "levels": [1], "material": {"name": "C"}},
                  {"name": "碱", "unit": "eq", "levels": [1.1], "material": {"name": "A"}, "basis": {"factor": "底物"}}]
    assert "换不成物质的量" in amounts.dose_spec(mass_basis[1], mass_basis, "mg", {"A": SOLID, "C": {"base_unit": "g"}})[1]
    mass_basis[0]["material"]["name"] = "A"
    spec, problem = amounts.dose_spec(mass_basis[1], mass_basis, "mg", {"A": SOLID})
    assert problem == "" and round(Decimal(spec["basis_ratio"]), 3) == Decimal("6.540"), "按质量给的基准经摩尔质量换成 mmol"


def test_condition_params_and_target_checks_use_converted_values():
    steps = [{"step_id": "s01", "kind": "device", "name": "称量加料", "cap": "cap.dose_solid", "params": {"mass": 1}}]
    capabilities = {"cap.dose_solid": {"params": {"mass": "质量"}, "param_specs": {"mass": {"type": "number", "unit": "g"}}}}
    factors = [{"name": "底物", "unit": "mmol", "levels": [1.0, 2.0], "target": {"step_id": "s01", "param": "mass"},
                "material": {"name": "A", "unit": "g", "per": 1}}]
    dosed, problems = amounts.attach_doses(factors, steps, capabilities, {"A": SOLID})
    assert problems == [] and dosed[0]["dose"]["ratio"] == "0.1529"
    rows = [{"well": "A1", "levels": [1.0]}, {"well": "A2", "levels": [2.0]}]
    assert matrix.condition_params(dosed, rows) == {"s01": {"A1": {"mass": 0.1529}, "A2": {"mass": 0.3058}}}
    assert matrix.step_condition(dosed, "s01", [2.0]) == {"mass": 0.3058}
    from app.domain.capability import StationSpec

    stations = [StationSpec(id="ST-X", limits={"cap.dose_solid": {"mass": [0, 0.2]}})]
    issues = matrix.target_issues(factors, steps, stations, capabilities, {"A": SOLID})
    assert any("水平 2.0（换算后 0.3058 g）超出" in item for item in issues), issues
    assert not any("1.0（" in item for item in issues), "1 mmol = 0.1529 g 在范围内"
    missing = matrix.target_issues(factors, steps, stations, capabilities, {"A": {"base_unit": "g", "conversions": {}}})
    assert any("没有登记 mmol 与 g 之间的换算" in item for item in missing)
    demand = matrix.material_demand(dosed, 2, None)
    assert demand[0]["qty"] == round((0.1529 + 0.3058) * 2, 4), "需求按换算后的量估"
