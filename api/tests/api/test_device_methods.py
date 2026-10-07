"""设备方法目录：起草 → 他人发布 → 流程引用 → 建批次冻结 → 指令带程序；修订发布后旧版退役，旧引用失效。"""
import copy

from app.models import Adapter, Batch, Command, Recipe

DEFINITION = {
    "name": "120℃ 真空干燥", "capability_id": "cap.vacuum_dry", "instrument_models": ["VAC-WEIGH-12"],
    "program": "VD-120",
    "params": {"temp": {"default": 120, "min": 100, "max": 130, "unit": "℃"},
               "vacuum": {"default": 1, "min": 0.5, "max": 5, "unit": "mbar"}},
    "outputs": [{"key": "moisture_ppm", "label": "水分", "unit": "ppm", "hi": 200}],
    "dur_min": 60,
}


def _released_method(researcher, qa, definition=DEFINITION) -> dict:
    created = researcher.post("/api/device-methods", definition)
    assert created.status_code == 201, created.text
    method = created.json()
    assert method["state"] == "draft" and method["issues"] == []
    own = researcher.post(f"/api/device-methods/{method['id']}/release", {"row_version": method["row_version"]})
    assert own.status_code == 403, "研究员没有发布权限"
    released = qa.post(f"/api/device-methods/{method['id']}/release", {"row_version": method["row_version"]})
    assert released.status_code == 200, released.text
    return released.json()


def test_method_is_frozen_into_the_batch_and_travels_with_the_command(researcher, qa, operator, db, reset_runtime, executor):
    method = _released_method(researcher, qa)
    recipe = db.get(Recipe, "R-205")
    original = copy.deepcopy(recipe.steps)
    try:
        steps = copy.deepcopy(original)
        steps[0] = {**steps[0], "params": {"temp": 110}, "method": {"id": method["id"]}}
        recipe.steps = steps
        db.commit()

        detail = researcher.get("/api/recipes/R-205").json()
        first = detail["validation"][0]
        assert first["ok"], first["blockers"]
        assert first["params"] == {"temp": 110, "vacuum": 1.0} and first["fits"] == ["ST-05"]

        created = operator.post("/api/batches", {"plan_id": "EP-205-01"})
        assert created.status_code == 201, created.text
        batch_id = created.json()["id"]
        db.expire_all()
        frozen = db.get(Batch, batch_id).recipe_snapshot["steps"][0]
        assert frozen["method"]["program"] == "VD-120" and frozen["params"]["vacuum"] == 1.0

        assert operator.post(f"/api/batches/{batch_id}/schedule", {}).status_code == 200
        dispatched = operator.post(
            f"/api/batches/{batch_id}/dispatch",
            {"manual_review": True, "signature_id": operator.sign("批准执行", target=batch_id)},
        )
        assert dispatched.status_code == 200, dispatched.text
        db.expire_all()
        command = db.query(Command).filter(Command.batch_id == batch_id, Command.type == "dispatch").first()
        assert command.method["code"] == method["code"] and command.method["program"] == "VD-120"

        # 修订并发布 v2：v1 退役，引用 v1 的流程不能再建批次
        draft = researcher.post(f"/api/device-methods/{method['id']}/revise").json()
        assert draft["version"] == 2 and draft["state"] == "draft"
        v2 = qa.post(f"/api/device-methods/{draft['id']}/release", {"row_version": draft["row_version"]})
        assert v2.status_code == 200, v2.text
        assert researcher.get(f"/api/device-methods/{method['id']}").json()["state"] == "retired"
        stale = researcher.get("/api/recipes/R-205").json()["validation"][0]
        assert not stale["ok"] and any("请改引用 v2" in issue for issue in stale["issues"])
        refused = operator.post("/api/batches", {"plan_id": "EP-205-01"})
        assert refused.status_code == 409 and refused.json()["detail"]["code"] == "method_invalid", refused.text
    finally:
        db.expire_all()
        recipe = db.get(Recipe, "R-205")
        recipe.steps = original
        db.commit()


def test_out_of_range_step_params_and_bad_definitions_are_caught(researcher, qa):
    broken = researcher.post("/api/device-methods", {**DEFINITION, "params": {"speed": {"min": 1}}}).json()
    assert any("speed 不是能力" in issue for issue in broken["issues"])
    refused = qa.post(f"/api/device-methods/{broken['id']}/release", {"row_version": broken["row_version"]})
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "method_invalid"
    assert researcher.delete(f"/api/device-methods/{broken['id']}").status_code == 200


def test_driver_self_report_is_stored_and_listed(admin, db):
    described = admin.post("/api/stations/ST-05/adapter/describe")
    assert described.status_code == 200, described.text
    body = described.json()
    assert body["described_from"] == "device" and body["methods"][0]["program"] == "*"
    assert "hold" in body["commands"]
    db.expire_all()
    assert db.get(Adapter, "ST-05").methods[0]["program"] == "*"
    station = next(row for row in admin.get("/api/stations").json() if row["id"] == "ST-05")
    assert station["adapter"]["catalog"]["methods"]


