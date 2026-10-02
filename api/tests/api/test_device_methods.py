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
