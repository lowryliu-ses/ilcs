"""步骤级投料物料：流程 BOM 为空、每瓶用量由方案因子给出的整条链路。

- 流程检查：每个消耗步骤都写明投哪种料时，空 BOM 合法（用量由方案按样本给出）。
- 方案检查：这类物料必须有因子给出用量；指定的物理样本要与「条件 × 重复」一一对应。
- 建批次：按本批运行分配把因子给出的量追加进快照 BOM 并预留；运行分配指向方案给定的物理样本。
- 执行：内置模拟按用量参数回报消耗（逐孔位之和，单位是参数登记的单位），入账后与预留一致，对账按物料分摊不误报。
- 只写物料名的回报按这种料的多条预留（跨批号）分摊，整项入账或整项拒绝，不留空事件。
- 方案给出用量的物料，计划量就是这条指令下发的量：剔除瓶子、同一种料分两步投都不误报偏差。
- 工位配置 simulate_outputs 打开时，模拟回执补齐方法输出项，不再产生「缺必报项」。
"""
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.orm.attributes import flag_modified

from app.adapters.base import CommandRequest
from app.adapters.drivers.simulation import SimulationAdapter
from app.core.context import system_context
from app.core.errors import StateConflict
from app.models import Adapter, Alarm, Batch, InventoryEvent, InventoryLedger, Lot, Reservation, StepRun
from app.services.consumption_service import ConsumptionService
from app.services.execution_service import ExecutionService
from app.services.inventory_service import InventoryService
from test_graph_workflow import _dispatch, _graph_batch, _run

CAPACITY = "METRIC-discharge_capacity-v1"


def _lot(operator, qa, material: str, qty: str = "100", expiry: str = "2030-12-31") -> str:
    lot_id = f"LOT-SM-{uuid4().hex[:8]}"
    created = operator.post(
        "/api/lots", {"id": lot_id, "material": material, "qty": qty, "unit": "g", "expiry": expiry},
    )
    assert created.status_code == 201, created.text
    released = qa.post(f"/api/lots/{lot_id}/release", {"signature_id": qa.sign("复验合格", target=lot_id)})
    assert released.status_code == 200, released.text
    return lot_id


def _sample(operator) -> str:
    sample_id = f"PS-SM-{uuid4().hex[:8]}"
    created = operator.post("/api/samples", {"id": sample_id, "source": "瓶身序列号", "sample_type": "电解液"})
    assert created.status_code == 201, created.text
    return sample_id


def _dose(step_id: str, name: str, material: str, **extra) -> dict:
    return {
        "step_id": step_id, "kind": "device", "name": name, "cap": "cap.dose_solid",
        "consumes_materials": True, "material": material, "params": {"mass": 1}, "dur": 5, **extra,
    }


def _released(researcher, qa, steps: list[dict], bom: list[dict] | None = None) -> str:
    recipe_id = researcher.post("/api/recipes", {"name": f"逐瓶投料 {uuid4().hex[:4]}", "plate": 4}).json()["id"]
    patched = researcher.patch(
        f"/api/recipes/{recipe_id}", {"risk": "RA-step-materials v1", "bom": bom or [], "steps": steps},
    )
    assert patched.status_code == 200, patched.text
    submitted = researcher.post(f"/api/recipes/{recipe_id}/submit")
    assert submitted.status_code == 200, submitted.text
    for target, meaning in (("approved", "批准流程"), ("released", "发布流程")):
        moved = qa.post(f"/api/recipes/{recipe_id}/transition",
                        {"target_state": target, "signature_id": qa.sign_recipe(meaning, recipe_id)})
        assert moved.status_code == 200, moved.text
    return recipe_id


def _factor(name: str, material: str, step_id: str, levels: list[float]) -> dict:
    return {
        "name": name, "unit": "g", "levels": levels, "target": {"step_id": step_id, "param": "mass"},
        "material": {"name": material, "unit": "g", "per": 1},
    }


