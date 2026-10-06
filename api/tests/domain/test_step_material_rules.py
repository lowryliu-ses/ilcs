"""步骤级投料物料的纯规则：字段校验、BOM 检查、方案给出的预留量、内置模拟的消耗与示意检测值。"""
from app.adapters.base import CommandRequest
from app.adapters.drivers.simulation import SimulationAdapter, delivered_quantity, sample_output
from app.domain.dataquality import output_flags
from app.domain.recipe_rules import recipe_checks, step_issues
from app.services.batch_service import BatchService

CAPABILITIES = {
    "cap.dose": {
        "name": "称量加注", "params": {"mass": "质量 g", "speed": "速度"},
        "param_specs": {"mass": {"unit": "g"}},
    },
}


def dose(**overrides) -> dict:
    return {
        "name": "加注", "cap": "cap.dose", "params": {"mass": 1, "speed": 2}, "dur": 5,
        "consumes_materials": True, "material": "EC", **overrides,
    }


def bom_row(steps, bom):
    return next(row for row in recipe_checks(steps, [], bom, "") if row["key"] == "bom")


def test_material_field_messages():
    assert step_issues(dose(), CAPABILITIES) == []
    assert "物料名称必须是非空文字" in step_issues(dose(material=""), CAPABILITIES)
    assert "物料名称必须是非空文字" in step_issues(dose(material=3), CAPABILITIES)
    assert "声明了投料物料，但没有勾选「消耗物料」" in step_issues(dose(consumes_materials=False), CAPABILITIES)
    assert "用量参数 volume 不是该能力的参数" in step_issues(dose(material_param="volume"), CAPABILITIES)
    assert "用量参数 speed 没有登记单位，无法与物料单位对账" in step_issues(dose(material_param="speed"), CAPABILITIES)
    assert step_issues(dose(material_param="mass"), CAPABILITIES) == []
    manual = {"kind": "manual", "name": "人工投料", "dur": 5, "consumes_materials": True, "material": "EC",
              "material_param": "mass", "form": [{"key": "ok", "label": "确认", "type": "bool"}]}
    assert step_issues(manual, CAPABILITIES) == ["只有设备步骤可以指定用量参数"]
    wait = {"kind": "wait", "name": "静置", "dur": 5, "wait_for": {"mode": "duration"}, "material": "EC"}
    assert "声明了投料物料，但没有勾选「消耗物料」" in step_issues(wait, CAPABILITIES)


def test_bom_check_three_cases():
    assert bom_row([dose(consumes_materials=False, material=None)], [])["ok"]
    declared = bom_row([dose(), dose(material="EMC")], [])
    assert declared["ok"] and declared["detail"] == "EC、EMC 的用量由实验方案按样本给出"
    plain = dose()
    plain.pop("material")
    missing = bom_row([dose(), plain], [])
    assert not missing["ok"] and missing["detail"] == "存在消耗物料的步骤但未定义 BOM，排程前无法预留"
    outside = bom_row([dose(), dose(material="EMC")], [{"material": "EC", "qty": 1, "unit": "g"}])
    assert outside["ok"] and outside["detail"] == "EC 1g；EMC 不在 BOM 里，用量由实验方案给出"


def test_plan_materials_sum_levels_for_materials_not_in_bom():
    def aimed(step_id):
        return {"step_id": step_id, "param": "mass"}

    factors = [
        {"name": "EC", "levels": [1.1, 2.2], "target": aimed("s01"), "material": {"name": "EC", "unit": "g", "per": 1}},
        {"name": "LP57", "levels": [1, 2], "target": aimed("s02"),
         "material": {"name": "LP57", "unit": "mL", "per": 1}},
        {"name": "温度", "levels": [20, 30]},
        {"name": "LiBF4", "levels": [0, 0], "target": aimed("s03"),
         "material": {"name": "LiBF4", "unit": "g", "per": 1}},
    ]
    factors.append({"name": "导电剂比例", "levels": [1, 2], "material": {"name": "Super P", "unit": "g", "per": 0.9}})
    rows = [{"levels": [1.1, 1, 20, 0, 1]}, {"levels": [2.2, 2, 30, 0, 2]}, {"levels": [2.2, 1, 20, 0, 1]}]
    steps = [dose(step_id="s01"), dose(step_id="s02", material="LP57"), dose(step_id="s03", material="LiBF4")]
    extra = BatchService.plan_materials(
        factors, rows, [{"material": "LP57", "qty": 1, "unit": "mL"}], steps, CAPABILITIES,
    )
    # LP57 流程 BOM 已列，照旧按 BOM；LiBF4 全为 0 不预留；Super P 没有步骤声明投它，只是计划页估算
    assert extra == [{"material": "EC", "qty": "5.500000", "unit": "g", "source": "plan"}]


