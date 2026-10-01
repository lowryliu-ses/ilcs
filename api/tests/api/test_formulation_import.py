"""配液模板与配方表导入：模板维护与校验、上传解析（csv / xlsx）、预览、导入（登记瓶子、流程草稿、方案草稿）、
幂等重放、同结构不同量的第二张表沿用流程只新建方案；一瓶一配方：配过液的瓶子不能再导入。"""
import uuid

import pytest

from app.models import Batch, Capability, Plan, PhysicalSample, Recipe, Sample
from tests.domain.test_formulation import METRIC, capability_specs, template_config
from tests.domain.test_spreadsheet import xlsx

BASE = "/api/formulation-templates"


@pytest.fixture()
def line(db, researcher, qa, admin):
    """现建一套能力（唯一前缀）、一条已发布的液体加注方法和一组试剂物料主数据。"""
    tag = uuid.uuid4().hex[:6]
    prefix = f"cap.fx{tag}."
    for cap_id, spec in capability_specs(prefix).items():
        db.add(Capability(id=cap_id, name=spec["name"], params=spec["params"], param_specs=spec["param_specs"],
                          recovery={}, retired=False))
    db.commit()
    created = researcher.post("/api/device-methods", {
        "name": "溶剂称量加注", "capability_id": f"{prefix}dose_liquid",
        "params": {"mass": {"default": 0, "min": 0, "max": 60, "unit": "g"}}, "dur_min": 3,
    })
    assert created.status_code == 201, created.text
    method = created.json()
    released = qa.post(f"/api/device-methods/{method['id']}/release", {"row_version": method["row_version"]})
    assert released.status_code == 200, released.text
    reagents = {f"EC{tag}": "预热溶剂", f"EMC{tag}": "溶剂", f"LiPF6{tag}": "锂盐", f"VC{tag}": "添加剂"}
    for name, category in reagents.items():
        response = admin.post("/api/materials", {"code": f"ELY-{name}", "name": name, "base_unit": "g",
                                                 "category": category})
        assert response.status_code == 201, response.text
    return {"tag": tag, "prefix": prefix, "method": method["id"], "reagents": list(reagents),
            "config": template_config(prefix, {"dose_liquid": method["id"]})}


def _template(researcher, line, code=None):
    response = researcher.post(BASE, {"code": code or f"FT-{line['tag']}", "name": "电解液配制", "description": "测试模板",
                                      "config": line["config"]})
    assert response.status_code == 201, response.text
    return response.json()


def _table(line, rows):
    ec, emc, lipf6, vc = line["reagents"]
    return [["序列号", f"{ec} (g)", f"{emc}(g)", f"{lipf6}（g）", vc], *rows]


def _csv(table) -> bytes:
    return "\n".join(",".join("" if cell is None else str(cell) for cell in row) for row in table).encode("utf-8-sig")


def test_template_crud_validation_and_permissions(researcher, operator, line):
    broken = {**line["config"], "plate": 0, "routes": {**line["config"]["routes"], "锂盐": {"stage": "nope"}}}
    invalid = researcher.post(BASE, {"code": f"FT-{line['tag']}-X", "name": "坏模板", "config": broken})
    assert invalid.status_code == 422, invalid.text
    body = invalid.json()["detail"]
    assert body["code"] == "formulation_template_invalid"
    assert "每批样品位 plate 必须是 1–96 的整数" in body["problems"]
    assert "加法「锂盐」的阶段 nope 不存在" in body["problems"]

    assert operator.post(BASE, {"code": "FT-OP", "name": "x", "config": line["config"]}).status_code == 403
    template = _template(researcher, line)
    assert template["state"] == "active" and template["check"] == {"ok": True, "problems": []}
    taken = researcher.post(BASE, {"code": template["code"], "name": "重名", "config": line["config"]})
    assert taken.status_code == 409 and taken.json()["detail"]["code"] == "formulation_template_code_taken"
    assert any(row["id"] == template["id"] for row in operator.get(BASE).json()), "列表对所有登录用户可见"

    stale = researcher.patch(f"{BASE}/{template['id']}", {"name": "改名", "row_version": template["row_version"] + 5})
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "version_conflict"
    bad = researcher.patch(f"{BASE}/{template['id']}", {"config": broken, "row_version": template["row_version"]})
    assert bad.status_code == 422
    renamed = researcher.patch(f"{BASE}/{template['id']}", {"name": "改名", "row_version": template["row_version"]})
    assert renamed.status_code == 200 and renamed.json()["name"] == "改名"
    assert renamed.json()["row_version"] == template["row_version"] + 1

    retired = researcher.post(f"{BASE}/{template['id']}/retire", {"row_version": renamed.json()["row_version"]})
    assert retired.status_code == 200 and retired.json()["state"] == "retired"
    assert researcher.patch(f"{BASE}/{template['id']}", {"name": "再改"}).status_code == 409
    assert template["id"] not in {row["id"] for row in researcher.get(f"{BASE}?state=active").json()}
    closed = researcher.post(f"{BASE}/{template['id']}/preview", {"filename": "a.csv", "table": [["序列号"]]})
    assert closed.status_code == 409 and closed.json()["detail"]["code"] == "formulation_template_retired"
    audit = researcher.get(f"/api/audit?target={template['id']}").json()
    actions = {row["action"] for row in (audit["items"] if isinstance(audit, dict) else audit)}
    assert {"新建配液模板", "修改配液模板", "退役配液模板"} <= actions


