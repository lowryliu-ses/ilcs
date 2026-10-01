"""程序表参数的整条链：能力登记列定义 → 工位列极限 → 流程写程序表并引用本步参数 → 方案按孔位改被引用的参数 →
指令里每个孔位收到代入后的程序表（设备不认识引用）。"""
from uuid import uuid4

from test_graph_workflow import _dispatch, _run
from test_step_materials import CAPACITY, _approve, _released

COLUMNS = [
    {"key": "mode", "label": "工步", "type": "enum", "options": ["恒流充电", "恒压充电", "恒流放电", "静置"], "required": True},
    {"key": "current", "label": "电流", "unit": "C"},
    {"key": "voltage", "label": "电压", "unit": "V"},
    {"key": "time", "label": "时长", "unit": "min"},
]
PROTOCOL = [
    {"mode": "恒流充电", "current": {"param": "rate"}, "voltage": 4.2},
    {"mode": "静置", "time": 10},
    {"mode": "恒流放电", "current": {"param": "rate"}, "voltage": 2.8},
]


def test_program_parameter_from_capability_to_command(admin, researcher, qa, operator, reset_runtime, executor):
    uid = uuid4().hex[:6]
    cap = f"cap.cycle_{uid}"
    created = admin.post("/api/capabilities", {
        "id": cap, "name": f"充放电 {uid}", "params": {"protocol": "工步", "rate": "倍率"},
        "param_specs": {"protocol": {"type": "program", "columns": COLUMNS, "max_rows": 20}, "rate": {"unit": "C"}},
        "recovery": {"pausable": False, "retryable": False}, "stations": ["ST-02"],
        "signature_id": admin.sign("能力模型变更批准", target=cap),
    })
    assert created.status_code == 201, created.text
    listed = next(row for row in admin.get("/api/capabilities").json() if row["id"] == cap)
    assert listed["param_specs"]["protocol"]["columns"][0] == {
        "key": "mode", "label": "工步", "type": "enum", "options": ["恒流充电", "恒压充电", "恒流放电", "静置"], "required": True,
    }
    station = next(row for row in admin.get("/api/stations").json() if row["id"] == "ST-02")
    assert station["limits"][cap] == {"protocol": {}, "rate": [0, 100]}, "程序表缺省不约束列"

    limited = admin.patch("/api/stations/ST-02/limits", {
        "limits": {cap: {"protocol": {"voltage": [2.5, 4.4], "time": [0, 60]}, "rate": [0.05, 2]}},
        "signature_id": admin.sign("工程变更批准", target="ST-02"),
    })
    assert limited.status_code == 200, limited.text
    wrong = admin.patch("/api/stations/ST-02/limits", {
        "limits": {cap: {"protocol": {"power": [0, 1]}}}, "signature_id": admin.sign("工程变更批准", target="ST-02"),
    })
    assert wrong.status_code == 400 and "没有列 power" in wrong.json()["detail"]["message"]

    step = {"step_id": "s01", "kind": "device", "name": "化成", "cap": cap, "dur": 5,
            "params": {"protocol": PROTOCOL, "rate": 0.1}}
    recipe_id = _released(researcher, qa, [step])
    too_hot = researcher.post("/api/recipes", {"name": f"越界 {uid}", "plate": 4}).json()["id"]
    patched = researcher.patch(f"/api/recipes/{too_hot}", {"steps": [
        {**step, "params": {"protocol": [*PROTOCOL, {"mode": "恒压充电", "voltage": 4.6}], "rate": 0.1}}]})
    row = patched.json()["validation"][0]
    assert not row["fits"] and any("第 4 行 voltage=4.6 超出 [2.5, 4.4]" in reason for reason in row["blockers"]), row

    plan = researcher.post("/api/plans", {
        "name": "倍率筛选", "recipe_id": recipe_id, "plan_type": "matrix", "repeats": 1, "required_metrics": [CAPACITY],
        "factors": [{"name": "倍率", "unit": "C", "levels": [0.1, 0.5], "target": {"step_id": "s01", "param": "rate"}}],
    })
    assert plan.status_code == 201, plan.text
    plan_id = plan.json()["id"]
    detail = researcher.get(f"/api/plans/{plan_id}").json()
    assert [row["name"] for row in detail["target_options"][0]["params"]] == ["rate"], "程序表本身不作因子"
    _approve(researcher, qa, plan_id)

    people = admin.get("/api/people").json()
    people = people["items"] if isinstance(people, dict) else people
    person = next(row for row in people if row["user_id"] == operator.user["id"])
    assert admin.post(f"/api/people/{person['id']}/qualifications",
                      {"scope_kind": "capability", "scope_ref": cap}).status_code in (200, 201)

    batch = operator.post("/api/batches", {"plan_id": plan_id})
    assert batch.status_code == 201, batch.text
    batch_id = batch.json()["id"]
    _dispatch(operator, batch_id)
    detail = _run(operator, batch_id, executor)
    assert detail["state"] == "done", detail["failure_reason"]
    delivered = detail["checkpoints"][0]["payload"]["delivered"]
    assert "param" not in str(delivered["protocol"]), "顶层程序表也代入了具体的数"
    currents = {}
    for sample in detail["samples"]:
        protocol = delivered["wells"][sample["well"]]["protocol"]
        assert protocol[0]["current"] == protocol[2]["current"] == delivered["wells"][sample["well"]]["rate"]
        currents[sample["well"]] = protocol[0]["current"]
    assert sorted(currents.values()) == [0.1, 0.5]