def _request(**overrides) -> CommandRequest:
    base = dict(command_id="cmd-1", station_id="ST-X", capability="cap.dose", params={"mass": 1},
                type="dispatch", batch_id="B-1", step_index=0)
    return CommandRequest(**{**base, **overrides})


def test_simulation_reports_material_as_sum_over_wells_and_skips_zero():
    params = {"mass": 9, "wells": {"A1": {"mass": 10.6632}, "A2": {"mass": 0}, "A3": {}}}
    assert str(delivered_quantity(params, "mass")) == "19.663200", "没写该参数的孔用顶层值"
    adapter = SimulationAdapter("ST-X")
    material = {"name": "EC", "unit": "g", "param": "mass"}
    done = adapter.submit(_request(params=params, material=material))
    assert done.delivered["materials"] == [{"material": "EC", "unit": "g", "quantity": 19.6632}]
    assert done.delivered["wells"] == params["wells"], "仍原样回显参数"
    zero = adapter.submit(_request(command_id="cmd-2", params={"mass": 0}, material=material))
    assert "materials" not in zero.delivered, "0 用量不回报"
    plain = adapter.submit(_request(command_id="cmd-3"))
    assert plain.delivered == {"mass": 1}, "没声明物料就只回显参数"


def test_simulated_outputs_only_when_enabled_and_never_overwrite_params():
    outputs = (
        {"key": "mass", "label": "质量", "lo": 0, "hi": 100},
        {"key": "conductivity", "label": "电导率", "lo": 0.1, "hi": 30, "required": True},
        {"key": "density", "label": "密度", "lo": 0.8, "required": True},
        {"key": "note_value", "label": "无界"},
    )
    params = {"mass": 1, "wells": {"A1": {"mass": 1}, "B1": {"mass": 2}}}
    off = SimulationAdapter("ST-X").submit(_request(params=params, outputs=outputs))
    assert "conductivity" not in off.delivered, "缺省关闭：只回显参数"
    assert {f["code"] for f in output_flags(list(outputs), off.delivered)} == {"output_missing"}

    on = SimulationAdapter("ST-X", config={"simulate_outputs": True}).submit(_request(params=params, outputs=outputs))
    delivered = on.delivered
    assert delivered["mass"] == 1 and delivered["wells"]["A1"]["mass"] == 1, "绝不覆盖回显的参数"
    for well in ("A1", "B1"):
        assert 0.1 + 29.9 * 0.35 <= delivered["wells"][well]["conductivity"] <= 0.1 + 29.9 * 0.65
        assert delivered["wells"][well]["density"] == 0.84
    assert delivered["conductivity"] == round(
        (delivered["wells"]["A1"]["conductivity"] + delivered["wells"]["B1"]["conductivity"]) / 2, 4,
    )
    assert delivered["note_value"] == 1.0
    assert output_flags(list(outputs), delivered) == []
    again = SimulationAdapter("ST-Y", config={"simulate_outputs": True}).submit(_request(params=params, outputs=outputs))
    assert again.delivered == delivered, "示意值按指令 + 检测项 + 孔位确定性生成"
    assert sample_output({"hi": 10}, "x") == 9.5 and sample_output({"lo": 0}, "x") == 1.0


TASK_CAPABILITIES = {
    "cap.run": {
        "name": "整线任务", "params": {"m01": "加料位 1", "m02": "加料位 2", "m03": "加料位 3", "temp": "温度"},
        "param_specs": {"m01": {"unit": "g", "required": False}, "m02": {"unit": "g", "required": False},
                        "m03": {"unit": "g", "required": False}, "temp": {"unit": "℃"}},
    },
}


