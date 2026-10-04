"""方案与批次这一侧的守卫（配液线接入评审的修正）：

- 样本只用一次（fresh，配液线的一瓶一配方）：指定瓶子已在别的未终止批次里，不能再建批次；已处置/已用尽的瓶子锁定检查与建批次都挡。
- 人工步骤投的料不在 BOM 里：提交评审、批准都拒绝（与检查清单同一句话）。
- 方案检查按子流程展开后的步骤：子流程里「用量由方案给出」的料锁不上。
- 物料预览与预留同一口径：批号单位不一致不算可用。
- 方案给出用量、本批合计为 0：开跑检查的物料项不适用，不再永久拦住。
- 看板功能岛：没登记的岛号也汇总，名字退回「实验区 #N」。
"""
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.core.errors import StateConflict
from app.models import Recipe
from app.services.batch_service import BatchService
from test_step_materials import CAPACITY, _approve, _checks, _dose, _factor, _released, _sample

MANUAL_TAIL = "人工步骤的用量只能按 BOM 预留，请把它加进 BOM"


def _lot(operator, qa, material: str, unit: str = "g", qty: str = "100") -> str:
    lot_id = f"LOT-PG-{uuid4().hex[:8]}"
    created = operator.post(
        "/api/lots", {"id": lot_id, "material": material, "qty": qty, "unit": unit, "expiry": "2030-12-31"},
    )
    assert created.status_code == 201, created.text
    released = qa.post(f"/api/lots/{lot_id}/release", {"signature_id": qa.sign("复验合格", target=lot_id)})
    assert released.status_code == 200, released.text
    return lot_id


def _bottle_plan(researcher, qa, operator, solvent: str, bottles: list[str]) -> str:
    recipe_id = _released(researcher, qa, [_dose("s01", "溶剂称量加注", solvent, material_param="mass")])
    plan = researcher.post("/api/plans", {
        "name": "逐瓶配方", "recipe_id": recipe_id, "plan_type": "matrix", "repeats": 1,
        "factors": [_factor("溶剂", solvent, "s01", [2.5, 3.25])], "design_points": [[2.5], [3.25]],
        "sample_ids": bottles, "required_metrics": [CAPACITY],
    })
    assert plan.status_code == 201, plan.text
    return plan.json()["id"]


def test_one_bottle_one_formulation(researcher, qa, operator):
    solvent = f"守卫溶剂-{uuid4().hex[:6]}"
    _lot(operator, qa, solvent)
    bottles = [_sample(operator), _sample(operator)]
    plan_id = _bottle_plan(researcher, qa, operator, solvent, bottles)
    _approve(researcher, qa, plan_id)

    first = operator.post("/api/batches", {"plan_id": plan_id})
    assert first.status_code == 201, first.text
    first_id = first.json()["id"]
    again = operator.post("/api/batches", {"plan_id": plan_id})
    assert again.status_code == 409, again.text
    assert again.json()["detail"]["code"] == "sample_in_use"
    assert f"样本 {bottles[0]} 已分配给批次 {first_id}" in again.json()["detail"]["message"]
    reserved = operator.get(f"/api/batches/{first_id}").json()["reservations"]
    assert [row["qty"] for row in reserved] == ["5.750000"], "被拒的第二个批次没有留下第二份预留"

    # 第一个批次终止后瓶子可以重新配液
    aborted = operator.post(
        f"/api/batches/{first_id}/abort",
        {"reason": "换批重做", "signature_id": operator.sign("安全终止", target=first_id)},
    )
    assert aborted.status_code == 200, aborted.text
    assert operator.get(f"/api/batches/{first_id}").json()["state"] == "aborted"
    retry = operator.post("/api/batches", {"plan_id": plan_id})
    assert retry.status_code == 201, retry.text


def test_aborted_batch_that_already_sent_commands_keeps_its_bottles(researcher, qa, operator, db):
    """终止的批次只有真向设备发过指令才占着瓶子（瓶里可能已经投了料）；导入与建批次同一口径。"""
    from app.models import Batch, Command
    from app.services.batch_service import BatchService
    from app.core.context import system_context

    solvent = f"守卫溶剂-{uuid4().hex[:6]}"
    _lot(operator, qa, solvent)
    bottles = [_sample(operator), _sample(operator)]
    plan_id = _bottle_plan(researcher, qa, operator, solvent, bottles)
    _approve(researcher, qa, plan_id)
    first_id = operator.post("/api/batches", {"plan_id": plan_id}).json()["id"]
    batch = db.get(Batch, first_id)
    # 第一步的指令已经送到设备，之后批次被终止
    db.add(Command(
        org_id=batch.org_id, batch_id=first_id, station_id="ST-02", capability="cap.dose_solid",
        type="dispatch", state="done", delivery_state="delivered", step_index=0,
    ))
    batch.state = "aborted"
    db.commit()

    service = BatchService(db, system_context(batch.org_id))
    assert service.sample_used_by(bottles[0]) == first_id
    again = operator.post("/api/batches", {"plan_id": plan_id})
    assert again.status_code == 409, again.text
    assert again.json()["detail"]["code"] == "sample_in_use"