def _approve(researcher, qa, plan_id: str) -> None:
    locked = researcher.post(f"/api/plans/{plan_id}/lock")
    assert locked.status_code == 200, locked.text
    assert researcher.post(f"/api/plans/{plan_id}/submit").status_code == 200
    current = researcher.get(f"/api/plans/{plan_id}").json()
    decided = qa.post(
        f"/api/plans/{plan_id}/decision",
        {"conclusion": "approved",
         "signature_id": qa.sign("批准方案", target=plan_id, object_version=current["row_version"])},
    )
    assert decided.status_code == 200, decided.text


def _checks(researcher, plan_id: str) -> dict:
    return {row["key"]: row for row in researcher.get(f"/api/plans/{plan_id}").json()["checks"]}


def test_step_material_validation_messages(researcher):
    recipe_id = researcher.post("/api/recipes", {"name": f"投料校验 {uuid4().hex[:4]}", "plate": 4}).json()["id"]
    steps = [
        {"kind": "device", "name": "空物料名", "cap": "cap.dose_solid", "consumes_materials": True,
         "material": " ", "params": {"mass": 1}, "dur": 5},
        {"kind": "device", "name": "没勾消耗", "cap": "cap.dose_solid", "material": "某物料",
         "params": {"mass": 1}, "dur": 5},
        {"kind": "device", "name": "参数不属于能力", "cap": "cap.dose_solid", "consumes_materials": True,
         "material": "某物料", "material_param": "volume", "params": {"mass": 1}, "dur": 5},
        {"kind": "manual", "name": "人工不能指定用量参数", "consumes_materials": True, "material": "某物料",
         "material_param": "mass", "form": [{"key": "ok", "label": "确认", "type": "bool"}], "dur": 5},
    ]
    patched = researcher.patch(f"/api/recipes/{recipe_id}", {"steps": steps})
    assert patched.status_code == 200, patched.text
    issues = [row["issues"] for row in patched.json()["validation"]]
    assert "物料名称必须是非空文字" in issues[0]
    assert "声明了投料物料，但没有勾选「消耗物料」" in issues[1]
    assert "用量参数 volume 不是该能力的参数" in issues[2]
    assert "只有设备步骤可以指定用量参数" in issues[3]
    # 种子能力的参数都登记了单位；「没有登记单位」一条在 domain 测试里用临时能力验


def test_empty_bom_is_valid_when_every_consuming_step_names_its_material(researcher):
    recipe_id = researcher.post("/api/recipes", {"name": f"BOM 规则 {uuid4().hex[:4]}", "plate": 4}).json()["id"]

    def bom_check(steps, bom):
        patched = researcher.patch(f"/api/recipes/{recipe_id}", {"steps": steps, "bom": bom})
        assert patched.status_code == 200, patched.text
        return next(row for row in patched.json()["checks"] if row["key"] == "bom")

    declared = [_dose("s01", "投 EC", "EC"), _dose("s02", "投 EMC", "EMC")]
    check = bom_check(declared, [])
    assert check["ok"] and check["detail"] == "EC、EMC 的用量由实验方案按样本给出"

    undeclared = [_dose("s01", "投 EC", "EC"), _dose("s02", "投料", "x")]
    undeclared[1].pop("material")
    check = bom_check(undeclared, [])
    assert not check["ok"] and "未定义 BOM" in check["detail"]

    check = bom_check(declared, [{"material": "EC", "qty": 1, "unit": "g"}])
    assert check["ok"] and "EMC 不在 BOM 里，用量由实验方案给出" in check["detail"]