def task_step(**overrides) -> dict:
    """一步投几种料：整线一个任务把一瓶的全部组分按顺序投完。"""
    return {
        "name": "整线任务", "cap": "cap.run", "params": {"temp": 25, "m01": 0, "m02": 0}, "dur": 120,
        "consumes_materials": True,
        "materials": [{"material": "EC", "param": "m01"}, {"material": "EMC", "param": "m02"}], **overrides,
    }


def test_several_materials_in_one_step_field_messages():
    from app.domain.steps import step_doses, step_materials

    assert step_issues(task_step(), TASK_CAPABILITIES) == []
    assert step_doses(task_step()) == [("EC", "m01"), ("EMC", "m02")]
    assert step_materials(task_step()) == ["EC", "EMC"] and step_materials(task_step(consumes_materials=False)) == []
    assert step_materials(dose()) == ["EC"], "投一种料的照旧"

    def issues(**overrides):
        return step_issues(task_step(**overrides), TASK_CAPABILITIES)

    assert "materials 要写成" in issues(materials=[])[0]
    assert "一步投几种料用 materials，不能同时写 material / material_param" in issues(material="EC")
    assert "物料 EC 在 materials 里出现了两次" in issues(
        materials=[{"material": "EC", "param": "m01"}, {"material": "EC", "param": "m02"}])
    assert "用量参数 m01 被两种料共用" in issues(
        materials=[{"material": "EC", "param": "m01"}, {"material": "EMC", "param": "m01"}])
    assert "materials 里 EMC 要写用量参数 param" in issues(
        materials=[{"material": "EC", "param": "m01"}, {"material": "EMC"}])
    assert "EMC 的用量参数 m09 不是该能力的参数" in issues(
        materials=[{"material": "EC", "param": "m01"}, {"material": "EMC", "param": "m09"}])
    assert "声明了投料物料，但没有勾选「消耗物料」" in issues(consumes_materials=False)
    manual = {"kind": "manual", "name": "人工", "dur": 5, "consumes_materials": True,
              "materials": [{"material": "EC", "param": "m01"}], "form": [{"key": "ok", "label": "确认", "type": "bool"}]}
    assert step_issues(manual, TASK_CAPABILITIES) == ["只有设备步骤能一步投几种料（materials）"]


def test_bom_check_and_plan_materials_with_several_materials_in_one_step():
    declared = bom_row([task_step()], [])
    assert declared["ok"] and declared["detail"] == "EC、EMC 的用量由实验方案按样本给出"

    def aimed(param):
        return {"step_id": "s01", "param": param}

    factors = [
        {"name": "EC", "levels": [10, 12], "target": aimed("m01"), "material": {"name": "EC", "unit": "g", "per": 1}},
        {"name": "EMC", "levels": [20, 0], "target": aimed("m02"), "material": {"name": "EMC", "unit": "g", "per": 1}},
    ]
    rows = [{"levels": [10, 20]}, {"levels": [12, 0]}, {"levels": [12, 20]}]
    extra = BatchService.plan_materials(factors, rows, [], [{**task_step(), "step_id": "s01"}], TASK_CAPABILITIES)
    assert extra == [
        {"material": "EC", "qty": "34.000000", "unit": "g", "source": "plan"},
        {"material": "EMC", "qty": "40.000000", "unit": "g", "source": "plan"},
    ], "每种料按自己的用量参数各算一份"


def test_simulation_reports_each_material_of_a_several_material_step():
    adapter = SimulationAdapter("ST-X")
    params = {"m01": 0, "m02": 0, "wells": {"A1": {"m01": 12.5, "m02": 20}, "A2": {"m01": 11, "m02": 0}}}
    materials = ({"name": "EC", "unit": "g", "param": "m01"}, {"name": "EMC", "unit": "g", "param": "m02"},
                 {"name": "VC", "unit": "g", "param": "m03"})
    done = adapter.submit(_request(params=params, materials=materials))
    assert done.delivered["materials"] == [
        {"material": "EC", "unit": "g", "quantity": 23.5}, {"material": "EMC", "unit": "g", "quantity": 20.0},
    ], "逐种回报各孔之和，0 用量的料不回报"