def test_disposed_bottle_blocks_lock_and_batch_creation(researcher, qa, operator):
    solvent = f"守卫溶剂-{uuid4().hex[:6]}"
    _lot(operator, qa, solvent)
    bottles = [_sample(operator), _sample(operator)]
    plan_id = _bottle_plan(researcher, qa, operator, solvent, bottles)
    _approve(researcher, qa, plan_id)

    disposed = operator.post(f"/api/samples/{bottles[1]}/dispose", {"reason": "瓶子破损"})
    assert disposed.status_code == 200, disposed.text
    check = _checks(researcher, plan_id)["physical_samples"]
    assert not check["ok"] and f"已处置/已用尽：{bottles[1]}（已处置）" in check["detail"]
    created = operator.post("/api/batches", {"plan_id": plan_id})
    assert created.status_code == 409, created.text
    assert created.json()["detail"]["code"] == "sample_unusable"
    assert bottles[1] in created.json()["detail"]["message"]


def _manual(step_id: str, name: str, material: str) -> dict:
    return {"step_id": step_id, "name": name, "kind": "manual", "dur": 5, "requires_sample_check": False,
            "consumes_materials": True, "material": material,
            "form": [{"key": "ok", "label": "确认", "type": "bool"}]}


def test_manual_material_outside_bom_blocks_submit_and_approval(researcher, qa, db):
    uid = uuid4().hex[:6]
    salt = f"人工锂盐-{uid}"
    recipe_id = researcher.post("/api/recipes", {"name": f"人工投料 {uid}", "plate": 4}).json()["id"]
    patched = researcher.patch(
        f"/api/recipes/{recipe_id}", {"risk": "RA-manual v1", "bom": [], "steps": [_manual("s01", "手工补加锂盐", salt)]},
    )
    assert patched.status_code == 200, patched.text
    expected = f"人工步骤「手工补加锂盐」投的 {salt} 不在 BOM 里：{MANUAL_TAIL}"
    bom_row = next(row for row in patched.json()["checks"] if row["key"] == "bom")
    assert not bom_row["ok"] and bom_row["detail"] == expected
    refused = researcher.post(f"/api/recipes/{recipe_id}/submit")
    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"]["code"] == "manual_material_not_in_bom"
    assert refused.json()["detail"]["message"] == expected

    # 列进 BOM 就能提交；评审期间 BOM 被改没了（旧数据），批准同样拒绝
    patched = researcher.patch(f"/api/recipes/{recipe_id}", {"bom": [{"material": salt, "qty": 1, "unit": "g"}]})
    assert patched.status_code == 200, patched.text
    submitted = researcher.post(f"/api/recipes/{recipe_id}/submit")
    assert submitted.status_code == 200, submitted.text
    recipe = db.get(Recipe, recipe_id)
    recipe.bom = []
    db.commit()
    approved = qa.post(f"/api/recipes/{recipe_id}/transition",
                       {"target_state": "approved", "signature_id": qa.sign_recipe("批准流程", recipe_id)})
    assert approved.status_code == 422, approved.text
    assert approved.json()["detail"]["code"] == "manual_material_not_in_bom"
    assert approved.json()["detail"]["message"] == expected


def test_step_materials_check_sees_subflow_steps(researcher, qa):
    uid = uuid4().hex[:6]
    solvent = f"子流程溶剂-{uid}"
    inner = _released(researcher, qa, [_dose("s01", "子流程加注", solvent, material_param="mass")])
    parent = _released(researcher, qa, [
        {"step_id": "s01", "name": "配液", "kind": "subflow", "subflow": {"recipe_id": inner}},
        {**_dose("s02", "补加隔膜料", f"隔膜料-{uid}"), "after": ["s01"]},
    ], bom=[{"material": f"隔膜料-{uid}", "qty": 1, "unit": "g"}])
    plan = researcher.post("/api/plans", {
        "name": "引用子流程", "recipe_id": parent, "plan_type": "matrix", "repeats": 1,
        "factors": [{"name": "批次标签", "unit": "", "levels": [1, 2]}], "required_metrics": [CAPACITY],
    })
    assert plan.status_code == 201, plan.text
    check = _checks(researcher, plan.json()["id"])["step_materials"]
    assert not check["ok"], "以前只看父流程顶层步骤，报「流程没有由方案给出用量的物料」"
    assert f"子流程「配液」里的「子流程加注」投 {solvent}" in check["detail"]
    assert researcher.post(f"/api/plans/{plan.json()['id']}/lock").status_code == 409