def test_step_materials_reserved_from_plan_booked_from_simulation_and_bound_to_given_samples(
    researcher, qa, operator, reset_runtime, executor, db,
):
    uid = uuid4().hex[:6]
    solvent, salt = f"逐瓶溶剂-{uid}", f"逐瓶锂盐-{uid}"
    _lot(operator, qa, solvent)
    _lot(operator, qa, salt)
    # 锂盐步骤不写用量参数：cap.dose_solid 只有 mass 一个以 g 计的参数，执行器按单位推断
    recipe_id = _released(researcher, qa, [
        _dose("s01", "溶剂称量加注", solvent, material_param="mass"),
        {**_dose("s02", "锂盐称量加料", salt), "after": ["s01"]},
    ])
    recipe = researcher.get(f"/api/recipes/{recipe_id}").json()
    assert next(row for row in recipe["checks"] if row["key"] == "bom")["ok"]

    bottles = [_sample(operator), _sample(operator)]
    plan = researcher.post("/api/plans", {
        "name": "逐瓶配方", "recipe_id": recipe_id, "plan_type": "matrix", "repeats": 1,
        "factors": [_factor("溶剂", solvent, "s01", [2.5, 3.25])],
        "design_points": [[2.5], [3.25]],
        "sample_ids": [*bottles, _sample(operator)], "required_metrics": [CAPACITY],
    })
    assert plan.status_code == 201, plan.text
    plan_id = plan.json()["id"]

    # 缺锂盐的因子、样本多一个：两项都挡住锁定
    checks = _checks(researcher, plan_id)
    assert not checks["step_materials"]["ok"]
    assert "第 2 步「锂盐称量加料」投" in checks["step_materials"]["detail"]
    assert "方案里也没有给出" in checks["step_materials"]["detail"]
    assert not checks["physical_samples"]["ok"] and "指定了 3 个样本" in checks["physical_samples"]["detail"]
    refused = researcher.post(f"/api/plans/{plan_id}/lock")
    assert refused.status_code == 409
    assert {row["key"] for row in refused.json()["detail"]["checks"]} >= {"step_materials", "physical_samples"}

    current = researcher.get(f"/api/plans/{plan_id}").json()
    patched = researcher.patch(f"/api/plans/{plan_id}", {
        "factors": [_factor("溶剂", solvent, "s01", [2.5, 3.25]), _factor("锂盐", salt, "s02", [1.2, 1.5])],
        "design_points": [[2.5, 1.2], [3.25, 1.5]], "sample_ids": bottles, "row_version": current["row_version"],
    })
    assert patched.status_code == 200, patched.text
    checks = _checks(researcher, plan_id)
    assert checks["step_materials"]["ok"] and checks["physical_samples"]["ok"], checks
    previews = {row["material"]: row for row in researcher.get(f"/api/plans/{plan_id}").json()["materials"]}
    assert "按方案用量预留" in previews[solvent]["source"]
    _approve(researcher, qa, plan_id)

    created = operator.post("/api/batches", {"plan_id": plan_id})
    assert created.status_code == 201, created.text
    batch_id = created.json()["id"]
    detail = operator.get(f"/api/batches/{batch_id}").json()
    reserved = {row["material"]: row["qty"] for row in detail["reservations"]}
    assert reserved == {solvent: "5.750000", salt: "2.700000"}, "预留量 = 本批各瓶用量之和"
    by_group = {row["condition_group"]: row["physical_sample_id"] for row in detail["samples"]}
    assert by_group == {"C01": bottles[0], "C02": bottles[1]}, "运行分配指向方案给定的瓶子"

    _dispatch(operator, batch_id)
    detail = _run(operator, batch_id, executor)
    assert detail["state"] == "done", detail["failure_reason"]
    consumed = {row["material"]: row["consumed_qty"] for row in detail["reservations"]}
    assert consumed == {solvent: "5.750000", salt: "2.700000"}, "模拟回报的消耗 = 各孔之和 = 预留"
    delivered = [cp["payload"]["delivered"] for cp in detail["checkpoints"]]
    assert {row["materials"][0]["material"] for row in delivered} == {solvent, salt}

    db.expire_all()
    alarms = db.query(Alarm).filter(Alarm.source_id == batch_id, Alarm.condition_key.like("command:%")).all()
    assert not alarms, [a.message for a in alarms]
    notes = [row.note for row in db.query(InventoryLedger).filter(InventoryLedger.batch_id == batch_id).all()
             if row.event_type == "consume"]
    assert notes and all("模拟设备回执" in note for note in notes)


