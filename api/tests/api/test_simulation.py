"""执行前仿真：按需仿真给出路径、并发与物料结论；提交评审时仿真不通过就拒绝。"""


def test_simulation_reports_paths_concurrency_and_materials(researcher, reset_runtime):
    result = researcher.post("/api/recipes/R-205/simulate", {"concurrency": 3})
    assert result.status_code == 200, result.text
    body = result.json()
    checks = {row["key"]: row for row in body["checks"]}
    assert body["ok"] and checks["resources"]["state"] == "pass"
    assert checks["timeline"]["label"].startswith("当前时间线上 3 个批次")
    assert checks["materials"]["state"] in {"pass", "warn"} and "电解液" in checks["materials"]["detail"]
    assert body["paths"] and body["station_load_min"]
    detail = researcher.get("/api/recipes/R-205").json()
    assert detail["simulation"]["content_hash"] and detail["simulation_current"] is True


def test_submit_is_refused_when_the_method_cannot_be_scheduled_even_in_an_empty_lab(researcher):
    created = researcher.post("/api/recipes", {"name": "硬时限过紧", "plate": 8, "copy_from": "R-205"})
    assert created.status_code in {200, 201}, created.text
    recipe = created.json()
    detail = researcher.get(f"/api/recipes/{recipe['id']}").json()
    steps = detail["steps"]
    # 注液组装换到另一台工位，中间要转运；1 min 的硬时限在空实验室里也满足不了
    steps[2]["hard"] = {"from": "称重结束", "maxGapMin": 1}
    saved = researcher.patch(f"/api/recipes/{recipe['id']}", {"steps": steps, "row_version": detail["row_version"]})
    assert saved.status_code == 200, saved.text
    refused = researcher.post(f"/api/recipes/{recipe['id']}/submit")
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "simulation_failed", refused.text
    assert any("硬时限" in row["label"] for row in refused.json()["detail"]["blocked"])