def test_parse_csv_and_xlsx_then_preview_with_params(researcher, line):
    template = _template(researcher, line)
    table = _table(line, [["ELY-A1", 10.6632, 14.9328, 4.608, 1.368]])
    parsed = researcher.upload(f"{BASE}/{template['id']}/parse", "配方.csv", _csv(table), "text/csv")
    assert parsed.status_code == 200, parsed.text
    preview = parsed.json()
    assert preview["issues"] == [], preview["issues"]
    kinds = [col["kind"] for col in preview["columns"]]
    assert kinds == ["serial", "reagent", "reagent", "reagent", "reagent"]
    # 固定步骤 6 + 液体 EC、EMC+搅拌 3 + then 3 + 锂盐+搅拌、VC 3 + then 3 + suffix 9
    assert len(preview["steps"]) == 27
    assert [step["material"] for step in preview["steps"] if step.get("material")] == line["reagents"]
    assert preview["plan"]["factors"][-2]["levels"] == [2], "实验参数按缺省值"
    assert preview["table"][1][0] == "ELY-A1"
    assert any("当前没有可承接的工位" in item for item in preview["warnings"]), "新能力没有工位：只提醒，不挡导入"

    shared = ["序列号", *table[0][1:], "ELY-A1"]
    cells = "".join(f'<c r="{chr(65 + index)}1" t="s"><v>{index}</v></c>' for index in range(5))
    values = "".join(f'<c r="{chr(66 + index)}2"><v>{value}</v></c>' for index, value in enumerate(table[1][1:]))
    rows = f'<row r="1">{cells}</row><row r="2"><c r="A2" t="s"><v>5</v></c>{values}</row>'
    from_xlsx = researcher.upload(f"{BASE}/{template['id']}/parse", "配方.xlsx", xlsx(rows, shared),
                                  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    assert from_xlsx.status_code == 200, from_xlsx.text
    assert from_xlsx.json()["steps"] == preview["steps"] and from_xlsx.json()["rows"] == preview["rows"]

    refreshed = researcher.post(f"{BASE}/{template['id']}/preview",
                                {"filename": "配方.csv", "table": preview["table"], "params": {"bottles": 3}})
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["plan"]["factors"][-2]["levels"] == [3]

    broken = researcher.upload(f"{BASE}/{template['id']}/parse", "配方.xlsx", b"nope", "application/octet-stream")
    assert broken.status_code == 422 and broken.json()["detail"]["code"] == "spreadsheet_invalid"
    malformed = researcher.upload(f"{BASE}/{template['id']}/parse", "配方.xlsx",
                                  xlsx('<row r="x"><c r="A1"><v>1</v></c></row>'), "application/octet-stream")
    assert malformed.status_code == 422 and malformed.json()["detail"]["code"] == "spreadsheet_invalid"

    # 筛选掉的行不导入，读表的提醒带到预览里；行号仍是工作表里的
    def bottle(number, extra=""):
        cells = "".join(f'<c r="{chr(66 + index)}{number}"><v>{value}</v></c>' for index, value in enumerate(table[1][1:]))
        return f'<row r="{number}"{extra}><c r="A{number}" t="s"><v>5</v></c>{cells}</row>'

    hidden = f'<row r="1">{cells}</row>' + bottle(2, ' hidden="1"') + bottle(3)
    filtered = researcher.upload(f"{BASE}/{template['id']}/parse", "配方.xlsx", xlsx(hidden, shared),
                                 "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    assert filtered.status_code == 200, filtered.text
    assert filtered.json()["rows"][0]["row"] == 3
    assert any("第 2 行在 Excel 里被隐藏或筛选掉了" in item for item in filtered.json()["warnings"])


def test_import_registers_samples_and_drafts_then_reuses_the_recipe(researcher, operator, line, db):
    template = _template(researcher, line)
    tag = line["tag"]
    serials = [f"ELY-{tag}-1", f"ELY-{tag}-2"]
    body = {"filename": "formula-1.csv", "params": {"volume": 25}, "plan_name": "",
            "table": _table(line, [[serials[0], 10.6632, 14.9328, 4.608, 1.368], [serials[1], 11, 14, 0, 1.2]])}
    url = f"{BASE}/{template['id']}/import"

    assert researcher.post_without_key(url, body).json()["detail"]["code"] == "idempotency_key_required"
    assert operator.post(url, body).status_code == 403, "操作员没有编辑流程的权限"
    key = f"import-{tag}"
    first = researcher.post(url, body, idempotency_key=key)
    assert first.status_code == 201, first.text
    result = first.json()
    assert result["recipe"]["reused"] is False and result["recipe"]["state"] == "draft"
    assert result["plan"]["state"] == "draft"
    assert result["samples"] == [{"id": serials[0], "created": True}, {"id": serials[1], "created": True}]

    replay = researcher.post(url, body, idempotency_key=key)
    assert replay.status_code in (200, 201) and replay.json() == result
    db.expire_all()
    assert db.query(Plan).filter(Plan.recipe_id == result["recipe"]["id"]).count() == 1, "重放不重复建方案"

    recipe = db.get(Recipe, result["recipe"]["id"])
    assert recipe.bom == [] and recipe.risk == "RA-ELY-01" and recipe.plate == 96
    assert recipe.name.startswith("电解液配制 · ") and "加料顺序" in recipe.design
    assert [step.get("material") for step in recipe.steps if step.get("material")] == line["reagents"]
    plan = db.get(Plan, result["plan"]["id"])
    assert plan.plan_type == "matrix" and plan.repeats == 1 and plan.sample_ids == serials
    assert plan.name == "电解液配制 · formula-1.csv" and plan.required_metrics == [METRIC]
    assert plan.design_points[1][:4] == [11, 14, 0, 1.2] and plan.design_points[1][-2:] == [2, 25]
    lipf6 = next(factor for factor in plan.factors if factor["name"] == line["reagents"][2])
    assert lipf6["levels"] == [0, 4.608] and lipf6["material"] == {"name": line["reagents"][2], "unit": "g", "per": 1}
    sample = db.get(PhysicalSample, serials[0])
    assert sample.barcode == serials[0] and sample.sample_type == "电解液"
    assert sample.source == f"配方表 formula-1.csv（模板 {template['code']}）"
    detail = researcher.get(f"/api/plans/{plan.id}")
    assert detail.status_code == 200, detail.text
    # 生成的方案本身满足锁定检查；只剩「因子作用参数」因为测试里没给这些新能力建工位而不通过
    failing = {check["key"] for check in detail.json()["checks"] if not check["ok"]}
    assert failing <= {"targets"}, detail.json()["checks"]

    # 第二张表：同结构不同量，一瓶沿用（登记了、还没进过批次，比如改了表重新导入）、一瓶新登记
    # → 沿用流程，只新建方案；沿用的瓶子预览与导入都提醒
    second_table = _table(line, [[serials[1], 9, 15, 5, 1], [f"ELY-{tag}-3", 9.5, 15, 4, 1]])
    notice = f"序列号 {serials[1]} 已登记，将沿用"
    previewed = researcher.post(f"{BASE}/{template['id']}/preview", {"filename": "formula-2.csv", "table": second_table})
    assert previewed.status_code == 200 and previewed.json()["issues"] == [], previewed.text
    assert notice in previewed.json()["warnings"]
    second = researcher.post(url, {
        "filename": "formula-2.csv", "params": {}, "plan_name": "第二轮", "table": second_table,
    })
    assert second.status_code == 201, second.text
    again = second.json()
    assert again["recipe"] == {**result["recipe"], "reused": True}
    assert again["plan"]["id"] != result["plan"]["id"] and again["plan"]["name"] == "第二轮"
    assert again["samples"] == [{"id": serials[1], "created": False}, {"id": f"ELY-{tag}-3", "created": True}]
    assert notice in again["warnings"]
    audit = researcher.get(f"/api/audit?target={template['id']}").json()
    rows = audit["items"] if isinstance(audit, dict) else audit
    assert sum(1 for row in rows if row["action"] == "配方表导入") == 2

    # 一瓶一配方：已在某个批次里有运行分配的瓶子，预览列为问题，导入整张表拒绝
    batch = Batch(id=f"B-FX{tag}", org_id="ORG-001", plan_id=plan.id, recipe_id=recipe.id, state="done",
                  recipe_snapshot={}, plan_snapshot={})
    db.add(batch)
    db.flush()
    db.add(Sample(id=f"S-FX{tag}", org_id="ORG-001", physical_sample_id=serials[0], batch_id=batch.id,
                  well="A1", position=1))
    db.commit()
    try:
        third_table = _table(line, [[serials[0], 9, 15, 5, 1], [f"ELY-{tag}-4", 9.5, 15, 4, 1]])
        used = f"序列号 {serials[0]} 已在批次 {batch.id} 里配过液，不能再作为空瓶导入"
        blocked = researcher.post(f"{BASE}/{template['id']}/preview", {"filename": "formula-3.csv", "table": third_table})
        assert blocked.status_code == 200 and used in blocked.json()["issues"], blocked.text
        rejected = researcher.post(url, {"filename": "formula-3.csv", "table": third_table})
        assert rejected.status_code == 422, rejected.text
        assert rejected.json()["detail"]["code"] == "sample_unusable"
        assert used in rejected.json()["detail"]["problems"]
        db.expire_all()
        assert db.get(PhysicalSample, f"ELY-{tag}-4") is None, "被拒的表一个瓶子都不登记"
    finally:
        db.rollback()
        db.query(Sample).filter(Sample.id == f"S-FX{tag}").delete()
        db.query(Batch).filter(Batch.id == batch.id).delete()
        db.commit()


def test_import_rejects_problem_tables_and_unusable_bottles(researcher, line, db):
    template = _template(researcher, line)
    url = f"{BASE}/{template['id']}/import"
    tag = line["tag"]
    uneven = researcher.post(url, {"filename": "bad.csv", "table": _table(line, [
        [f"B-{tag}-1", 1, 1, 1, 1], [f"B-{tag}-2", 1, 1, 1, 1], [f"B-{tag}-3", 2, 1, 1, 1], ["", -1, 1, 1, 1],
    ])})
    assert uneven.status_code == 422, uneven.text
    detail = uneven.json()["detail"]
    assert detail["code"] == "formulation_invalid"
    assert any("配方重复数不一致" in item for item in detail["problems"])
    assert any("第 5 行没有序列号" in item for item in detail["problems"])
    db.expire_all()
    assert db.get(PhysicalSample, f"B-{tag}-1") is None, "有问题的表格一个瓶子都不登记"

    db.add(PhysicalSample(id=f"B-{tag}-9", org_id="ORG-001", barcode=f"B-{tag}-9", lifecycle_state="disposed"))
    db.commit()
    disposed = researcher.post(url, {"filename": "ok.csv", "table": _table(line, [
        [f"B-{tag}-8", 1, 1, 1, 1], [f"B-{tag}-9", 2, 1, 1, 1],
    ])})
    assert disposed.status_code == 422, disposed.text
    assert disposed.json()["detail"]["code"] == "sample_unusable"
    db.expire_all()
    assert db.get(PhysicalSample, f"B-{tag}-8") is None


def test_check_endpoint_validates_unsaved_config_and_can_try_a_table(researcher, operator, line):
    """模板编辑器：没保存的配置也能核对、带一张表试算；不写库、不登记瓶子。"""
    broken = {**line["config"], "plate": 0}
    checked = researcher.post(f"{BASE}/check", {"config": broken})
    assert checked.status_code == 200, checked.text
    assert not checked.json()["ok"] and "每批样品位 plate 必须是 1–96 的整数" in checked.json()["problems"]
    specs = checked.json()["param_specs"]
    assert specs["bottles"]["type"] == "integer" and specs["volume"]["unit"] == "mL"

    serial = f"TRY-{line['tag']}"
    table = _table(line, [[serial, 10, 15, 4.6, 1.2]])
    tried = researcher.post(f"{BASE}/check", {"config": line["config"], "name": "试算", "table": table,
                                              "params": {"volume": 5}})
    assert tried.status_code == 200, tried.text
    preview = tried.json()["preview"]
    assert tried.json()["ok"] and preview["issues"] == [], preview["issues"]
    assert any(step["name"].endswith("称量加注") for step in preview["steps"])
    assert preview["plan"]["sample_ids"] == [serial]
    assert researcher.get(f"/api/samples/{serial}").status_code == 404, "试算不登记瓶子"
    assert operator.post(f"{BASE}/check", {"config": line["config"]}).status_code == 403


def test_table_endpoint_reads_a_sheet_without_any_template(researcher, operator, line):
    table = _table(line, [["R-1", 10, 15, 4.6, 1.2]])
    response = researcher.upload(f"{BASE}/table", "配方.csv", _csv(table), "text/csv")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["filename"] == "配方.csv" and body["table"][1][0] == "R-1" and body["warnings"] == []
    assert researcher.upload(f"{BASE}/table", "配方.txt", b"x", "text/plain").status_code == 422
    assert operator.upload(f"{BASE}/table", "配方.csv", _csv(table), "text/csv").status_code == 403