def test_simulated_outputs_fill_method_rules_when_enabled(operator, db, reset_runtime, executor):
    """ST-05（内置模拟）打开 simulate_outputs：方法输出项补齐，不再有「缺必报项」；回显的参数不被覆盖。"""
    adapter = db.get(Adapter, "ST-05")
    original = dict(adapter.config or {})
    adapter.config = {**original, "simulate_outputs": True}
    adapter.config_version = (adapter.config_version or 1) + 1
    flag_modified(adapter, "config")
    db.commit()
    try:
        def with_method(steps):
            first = dict(steps[0])
            first["method"] = {
                "id": "dm-sim-out", "code": "DM-SO", "version": 1, "name": "干燥", "program": "VD",
                "outputs": [
                    {"key": "temp", "label": "箱温", "lo": 0, "hi": 200},
                    {"key": "moisture", "label": "水分", "unit": "ppm", "lo": 10, "hi": 50, "required": True},
                ],
            }
            return [first, *steps[1:]]

        batch_id = _graph_batch(operator, db, with_method)
        _dispatch(operator, batch_id)
        detail = _run(operator, batch_id, executor)
        assert detail["state"] == "done", detail["failure_reason"]
        first = next(cp for cp in detail["checkpoints"] if cp["step_index"] == 0)["payload"]["delivered"]
        assert first["temp"] == 120, "回显的参数不被示意值覆盖"
        assert 10 + 40 * 0.35 <= first["moisture"] <= 10 + 40 * 0.65
        db.expire_all()
        run = db.query(StepRun).filter(StepRun.batch_id == batch_id, StepRun.step_index == 0).first()
        assert not any(flag["code"] == "output_missing" for flag in run.flags or []), run.flags
    finally:
        db.expire_all()
        adapter = db.get(Adapter, "ST-05")
        adapter.config = original
        adapter.config_version = (adapter.config_version or 1) + 1
        flag_modified(adapter, "config")
        db.commit()


def _plan_batch(researcher, qa, operator, solvent: str, salt: str) -> str:
    """两瓶、溶剂 2.5 + 3.25 g、锂盐 1.2 + 1.5 g 的逐瓶配方批次。"""
    recipe_id = _released(researcher, qa, [
        _dose("s01", "溶剂称量加注", solvent, material_param="mass"),
        {**_dose("s02", "锂盐称量加料", salt), "after": ["s01"]},
    ])
    bottles = [_sample(operator), _sample(operator)]
    plan = researcher.post("/api/plans", {
        "name": "逐瓶配方", "recipe_id": recipe_id, "plan_type": "matrix", "repeats": 1,
        "factors": [_factor("溶剂", solvent, "s01", [2.5, 3.25]), _factor("锂盐", salt, "s02", [1.2, 1.5])],
        "design_points": [[2.5, 1.2], [3.25, 1.5]], "sample_ids": bottles, "required_metrics": [CAPACITY],
    })
    assert plan.status_code == 201, plan.text
    _approve(researcher, qa, plan.json()["id"])
    created = operator.post("/api/batches", {"plan_id": plan.json()["id"]})
    assert created.status_code == 201, created.text
    return created.json()["id"]


