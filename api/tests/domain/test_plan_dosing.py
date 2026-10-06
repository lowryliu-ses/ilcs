"""方案给出用量的物料：锁定检查与建批次预留认同一个因子；人工步骤的料只能进 BOM；本批合计为 0 不拦开跑。"""
from app.domain import preflight
from app.domain.plan_dosing import dosing_factors, plan_dosed_steps
from app.domain.recipe_rules import manual_materials_outside_bom, recipe_checks
from app.services.batch_service import BatchService

CAPABILITIES = {
    "cap.dose": {
        "name": "称量加注", "params": {"mass": "质量 g", "rate": "速率 g/min", "speed": "转速"},
        "param_specs": {"mass": {"unit": "g"}, "rate": {"unit": "g/min"}},
    },
    "cap.twin": {
        "name": "双通道加注", "params": {"a": "A 路", "b": "B 路"},
        "param_specs": {"a": {"unit": "g"}, "b": {"unit": "g"}},
    },
}
MANUAL_TAIL = "人工步骤的用量只能按 BOM 预留，请把它加进 BOM"


def dose(step_id="s01", material="VC", cap="cap.dose", **extra) -> dict:
    return {"step_id": step_id, "kind": "device", "name": f"加 {material}", "cap": cap, "params": {},
            "consumes_materials": True, "material": material, "dur": 5, **extra}


def factor(name="VC 用量", material="VC", step_id="s01", param="mass", per=1, unit="g", levels=(0.5, 1.0)) -> dict:
    spec = {"name": material, "unit": unit}
    if per is not None:
        spec["per"] = per
    out = {"name": name, "levels": list(levels), "material": spec}
    if step_id is not None:
        out["target"] = {"step_id": step_id, "param": param}
    return out


def problem(steps, factors, bom=None) -> str:
    rows = plan_dosed_steps(steps, bom or [], factors, CAPABILITIES)
    return "；".join(row.problem for row in rows if row.factor is None)


def test_factor_must_target_the_dosing_param_with_positive_per():
    steps = [dose()]
    assert problem(steps, [factor()]) == ""
    assert dosing_factors(steps, [], [factor()], CAPABILITIES)[0].material == "VC"

    # per 清空（编辑器存成 0）或没写：检查不再放过，因为建批次会算出 0
    assert "per）没填或不大于 0" in problem(steps, [factor(per=0)])
    assert "per）没填或不大于 0" in problem(steps, [factor(per=None)])
    # 作用在同一步的别的参数上：下发的用量参数没变，预留却按它算
    assert problem(steps, [factor(param="rate")]) == "第 1 步「加 VC」投 VC：因子「VC 用量」作用在 rate，这一步的用量取 mass"
    assert "作用在 rate，这一步的用量取 mass" in problem([dose(material_param="mass")], [factor(param="rate")])
    # 没写物料单位
    assert "没写物料单位" in problem(steps, [factor(unit="")])
    # 按单位推断不出用量参数（两个 g 参数）：要求在步骤里指定
    assert "推断不出" in problem([dose(cap="cap.twin")], [factor(param="a")])
    assert problem([dose(cap="cap.twin", material_param="a")], [factor(param="a")]) == ""
    # 两个合格因子给同一步
    assert "有 2 个因子都给出它的用量" in problem(steps, [factor(), factor(name="VC 二")])
    # 没作用参数的因子只是估算，既不算覆盖、也不预留
    assert "方案里也没有给出 VC 用量的因子" in problem(steps, [factor(step_id=None)])


def test_plan_materials_reserves_only_the_dosing_factor():
    steps = [dose()]
    rows = [{"levels": [0.5, 2]}, {"levels": [1.0, 3]}]
    # 第二个因子也带 VC、但没有作用参数（比例估算）：以前会把它的水平 × per 也加进预留
    factors = [factor(), factor(name="VC 比例", step_id=None, per=10, levels=(2, 3))]
    assert BatchService.plan_materials(factors, rows, [], steps, CAPABILITIES) == [
        {"material": "VC", "qty": "1.500000", "unit": "g", "source": "plan"},
    ]
    # 检查不通过的因子（per 为 0 / 参数不对）一律不预留：检查与预留同一口径
    assert BatchService.plan_materials([factor(per=0)], rows, [], steps, CAPABILITIES) == []
    assert BatchService.plan_materials([factor(param="rate")], rows, [], steps, CAPABILITIES) == []


