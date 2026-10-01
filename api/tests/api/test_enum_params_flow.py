"""选项型参数的整条链：能力登记选项 → 工位允许的选项 → 流程固定值 → 方案按孔位给选项 → 指令按孔位下发文字。"""
from uuid import uuid4

from test_graph_workflow import _dispatch, _run
from test_step_materials import CAPACITY, _approve, _released


def _limits(admin, cap: str, solvent: list):
    return admin.patch("/api/stations/ST-02/limits", {
        "limits": {cap: {"temp": [0, 100], "solvent": solvent}},
        "signature_id": admin.sign("工程变更批准", target="ST-02"),
    })


def test_option_parameter_from_capability_to_command(admin, researcher, qa, operator, reset_runtime, executor):
    uid = uuid4().hex[:6]
    cap = f"cap.react_{uid}"
    created = admin.post("/api/capabilities", {
        "id": cap, "name": f"偶联反应 {uid}", "params": {"temp": "温度", "solvent": "溶剂"},
        "param_specs": {"temp": {"unit": "℃"}, "solvent": {"type": "enum", "options": ["THF", "DMF", "Toluene"]}},
        "recovery": {"pausable": False, "retryable": False}, "stations": ["ST-02"],
        "signature_id": admin.sign("能力模型变更批准", target=cap),
    })
    assert created.status_code == 201, created.text
    station = next(row for row in admin.get("/api/stations").json() if row["id"] == "ST-02")
    assert station["limits"][cap] == {"temp": [0, 100], "solvent": ["THF", "DMF", "Toluene"]}, "新能力的选项缺省全都允许"

    bad = _limits(admin, cap, ["THF", "水"])
    assert bad.status_code == 400 and "不是能力登记的选项" in bad.json()["detail"]["message"], bad.text
    assert _limits(admin, cap, [0, 50]).status_code == 400
    assert _limits(admin, cap, ["THF", "DMF"]).status_code == 200

    step = {"step_id": "s01", "kind": "device", "name": "偶联反应", "cap": cap,
            "params": {"temp": 60, "solvent": "THF"}, "dur": 5}
    recipe_id = _released(researcher, qa, [step])
    recipe = researcher.get(f"/api/recipes/{recipe_id}").json()
    assert recipe["validation"][0]["ok"] and "ST-02" in recipe["validation"][0]["fits"]

    factor = {"name": "溶剂", "unit": "", "levels": ["THF", "DMF"], "target": {"step_id": "s01", "param": "solvent"}}
    plan = researcher.post("/api/plans", {
        "name": "溶剂筛选", "recipe_id": recipe_id, "plan_type": "matrix", "repeats": 1,
        "factors": [factor], "required_metrics": [CAPACITY],
    })
    assert plan.status_code == 201, plan.text
    plan_id = plan.json()["id"]
    detail = researcher.get(f"/api/plans/{plan_id}").json()
    solvent = next(row for row in detail["target_options"][0]["params"] if row["name"] == "solvent")
    assert solvent["type"] == "enum" and solvent["options"] == ["THF", "DMF", "Toluene"]
    checks = {row["key"]: row for row in detail["checks"]}
    assert checks["targets"]["ok"], checks["targets"]

    # Toluene 是登记的选项，但 ST-02 只允许 THF、DMF：没有工位能做
    toluene = researcher.patch(f"/api/plans/{plan_id}", {
        "factors": [{**factor, "levels": ["THF", "Toluene"]}], "row_version": detail["row_version"],
    })
    assert toluene.status_code == 200, toluene.text
    targets = {row["key"]: row for row in toluene.json()["checks"]}["targets"]
    assert not targets["ok"] and "水平 Toluene 超出所有可承接" in targets["detail"], targets
    restored = researcher.patch(f"/api/plans/{plan_id}", {"factors": [factor], "row_version": toluene.json()["row_version"]})
    assert restored.status_code == 200, restored.text
    _approve(researcher, qa, plan_id)

    # 新能力要给操作员授能力资质，开跑检查才放行
    people = admin.get("/api/people").json()
    people = people["items"] if isinstance(people, dict) else people
    person = next(row for row in people if row["user_id"] == operator.user["id"])
    granted = admin.post(f"/api/people/{person['id']}/qualifications", {"scope_kind": "capability", "scope_ref": cap})
    assert granted.status_code in (200, 201), granted.text

    batch = operator.post("/api/batches", {"plan_id": plan_id})
    assert batch.status_code == 201, batch.text
    batch_id = batch.json()["id"]
    _dispatch(operator, batch_id)
    detail = _run(operator, batch_id, executor)
    assert detail["state"] == "done", detail["failure_reason"]
    delivered = detail["checkpoints"][0]["payload"]["delivered"]
    wells = {row["well"]: row["condition_label"] for row in detail["samples"]}
    assert {delivered["wells"][well]["solvent"] for well in wells} == {"THF", "DMF"}, delivered
    for well, label in wells.items():
        assert delivered["wells"][well]["solvent"] in label, "每孔下发的是它自己条件里的溶剂"