def test_batch_creation_refuses_dosing_steps_without_a_source():
    steps = [_dose("s01", "加注", "EC")]
    content = SimpleNamespace(plan_type="matrix", factors=[_factor("EC", "EC", "s01", [1, 2]) | {"material": {
        "name": "EC", "unit": "g", "per": 0}}])
    capabilities = {"cap.dose_solid": {"params": {"mass": "质量"}, "param_specs": {"mass": {"unit": "g"}}}}
    with pytest.raises(StateConflict) as caught:
        BatchService._require_plan_dosing(content, [], steps, capabilities)
    assert caught.value.code == "plan_material_missing"
    ok = SimpleNamespace(plan_type="matrix", factors=[_factor("EC", "EC", "s01", [1, 2])])
    BatchService._require_plan_dosing(ok, [], steps, capabilities)


def test_material_preview_uses_reservation_lot_matching(researcher, qa, operator):
    solvent = f"单位溶剂-{uuid4().hex[:6]}"
    _lot(operator, qa, solvent, unit="mg", qty="100000")
    plan_id = _bottle_plan(researcher, qa, operator, solvent, [])
    rows = {row["material"]: row for row in researcher.get(f"/api/plans/{plan_id}").json()["materials"]}
    row = rows[solvent]
    assert "按方案用量预留" in row["source"]
    assert not row["ok"] and row["lots"] == [] and Decimal(row["available"]) == 0, "mg 批号不能给 g 的需求预留"
    assert "mg" in row["note"]
    _lot(operator, qa, solvent, unit="g")
    row = {r["material"]: r for r in researcher.get(f"/api/plans/{plan_id}").json()["materials"]}[solvent]
    assert row["ok"] and len(row["lots"]) == 1


def test_preflight_zero_plan_material_is_not_applicable(researcher, qa, operator, db):
    from sqlalchemy.orm.attributes import flag_modified

    from app.models import Batch

    solvent = f"零用量-{uuid4().hex[:6]}"
    _lot(operator, qa, solvent)
    plan_id = _bottle_plan(researcher, qa, operator, solvent, [])
    _approve(researcher, qa, plan_id)
    created = operator.post("/api/batches", {"plan_id": plan_id})
    assert created.status_code == 201, created.text
    batch_id = created.json()["id"]
    # 本批合计为 0 时 plan_materials 不往快照 BOM 里加行（例如只补测对照组的子任务）：直接造出这个结果。
    # 工位参数范围不收 0，所以没法经由正常方案走到这里
    batch = db.get(Batch, batch_id)
    batch.recipe_snapshot = {**batch.recipe_snapshot, "bom": []}
    flag_modified(batch, "recipe_snapshot")
    db.commit()
    assert operator.post(f"/api/batches/{batch_id}/schedule", {}).status_code == 200
    checks = {row["key"]: row for row in operator.get(f"/api/batches/{batch_id}/preflight").json()["checks"]}
    assert checks["material"]["state"] == "not_applicable", checks["material"]
    assert f"{solvent} 由方案给出用量，本批合计为 0" in checks["material"]["detail"]


def test_dashboard_lists_stations_on_unregistered_islands(operator, db):
    from app.models import Station

    station = db.query(Station).order_by(Station.id).first()
    original = station.island
    try:
        station.island = 97
        db.commit()
        islands = {row["id"]: row for row in operator.get("/api/dashboard").json()["islands"]}
        assert islands[97]["name"] == "实验区 #97" and islands[97]["stations"] == 1
    finally:
        station.island = original
        db.commit()


def test_naming_an_experiment_area(admin, operator, db):
    """实验区（岛号）起名：要工位编辑权限；列表带出工位用着、还没起名的岛号；看板按名称显示；改名留审计。"""
    from app.models import AuditEvent, Island, Station

    station = db.query(Station).order_by(Station.id).first()
    original = station.island
    try:
        station.island = 96
        db.commit()
        listed = {row["id"]: row for row in admin.get("/api/islands").json()}
        assert listed[96] == {"id": 96, "name": "", "stations": 1}
        assert operator.put("/api/islands/96", {"name": "配液段"}).status_code == 403
        assert admin.put("/api/islands/96", {"name": "  "}).status_code == 422
        assert admin.put("/api/islands/0", {"name": "未分区"}).status_code == 422
        named = admin.put("/api/islands/96", {"name": "  配液段 "})
        assert named.status_code == 200 and named.json() == {"id": 96, "name": "配液段"}
        renamed = admin.put("/api/islands/96", {"name": "配液段（手套箱 B）"})
        assert renamed.status_code == 200
        assert {row["id"]: row for row in admin.get("/api/islands").json()}[96]["name"] == "配液段（手套箱 B）"
        islands = {row["id"]: row for row in operator.get("/api/dashboard").json()["islands"]}
        assert islands[96]["name"] == "配液段（手套箱 B）"
        db.expire_all()
        trail = db.query(AuditEvent).filter(AuditEvent.target == "实验区 #96").order_by(AuditEvent.id).all()
        assert [row.action for row in trail][-2:] == ["登记实验区", "修改实验区名称"]
    finally:
        station.island = original
        row = db.get(Island, 96)
        if row is not None:
            db.delete(row)
        db.commit()
