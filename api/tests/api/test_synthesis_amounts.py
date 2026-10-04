"""合成用量：方案按 mmol / 当量给，物料登记摩尔质量，建批次换成设备收的 g，预留与消耗按换算后的量对账。"""
from uuid import uuid4

from test_graph_workflow import _dispatch, _run
from test_step_materials import CAPACITY, _approve, _checks, _dose, _lot, _released


def _material(admin, name: str, conversions: dict) -> dict:
    created = admin.post("/api/materials", {"code": f"{name}@g", "name": name, "base_unit": "g", "conversions": conversions})
    assert created.status_code == 201, created.text
    return created.json()


def test_mmol_and_equivalent_factors_are_dosed_in_grams(admin, researcher, qa, operator, reset_runtime, executor):
    uid = uuid4().hex[:6]
    substrate, base = f"底物-{uid}", f"碱-{uid}"
    _material(admin, substrate, {"mmol": "0.1529"})  # 152.9 g/mol
    base_row = _material(admin, base, {})  # 先不登记摩尔质量：方案检查要说清楚缺什么
    _lot(operator, qa, substrate)
    _lot(operator, qa, base)
    recipe_id = _released(researcher, qa, [
        _dose("s01", "底物称量", substrate, material_param="mass"),
        {**_dose("s02", "碱称量", base, material_param="mass"), "after": ["s01"]},
    ])
    plan = researcher.post("/api/plans", {
        "name": "偶联反应筛选", "recipe_id": recipe_id, "plan_type": "matrix", "repeats": 1,
        "factors": [
            {"name": "底物", "unit": "mmol", "levels": [1.0, 2.0], "target": {"step_id": "s01", "param": "mass"},
             "material": {"name": substrate, "unit": "g", "per": 1}},
            {"name": "碱", "unit": "eq", "levels": [1.2, 1.5], "basis": {"factor": "底物"},
             "target": {"step_id": "s02", "param": "mass"}, "material": {"name": base, "unit": "g", "per": 1}},
        ],
        "design_points": [[1.0, 1.2], [2.0, 1.5]], "required_metrics": [CAPACITY],
    })
    assert plan.status_code == 201, plan.text
    plan_id = plan.json()["id"]
    targets = _checks(researcher, plan_id)["targets"]
    assert not targets["ok"] and "没有登记 mmol 与 g 之间的换算" in targets["detail"], targets

    patched = admin.patch(f"/api/materials/{base_row['id']}", {"conversions": {"mmol": "0.1"}, "row_version": base_row["row_version"]})
    assert patched.status_code == 200, patched.text
    assert _checks(researcher, plan_id)["targets"]["ok"]
    _approve(researcher, qa, plan_id)

    created = operator.post("/api/batches", {"plan_id": plan_id})
    assert created.status_code == 201, created.text
    batch_id = created.json()["id"]
    detail = operator.get(f"/api/batches/{batch_id}").json()
    reserved = {row["material"]: row["qty"] for row in detail["reservations"]}
    # 底物 (1 + 2) mmol × 0.1529 g；碱 1.2 × 1 + 1.5 × 2 = 4.2 mmol × 0.1 g
    assert reserved == {substrate: "0.458700", base: "0.420000"}, reserved
    by_group = {row["condition_group"]: row for row in detail["samples"]}
    assert by_group["C01"]["levels"] == [1.0, 1.2], "样本上记的仍是化学家写的水平"

    from app.core.db import SessionLocal
    from app.models import Batch

    with SessionLocal() as session:
        snapshot = session.get(Batch, batch_id).plan_snapshot
    dose = snapshot["factors"][1]["dose"]
    assert dose["from"] == "eq" and dose["basis"] == 0 and dose["ratio"] == "0.1"
    wells = snapshot["condition_params"]
    grams = sorted(round(row["mass"], 4) for row in wells["s01"].values())
    assert grams == [0.1529, 0.3058] and sorted(round(row["mass"], 4) for row in wells["s02"].values()) == [0.12, 0.3]

    _dispatch(operator, batch_id)
    done = _run(operator, batch_id, executor)
    assert done["state"] == "done", done["failure_reason"]
    consumed = {row["material"]: row["consumed_qty"] for row in done["reservations"]}
    assert consumed == {substrate: "0.458700", base: "0.420000"}, "下发换算后的量，模拟回报的消耗与预留一致"


def _bottle_plan(researcher, recipe_id: str, material: str, bottles: list[str], policy: str) -> str:
    plan = researcher.post("/api/plans", {
        "name": f"接续 {policy}", "recipe_id": recipe_id, "plan_type": "matrix", "repeats": 1,
        "factors": [{"name": "加料", "unit": "g", "levels": [0.2, 0.3], "target": {"step_id": "s01", "param": "mass"},
                     "material": {"name": material, "unit": "g", "per": 1}}],
        "design_points": [[0.2], [0.3]], "sample_ids": bottles, "sample_policy": policy, "required_metrics": [CAPACITY],
    })
    assert plan.status_code == 201, plan.text
    assert plan.json()["sample_policy"] == policy
    return plan.json()["id"]


def test_continue_policy_lets_a_next_step_reuse_finished_products(admin, researcher, qa, operator, reset_runtime, executor):
    """多步合成：上一批的产物接着做下一步。缺省一瓶一配方照旧挡住；声明「接着用」后允许已跑完的样本，没跑完的仍挡住。"""
    from test_step_materials import _sample

    uid = uuid4().hex[:6]
    reagent = f"试剂-{uid}"
    _lot(operator, qa, reagent)
    recipe_id = _released(researcher, qa, [_dose("s01", "加料", reagent, material_param="mass")])
    bottles = [_sample(operator), _sample(operator)]
    first = _bottle_plan(researcher, recipe_id, reagent, bottles, "fresh")
    _approve(researcher, qa, first)
    batch_id = operator.post("/api/batches", {"plan_id": first}).json()["id"]

    # 第一批还没跑：接着用也不行（同一时刻只在一处）
    waiting = _bottle_plan(researcher, recipe_id, reagent, bottles, "continue")
    _approve(researcher, qa, waiting)
    busy = operator.post("/api/batches", {"plan_id": waiting})
    assert busy.status_code == 409 and busy.json()["detail"]["code"] == "sample_in_use" and "没跑完" in busy.text

    _dispatch(operator, batch_id)
    assert _run(operator, batch_id, executor)["state"] == "done"
    strict = _bottle_plan(researcher, recipe_id, reagent, bottles, "fresh")
    _approve(researcher, qa, strict)
    refused = operator.post("/api/batches", {"plan_id": strict})
    assert refused.status_code == 409 and "方案要求样本只用一次" in refused.text, "缺省仍是样本只用一次"
    reused = operator.post("/api/batches", {"plan_id": waiting})
    assert reused.status_code == 201, reused.text
    samples = operator.get(f"/api/batches/{reused.json()['id']}").json()["samples"]
    assert sorted(row["physical_sample_id"] for row in samples) == sorted(bottles), "新批次的样本就是上一批的产物"