def test_material_only_usage_spreads_over_lots_and_is_all_or_nothing(
    researcher, qa, operator, reset_runtime, executor, db, monkeypatch,
):
    uid = uuid4().hex[:6]
    solvent, salt = f"跨批号溶剂-{uid}", f"跨批号锂盐-{uid}"
    early = _lot(operator, qa, solvent, qty="3", expiry="2030-06-30")
    late = _lot(operator, qa, solvent, qty="100")
    _lot(operator, qa, salt)
    batch_id = _plan_batch(researcher, qa, operator, solvent, salt)
    rows = sorted(
        (row for row in operator.get(f"/api/batches/{batch_id}").json()["reservations"] if row["material"] == solvent),
        key=lambda row: row["id"],
    )
    assert [(row["lot_id"], row["qty"]) for row in rows] == [(early, "3.000000"), (late, "2.750000")], (
        "有效期早的批号只剩 3 g：5.75 g 的溶剂分成两条预留"
    )

    db.expire_all()
    batch = db.get(Batch, batch_id)
    service = ConsumptionService(db, system_context(batch.org_id, "测试"))

    def fake(tag: str):
        return SimpleNamespace(id=f"fake-{tag}-{uid}", step_index=0, capability="cap.dose_solid", params={})

    def untouched(command_id: str) -> None:
        db.expire_all()
        assert db.query(InventoryEvent).filter(InventoryEvent.event_id.like(f"{command_id}#%")).count() == 0, (
            "被拒的回报不留没有明细的空事件"
        )
        assert db.get(Lot, early).qty == Decimal("3") and db.get(Lot, late).qty == Decimal("100")
        assert all(db.get(Reservation, row["id"]).consumed_qty == 0 for row in rows)

    # 预留合计装不下（多出的远超偏差阈值）：整项拒绝，按基础单位报合计余量（mg 回报先换成 g）
    short = fake("short")
    assert service.book(batch, short, {"materials": [{"material": solvent, "unit": "mg", "quantity": 7000}]}) == {
        "booked": 0, "rejected": 1, "deviations": 0,
    }
    alarm = db.query(Alarm).filter(Alarm.condition_key == f"command:{short.id}:material:1").one()
    assert "消耗被拒" in alarm.message and "剩余预留合计 5.750000g" in alarm.message, alarm.message
    untouched(short.id)

    # 第二条明细被库存校验拒绝：第一条对批号余额与预留的改动、事件行一起撤销
    original = InventoryService._apply_line

    def second_line_fails(self, event, line_no, item, user):
        if line_no == 2:
            raise StateConflict("库存校验未通过", {"blocked": [{"key": "line2", "label": "模拟拒绝"}]})
        return original(self, event, line_no, item, user)

    monkeypatch.setattr(InventoryService, "_apply_line", second_line_fails)
    partial = fake("partial")
    assert service.book(batch, partial, {"materials": [{"material": solvent, "unit": "g", "quantity": 5.75}]})[
        "rejected"
    ] == 1
    untouched(partial.id)
    monkeypatch.undo()
    # 上面两条是手工造的回报：报警清掉，免得活动报警挡住下面真跑时的首工位许可
    db.query(Alarm).filter(Alarm.condition_key.like(f"command:fake-%-{uid}:%")).delete(synchronize_session=False)
    db.commit()

    _dispatch(operator, batch_id)
    detail = _run(operator, batch_id, executor)
    assert detail["state"] == "done", detail["failure_reason"]
    consumed = {row["lot_id"]: row["consumed_qty"] for row in detail["reservations"] if row["material"] == solvent}
    assert consumed == {early: "3.000000", late: "2.750000"}, "一步的量按预留顺序分摊到两个批号"
    db.expire_all()
    lines = db.query(InventoryLedger).filter(
        InventoryLedger.batch_id == batch_id, InventoryLedger.event_type == "consume",
        InventoryLedger.lot_id.in_([early, late]),
    ).all()
    assert len({line.event_id for line in lines}) == 1 and sorted(line.line_no for line in lines) == [1, 2]
    alarms = [
        a.message for a in db.query(Alarm).filter(Alarm.source_id == batch_id, Alarm.condition_key.like("command:%"))
        if "fake-" not in a.condition_key
    ]
    assert not alarms, alarms