def test_a_curve_output_keeps_its_kind_and_links_a_curve_metric(researcher):
    """谱图、充放电曲线这类输出：输出类型 series 要随方法存下来，才能关联曲线型指标（不然核对一律说「输出类型要选曲线」）。"""
    from uuid import uuid4

    metric = researcher.post("/api/metrics", {"code": f"spec_{uuid4().hex[:6]}", "name": "谱图", "value_type": "series",
                                              "unit": "counts", "rules": {"x_label": "拉曼位移", "x_unit": "cm-1"}})
    assert metric.status_code == 201, metric.text
    created = researcher.post("/api/device-methods", {**DEFINITION, "name": f"带谱图 {uuid4().hex[:4]}", "outputs": [
        {"key": "spectrum", "label": "谱图", "unit": "counts", "kind": "series", "metric_id": metric.json()["id"]},
    ]})
    assert created.status_code == 201, created.text
    method = created.json()
    assert method["issues"] == [] and method["outputs"][0]["kind"] == "series", method
    numeric = researcher.post("/api/device-methods", {**DEFINITION, "name": f"错的类型 {uuid4().hex[:4]}", "outputs": [
        {"key": "spectrum", "unit": "counts", "metric_id": metric.json()["id"]},
    ]}).json()
    assert any("输出类型要选曲线" in issue for issue in numeric["issues"]), numeric["issues"]
    for row in (method, numeric):
        assert researcher.delete(f"/api/device-methods/{row['id']}").status_code == 200


def test_option_and_program_parameter_rules_are_saved_through_the_api(admin, researcher):
    """选项型参数的方法规则（缺省选项 + 允许的选项）与程序表参数的缺省程序表走接口能存下来：
    接口模型以前只收数值缺省值，页面一提交选项型或程序表规则就 422，只给允许的选项则被悄悄丢掉。"""
    from uuid import uuid4

    uid = uuid4().hex[:6]
    cap = f"cap.rules_{uid}"
    columns = [
        {"key": "mode", "label": "工步", "type": "enum", "options": ["恒流充电", "静置"], "required": True},
        {"key": "current", "label": "电流", "unit": "C"},
        {"key": "time", "label": "时长", "unit": "min"},
    ]
    created = admin.post("/api/capabilities", {
        "id": cap, "name": f"规则 {uid}", "params": {"temp": "温度", "solvent": "溶剂", "protocol": "工步"},
        "param_specs": {"temp": {"unit": "℃"}, "solvent": {"type": "enum", "options": ["THF", "DMF", "Toluene"]},
                        "protocol": {"type": "program", "columns": columns, "max_rows": 10}},
        "recovery": {"pausable": False, "retryable": False}, "stations": [],
        "signature_id": admin.sign("能力模型变更批准", target=cap),
    })
    assert created.status_code == 201, created.text
    protocol = [{"mode": "恒流充电", "current": 0.1}, {"mode": "静置", "time": 10}]
    definition = {
        "name": f"选项与程序表 {uid}", "capability_id": cap, "program": "P-1", "dur_min": 5,
        "params": {"temp": {"default": 60, "min": 20, "max": 80, "unit": "℃"},
                   "solvent": {"default": "THF", "options": ["THF", "DMF"]},
                   "protocol": {"default": protocol}},
    }
    saved = researcher.post("/api/device-methods", definition)
    assert saved.status_code == 201, saved.text
    method = saved.json()
    assert method["issues"] == [], method["issues"]
    assert method["params"]["solvent"]["default"] == "THF" and method["params"]["solvent"]["options"] == ["THF", "DMF"]
    assert method["params"]["protocol"]["default"] == protocol
    assert method["params"]["temp"]["default"] == 60 and "options" not in method["params"]["temp"], "数值参数不存空的选项"

    narrowed = researcher.patch(f"/api/device-methods/{method['id']}", {
        "params": {**definition["params"], "solvent": {"options": ["DMF"]}}, "row_version": method["row_version"],
    })
    assert narrowed.status_code == 200, narrowed.text
    assert narrowed.json()["params"]["solvent"] == {"options": ["DMF"]}

    wrong = researcher.post("/api/device-methods", {
        **definition, "name": f"错的选项 {uid}", "params": {"solvent": {"default": "Toluene", "options": ["THF", "水"]}},
    })
    assert wrong.status_code == 201, wrong.text
    issues = wrong.json()["issues"]
    assert any("不是能力登记的选项" in issue for issue in issues) and any("不在允许的选项" in issue for issue in issues), issues
    for row in (method, wrong.json()):
        assert researcher.delete(f"/api/device-methods/{row['id']}").status_code == 200


def test_two_device_steps_linking_one_metric_are_refused_before_any_result(researcher, qa, operator, db, reset_runtime):
    """同一指标被两个设备步骤关联：一个样本每个指标只保留一条当前结果，后一步的读数会把前一步的当成旧版本取代。
    流程校验把它列为后一步的问题（不能发布）；绕过发布的老流程，建批次时同样拦下。"""
    from uuid import uuid4

    metric = researcher.post("/api/metrics", {"code": f"moist_{uuid4().hex[:6]}", "name": "水分", "value_type": "number",
                                              "unit": "ppm"})
    assert metric.status_code == 201, metric.text
    method = _released_method(researcher, qa, {
        **DEFINITION, "name": f"带指标的干燥 {uuid4().hex[:4]}",
        "outputs": [{"key": "moisture_ppm", "label": "水分", "unit": "ppm", "metric_id": metric.json()["id"]}],
    })
    recipe = db.get(Recipe, "R-205")
    original = copy.deepcopy(recipe.steps)
    try:
        first = {**original[0], "method": {"id": method["id"]}}
        again = {**first, "step_id": f"{first.get('step_id') or 's01'}-again", "name": "二次干燥"}
        recipe.steps = [first, again, *original[1:]]
        db.commit()

        validation = researcher.get("/api/recipes/R-205").json()["validation"]
        assert validation[0]["ok"], validation[0]["blockers"]
        assert not validation[1]["ok"] and any("已由第 1 步" in issue for issue in validation[1]["issues"]), validation[1]

        refused = operator.post("/api/batches", {"plan_id": "EP-205-01"})
        assert refused.status_code == 409 and refused.json()["detail"]["code"] == "metric_linked_twice", refused.text
    finally:
        db.expire_all()
        recipe = db.get(Recipe, "R-205")
        recipe.steps = original
        db.commit()