def test_manual_and_subflow_steps_cannot_take_amounts_from_the_plan():
    manual = {"step_id": "s02", "kind": "manual", "name": "手工补加 VC", "consumes_materials": True,
              "material": "VC", "dur": 5}
    assert "第 1 步「手工补加 VC」是人工步骤" in problem([manual], [factor(step_id="s02")])
    inner = {**dose(step_id="s03.s01"), "groups": [{"step_id": "s03", "name": "配液子流程"}]}
    text = problem([inner], [])
    assert text.startswith("子流程「配液子流程」里的「加 VC」投 VC") and "被引用流程的 BOM 里列出 VC" in text
    # 合并 BOM 列了它就按 BOM 走，不算方案给量
    assert plan_dosed_steps([inner], [{"material": "VC", "qty": 1, "unit": "g"}], [], CAPABILITIES) == []


def _bom(steps, bom):
    return next(row for row in recipe_checks(steps, [], bom, "") if row["key"] == "bom")


def test_bom_check_rejects_manual_material_outside_bom():
    manual = {"step_id": "s02", "kind": "manual", "name": "手工补加 VC", "consumes_materials": True,
              "material": "VC", "dur": 5}
    other = {**manual, "step_id": "s03", "name": "手工补加 FEC", "material": "FEC"}
    expected = f"人工步骤「手工补加 VC」投的 VC 不在 BOM 里：{MANUAL_TAIL}"
    # 空 BOM：以前说「用量由实验方案按样本给出」并放行
    row = _bom([dose(material="EC"), manual], [])
    assert not row["ok"] and row["detail"] == expected
    # 非空 BOM 同样拦；多个人工步骤用「；」连接
    row = _bom([manual, other], [{"material": "EC", "qty": 1, "unit": "g"}])
    assert not row["ok"]
    assert row["detail"] == f"{expected}；人工步骤「手工补加 FEC」投的 FEC 不在 BOM 里：{MANUAL_TAIL}"
    # 列进 BOM 就通过；设备步骤的料不在 BOM 里仍按方案给量
    assert _bom([manual, dose(material="EC")], [{"material": "VC", "qty": 1, "unit": "g"}])["ok"]
    assert manual_materials_outside_bom([manual], [{"material": "VC", "qty": 1, "unit": "g"}]) == ""


def _context(**extra):
    return preflight.PreflightContext(
        recipe_state="released", recipe_risk="RA", snapshot_version="1.0.0", released_version="1.0.0",
        steps_total=1, steps_needing_station=1, steps_allocated=1, **extra,
    )


def test_preflight_zero_plan_materials_are_not_applicable():
    row = next(check for check in preflight.evaluate(_context(material_steps=0, zero_plan_materials=["VC"]))
               if check.key == "material")
    assert row.state == preflight.NOT_APPLICABLE
    assert "VC 由方案给出用量，本批合计为 0" in row.detail
    blocked = next(
        check for check in preflight.evaluate(_context(material_steps=1)) if check.key == "material"
    )
    assert blocked.state == preflight.BLOCKED


def test_each_material_of_a_several_material_step_needs_its_own_factor():
    """一步投几种料（整线任务）：每种料各认一个因子，作用在这种料写明的用量参数上；说明里点明是哪种料。"""
    capabilities = {"cap.run": {"name": "整线任务", "params": {"m01": "加料位 1", "m02": "加料位 2"},
                                "param_specs": {"m01": {"unit": "g"}, "m02": {"unit": "g"}}}}
    step = {"step_id": "s01", "kind": "device", "name": "整线任务", "cap": "cap.run", "params": {}, "dur": 60,
            "consumes_materials": True,
            "materials": [{"material": "EC", "param": "m01"}, {"material": "EMC", "param": "m02"}]}
    ec = factor(name="EC 用量", material="EC", param="m01")
    emc = factor(name="EMC 用量", material="EMC", param="m02")
    rows = plan_dosed_steps([step], [], [ec, emc], capabilities)
    assert [(row.material, row.factor) for row in rows] == [("EC", 0), ("EMC", 1)]
    assert set(dosing_factors([step], [], [ec, emc], capabilities)) == {0, 1}

    missing = plan_dosed_steps([step], [], [ec], capabilities)
    assert missing[1].factor is None and "方案里也没有给出 EMC 用量的因子" in missing[1].problem
    wrong = plan_dosed_steps([step], [], [ec, factor(name="EMC 用量", material="EMC", param="m01")], capabilities)
    assert "作用在 m01，这一步 EMC 的用量取 m02" in wrong[1].problem
    # BOM 已列的料不由因子给量
    listed = plan_dosed_steps([step], [{"material": "EMC", "qty": 5, "unit": "g"}], [ec], capabilities)
    assert [row.material for row in listed] == ["EC"]