def test_small_overdraw_tops_up_the_reservation_and_books_the_actual_amount(researcher, qa, operator, reset_runtime, db):
    """称量加料多加了一点（在偏差阈值内）：先从同一批号追加预留、再按实际量入账，流水里看得到那笔追加；
    批号没有余量可补就照旧整项拒绝，追加的预留和消耗一起撤销，什么都不留。"""
    uid = uuid4().hex[:6]
    solvent, salt = f"超量溶剂-{uid}", f"超量锂盐-{uid}"
    lot = _lot(operator, qa, solvent, qty="100")
    _lot(operator, qa, salt)
    batch_id = _plan_batch(researcher, qa, operator, solvent, salt)
    db.expire_all()
    batch = db.get(Batch, batch_id)
    service = ConsumptionService(db, system_context(batch.org_id, "测试"))
    over = SimpleNamespace(id=f"fake-over-{uid}", step_index=0, capability="cap.dose_solid", params={})
    assert service.book(batch, over, {"materials": [{"material": solvent, "unit": "g", "quantity": 5.8}]}) == {
        "booked": 1, "rejected": 0, "deviations": 0,
    }
    db.commit()
    db.expire_all()
    assert db.get(Lot, lot).qty == Decimal("94.2"), "账上按天平称出来的 5.8 g 扣"
    reservation = db.query(Reservation).filter(Reservation.batch_id == batch_id, Reservation.lot_id == lot).one()
    assert (reservation.qty, reservation.consumed_qty) == (Decimal("5.8"), Decimal("5.8"))
    topup = db.query(InventoryEvent).filter(InventoryEvent.event_id == f"{over.id}#1:topup").one()
    assert (topup.source, topup.event_type) == ("system", "reserve") and "偏差阈值内" in topup.reason
    assert not db.query(Alarm).filter(Alarm.condition_key == f"command:{over.id}:material:1").count()

    # 批号已经全被占用：差的 0.05 g 补不上，整项拒绝，追加预留也不留下
    tight, tight_salt = f"满占溶剂-{uid}", f"满占锂盐-{uid}"
    tight_lot = _lot(operator, qa, tight, qty="5.75")
    _lot(operator, qa, tight_salt)
    tight_batch_id = _plan_batch(researcher, qa, operator, tight, tight_salt)
    db.expire_all()
    tight_batch = db.get(Batch, tight_batch_id)
    blocked = SimpleNamespace(id=f"fake-tight-{uid}", step_index=0, capability="cap.dose_solid", params={})
    assert service.book(tight_batch, blocked, {"materials": [{"material": tight, "unit": "g", "quantity": 5.8}]}) == {
        "booked": 0, "rejected": 1, "deviations": 0,
    }
    db.commit()
    db.expire_all()
    alarm = db.query(Alarm).filter(Alarm.condition_key == f"command:{blocked.id}:material:1").one()
    assert "消耗被拒" in alarm.message and "可用量" in alarm.message, alarm.message
    assert not db.query(InventoryEvent).filter(InventoryEvent.event_id.like(f"{blocked.id}#%")).count()
    assert db.get(Lot, tight_lot).qty == Decimal("5.75")
    db.query(Alarm).filter(Alarm.condition_key.like(f"command:fake-%-{uid}:%")).delete(synchronize_session=False)
    db.commit()


def test_dosing_hook_reports_param_unit_and_consumption_converts_it(db):
    """显式用量参数的单位（μL）和 BOM（mL）不同：回报贴参数自己的单位，入账按同量纲换算，不错量级。"""
    snapshot = {
        "steps": [{"step_id": "s01", "kind": "device", "name": "注液", "cap": "cap.assemble",
                   "consumes_materials": True, "material": "电解液 LP57", "material_param": "electrolyte",
                   "params": {"electrolyte": 55}}],
        "bom": [{"material": "电解液 LP57", "qty": 1, "unit": "mL"}],
    }
    ctx = system_context("ORG-001", "测试")
    hooks = ExecutionService(db, ctx)._step_hooks(
        SimpleNamespace(id="B-HOOK", recipe_snapshot=snapshot),
        SimpleNamespace(type="dispatch", step_index=0, capability="cap.assemble", params={"electrolyte": 55}),
    )
    assert hooks["material"] == {"name": "电解液 LP57", "unit": "μL", "param": "electrolyte"}

    wells = {f"A{i}": {"electrolyte": 55} for i in range(1, 9)}
    done = SimulationAdapter("ST-X").submit(CommandRequest(
        command_id="cmd-unit", station_id="ST-X", capability="cap.assemble", type="dispatch", batch_id="B-1",
        step_index=0, params={"electrolyte": 55, "wells": wells}, material=hooks["material"],
    ))
    assert done.delivered["materials"] == [{"material": "电解液 LP57", "unit": "μL", "quantity": 440.0}]

    service = ConsumptionService(db, ctx)
    lot = SimpleNamespace(material="电解液 LP57", material_id=None, unit="mL")
    assert service._to_base(Decimal("440"), "μL", lot) == ("mL", Decimal("0.440000"))
    with pytest.raises(ValueError):
        service._to_base(Decimal("1"), "g", lot)  # 跨量纲没登记换算：拒绝，不猜密度


def test_planned_amount_for_plan_materials_is_what_the_command_carried(db):
    """方案给出用量的物料：计划量 = 这条指令下发的量，不是整批量 ÷ 步骤数。"""
    steps = [
        _dose("s01", "EC 第一份", "EC", material_param="mass"),
        {**_dose("s02", "EC 第二份", "EC"), "after": ["s01"]},
        {**_dose("s03", "注液", "电解液 LP57", cap="cap.dose_liquid", params={"volume": 1}), "after": ["s02"]},
    ]
    batch = SimpleNamespace(recipe_snapshot={"steps": steps, "bom": [
        {"material": "EC", "qty": "12.000000", "unit": "g", "source": "plan"},
        {"material": "电解液 LP57", "qty": 3, "unit": "mL"},
    ]})
    service = ConsumptionService(db, system_context("ORG-001", "测试"))
    ec = SimpleNamespace(material="EC", material_id=None, unit="g")

    def command(index: int, wells: dict, capability: str = "cap.dose_solid"):
        return SimpleNamespace(id="c", step_index=index, capability=capability, params={"mass": 1, "wells": wells})

    # 两步分别投 10 g 与 2 g：各和自己下发的量比，不是都和 6 g 比
    assert service._planned(batch, command(0, {"A1": {"mass": 6}, "A2": {"mass": 4}}), ec, "g") == Decimal("10")
    assert service._planned(batch, command(1, {"A1": {"mass": 2}}), ec, "g") == Decimal("2")
    # 中途剔除了一瓶：下发的孔位只剩一瓶，计划量跟着少
    assert service._planned(batch, command(1, {"A1": {}}), ec, "g") == Decimal("1")
    # 流程 BOM 列的物料（每批一份）照旧按声明投它的步骤均分
    lp57 = SimpleNamespace(material="电解液 LP57", material_id=None, unit="mL")
    assert service._planned(batch, command(2, {}, "cap.dose_liquid"), lp57, "mL") == Decimal("3")


def test_absurd_device_quantity_is_refused_not_a_crash(db):
    """设备回报的量超出十进制精度（1e30 mg）：按「无法入账」处理，不让设备回执事务整个失败。"""
    service = ConsumptionService(db, system_context("ORG-001"))
    lot = SimpleNamespace(material_id="", unit="g", material="任意")
    with pytest.raises(ValueError, match="超出可记账的范围"):
        service._to_base(Decimal("1e30"), "mg", lot)
    assert service._to_base(Decimal("360"), "mg", lot) == ("g", Decimal("0.360000"))
