"""C 公司电解液产线端到端：导入脚本（scripts/load-electrolyte-line.py）经 TestClient 传输登记整条线，
按参考配方 + 一个变体（LiBF4=0、其余量略改）两瓶导入，走完审批、建批次、执行到完成。

与正式部署的调用序列完全相同，只是执行器由测试逐轮驱动（正式部署是常驻执行器）：
- 43 个步骤全部完成，并行段（准备段两路汇合、测试段两路分叉）顺序正确；
- 每种试剂一条消耗入账、量 = 各瓶之和 = 预留，没有偏差 / 拒绝报警；变体瓶 LiBF4 下发 0，消耗只含参考瓶的量；
- 按瓶搅拌：LiBF4 之后的制冷搅拌只下发参考瓶；一瓶都不用做的搅拌（另一张表）不下发、记为跳过；
- 电导率、密度黏度检查点有数值，没有「缺必报项」；
- 运行分配的物理样本就是两个瓶身序列号；每一步落在方法型号对应的工位；
- 一瓶一配方：同结构的第二张表用新瓶子沿用已发布流程；配过液的瓶子再导入被拒绝。
"""
import csv
import importlib.util
import io
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from invariants import assert_consistent

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "load-electrolyte-line.py"


def _loader():
    spec = importlib.util.spec_from_file_location("load_electrolyte_line", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _table(loader, serials: tuple[str, str]) -> tuple[list[str], dict[str, dict[str, Decimal]], bytes]:
    """参考配方表的表头与参考瓶，加一个变体瓶：LiBF4 不加，其余每种多 1%。"""
    rows = list(csv.reader(io.StringIO(loader.FORMULA.read_text(encoding="utf-8"))))
    header, reference = rows[0], rows[1]
    names = [cell.split("(")[0].strip() for cell in header[1:]]
    amounts = {serials[0]: {name: Decimal(value) for name, value in zip(names, reference[1:])}}
    amounts[serials[1]] = {
        name: Decimal(0) if name == "LiBF4" else (value * Decimal("1.01")).quantize(Decimal("0.0001"))
        for name, value in amounts[serials[0]].items()
    }
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(header)
    for serial in serials:
        writer.writerow([serial, *(f"{amounts[serial][name]:f}" for name in names)])
    return names, amounts, out.getvalue().encode("utf-8-sig")


def test_electrolyte_line_runs_end_to_end_through_the_loader(client, reset_runtime, executor, db):
    from app.models import Alarm

    loader = _loader()
    team = loader.actors(loader.client_transport(client))
    line = loader.load_line()
    context = loader.register(team, line)
    # 重复运行不多建：方法、模板都沿用
    again = loader.register(team, line)
    areas = {row["id"]: row["name"] for row in team["engineer"].get("/islands")}
    assert (areas[7], areas[8], areas[9]) == ("物料准备段", "配液段", "测试段")
    assert again["methods"] == context["methods"] and again["template"]["id"] == context["template"]["id"]

    uid = uuid4().hex[:6].upper()
    serials = (f"ELY-{uid}-01", f"ELY-{uid}-02")
    names, amounts, content = _table(loader, serials)
    outcome = loader.run(team, context, f"formula-{uid}.csv", content, {}, pump=executor, rounds=200)
    detail = outcome["detail"]
    batch_id = outcome["batch"]
    assert detail["state"] == "done", detail.get("failure_reason")

    # 43 步全部完成；依赖图上每一步都在它的前驱结束之后才开始
    steps = detail["snapshot"]["steps"]
    assert len(steps) == 43

    # 按配液线 SOP 执行：流程关联 SOP-ELY-01 的生效版本，每个节点都对应到这一版的某一步，批次固化了这一版
    assert detail["sop_snapshot"]["code"] == "SOP-ELY-01" and detail["sop_snapshot"]["version"] == "v2"
    sop_keys = {row["key"]: row["title"] for row in detail["sop_snapshot"]["steps"]}
    assert all(step.get("sop_step_key") in sop_keys for step in steps), [s["name"] for s in steps if not s.get("sop_step_key")]
    by_name = {step["name"]: sop_keys[step["sop_step_key"]] for step in steps}
    assert by_name["EC 预热移液加注"] == "溶剂与预热溶剂加注"
    assert by_name["LiBF4 加料后制冷搅拌"] == "加料后制冷搅拌"
    assert by_name["第 1 瓶拉曼"] == "拉曼检测"
    recipe = team["researcher"].get(f"/recipes/{outcome['recipe']}")
    assert recipe["sop_version_id"] == detail["sop_snapshot"]["sop_version_id"]
    runs = {row["step_id"]: row for row in detail["step_runs"] if row["state"] not in {"superseded", "cancelled"}}
    assert {step["step_id"] for step in steps} == set(runs)
    assert all(row["state"] == "completed" for row in runs.values()), {k: r["state"] for k, r in runs.items()}
    for step in steps:
        for parent in step.get("after") or []:
            assert runs[step["step_id"]]["started_at"] >= runs[parent]["ended_at"], (step["name"], parent)
    by_name = {step["name"]: step for step in steps}
    assert by_name["过渡舱转物料进配液段"]["after"] == [by_name["手动扫码核对瓶身序列号"]["step_id"],
                                                  by_name["机械手取空瓶放到周转位"]["step_id"]]
    # 液体加注从「物料进配液段」分叉，「固体物料到加料位」另走一路，到第一个锂盐加料处汇合（客户流程图）
    solid = by_name["过渡舱转固体物料到加料位"]["step_id"]
    assert by_name["EC 预热移液加注"]["after"] == [by_name["过渡舱转物料进配液段"]["step_id"]]
    assert solid in by_name["LiPF6 称量加料"]["after"]
    assert [step["name"] for step in steps if solid in (step.get("after") or [])] == ["LiPF6 称量加料"]
    aliquot = by_name["电解液分装"]["step_id"]
    assert by_name["母瓶与备用瓶关盖"]["after"] == [aliquot] and by_name["第 1 瓶电导率"]["after"] == [aliquot]
    assert_consistent(batch_id)

    # 运行分配的物理样本 = 两个瓶身序列号（条件顺序 = 表格行顺序）
    samples = sorted(detail["samples"], key=lambda row: row["condition_group"])
    assert [row["physical_sample_id"] for row in samples] == list(serials)
    well_of = {row["physical_sample_id"]: row["well"] for row in samples}

    # 每一步落在方法型号对应的工位
    method_station = {context["methods"][row["key"]]: row["station"] for row in line["methods"]}
    checkpoints = {cp["step_index"]: cp["payload"] for cp in detail["checkpoints"]}
    for index, step in enumerate(steps):
        if step.get("kind") != "device":
            continue
        assert checkpoints[index]["station_id"] == method_station[step["method"]["id"]], step["name"]

    # 消耗：每种试剂一条入账，量 = 各瓶之和 = 预留；变体瓶 LiBF4 下发 0
    reservations = {row["material"]: row for row in detail["reservations"]}
    lot_material = {row["lot_id"]: row["material"] for row in detail["reservations"]}
    consumed: dict[str, list[Decimal]] = {}
    for row in detail["inventory_ledger"]:
        if row["event_type"] == "consume":
            consumed.setdefault(lot_material[row["lot_id"]], []).append(Decimal(row["quantity"]))
    assert set(consumed) == set(names) == set(reservations)
    for name in names:
        total = sum(amounts[serial][name] for serial in serials).quantize(Decimal("0.000001"))
        assert len(consumed[name]) == 1, name
        assert consumed[name][0] == total, (name, consumed[name], total)
        assert Decimal(reservations[name]["qty"]) == total and Decimal(reservations[name]["consumed_qty"]) == total
    libf4 = checkpoints[steps.index(by_name["LiBF4 称量加料"])]
    assert libf4["params"]["wells"][well_of[serials[1]]]["mass"] == 0
    assert libf4["delivered"]["materials"] == [{"material": "LiBF4", "unit": "g", "quantity": 0.36}]
    # 按瓶搅拌：变体瓶没加 LiBF4，LiBF4 之后那次制冷搅拌只下发参考瓶；两瓶都加了的料之后两瓶都搅
    stir = checkpoints[steps.index(by_name["LiBF4 加料后制冷搅拌"])]
    assert stir["params"]["wells"] == {well_of[serials[0]]: {"temp": -10, "time": 60, "rpm": 400}}, stir["params"]
    both = checkpoints[steps.index(by_name["LiPF6 加料后制冷搅拌"])]
    assert set(both["params"]["wells"]) == set(well_of.values())

    # 检测值：电导率、密度黏度都有数值，没有缺必报项；方法输出之外不造数（拉曼只有谱图）
    for name, keys in (("第 1 瓶电导率", ["conductivity_mS_cm"]), ("第 1 瓶密度黏度", ["density_g_cm3", "viscosity_mPa_s"])):
        payload = checkpoints[steps.index(by_name[name])]
        assert all(isinstance(payload["delivered"].get(key), float) for key in keys), payload["delivered"]
        assert not [flag for flag in payload.get("flags") or [] if flag["code"] == "output_missing"]
    flags = [flag for row in runs.values() for flag in row["flags"] or []]
    assert not [flag for flag in flags if flag["code"] in {"output_missing", "output_invalid", "out_of_range"}], flags

    # 没有消耗偏差 / 拒绝、数据越界这类报警
    db.expire_all()
    alarms = db.query(Alarm).filter(Alarm.source_id == batch_id).all()
    assert not [a.message for a in alarms if a.condition_key.startswith(("command:", "data:"))], \
        [a.message for a in alarms]

    # 一瓶一配方：跑过一批的瓶子不能再当空瓶导入——预览列为问题，直接导入也被拒绝
    _, _, used = _table(loader, (serials[0], f"ELY-{uid}-09"))
    with pytest.raises(loader.Failed, match=f"序列号 {serials[0]} 已在批次 {batch_id} 里配过液，不能再作为空瓶导入"):
        loader.import_table(team["researcher"], context["template"], f"formula-{uid}-used.csv", used, {})
    parsed = team["researcher"].upload(f"/formulation-templates/{context['template']['id']}/parse",
                                       f"formula-{uid}-used.csv", used, "text/csv")
    rejected = team["researcher"].post(f"/formulation-templates/{context['template']['id']}/import", {
        "filename": f"formula-{uid}-used.csv", "table": parsed["table"], "params": {},
    }, expect=(422,))
    assert rejected["detail"]["code"] == "sample_unusable", rejected

    # 设备回报的检测值写成检测结果：每瓶 3 个指标（电导率、密度、黏度），记在各自的样本与测出它的工位上，
    # 标明是模拟示意值；导入脚本已由 QA 复核通过并发布报告；闭环训练数据把它们排除
    from app.models import ResultValue
    from app.services.proposal_service import _exclusion

    results = outcome["results"]
    assert len(results) == 6, results
    by_sample: dict[str, dict[str, dict]] = {}
    for row in results:
        by_sample.setdefault(row["physical_sample_id"], {})[row["metric_code"]] = row
    assert set(by_sample) == set(serials)
    for serial, metrics in by_sample.items():
        assert set(metrics) == {"ely_conductivity", "ely_density", "ely_viscosity"}, (serial, metrics)
        assert metrics["ely_conductivity"]["station_id"] == "EL-T-CND"
        assert metrics["ely_density"]["station_id"] == metrics["ely_viscosity"]["station_id"] == "EL-T-DV"
        for row in metrics.values():
            assert row["review_state"] == "approved" and row["quality"] == "valid", row
            assert any(flag["code"] == "simulated" for flag in row["flags"]), row["flags"]
            assert not any(flag["code"] == "batch_level" for flag in row["flags"]), "逐瓶读数，不是批次级复制"
    # 两瓶各测各的：同一指标两瓶的值不同（模拟按指令 + 孔位给值）
    assert by_sample[serials[0]]["ely_conductivity"]["value"] != by_sample[serials[1]]["ely_conductivity"]["value"]
    report = team["qa"].get(f"/reports/{outcome['report']}")
    assert report["state"] == "published", report["state"]
    db.expire_all()
    stored = db.query(ResultValue).filter(ResultValue.id.in_([row["id"] for row in results])).all()
    assert {_exclusion(row) for row in stored} == {"simulated"}

    # 批次跑完、每瓶的检测任务都采集齐：运行分配记为完成，结果分析的「运行分配」与批次列表的样品数都是 2/2
    finished = team["operator"].get(f"/batches/{batch_id}")
    assert {row["state"] for row in finished["samples"]} == {"done"}, [row["state"] for row in finished["samples"]]
    listed = next(row for row in team["researcher"].get("/results") if row["batch_id"] == batch_id)
    assert (listed["sample_done"], listed["sample_count"]) == (2, 2)
    summary = next(row for row in team["operator"].get("/batches") if row["id"] == batch_id)
    assert (summary["sample_done"], summary["sample_count"]) == (2, 2)

    # 回执重放不重复写；同一步重做（新指令）的读数取代上一版、重新待复核，旧版保留

    from app.core.context import system_context
    from app.models import Batch, Command
    from app.services.device_result_service import DeviceResultService

    batch = db.get(Batch, batch_id)
    command = next(row for row in db.query(Command).filter(Command.batch_id == batch_id, Command.station_id == "EL-T-CND").all()
                   if row.type == "dispatch")
    checkpoint = next(row for row in detail["checkpoints"] if row["payload"]["station_id"] == "EL-T-CND")
    step = next(row for row in batch.recipe_snapshot["steps"] if row["name"] == "第 1 瓶电导率")
    service = DeviceResultService(db, system_context(batch.org_id))
    delivered = checkpoint["payload"]["delivered"]
    assert service.record(batch, command, step, delivered, "simulation")["written"] == 0
    rerun = Command(id=f"{command.id[:24]}-rerun", org_id=command.org_id, batch_id=batch_id, station_id=command.station_id,
                    capability=command.capability, params=command.params, method=command.method, type="retry",
                    state="done", delivery_state="delivered", step_index=command.step_index)
    db.add(rerun)
    db.flush()
    assert service.record(batch, rerun, step, delivered, "simulation") == {"written": 2, "samples": 2, "problems": []}
    db.commit()
    db.expire_all()
    for serial in serials:
        rows = sorted((row for row in db.query(ResultValue).filter(ResultValue.physical_sample_id == serial).all()
                       if row.station_id == "EL-T-CND"), key=lambda row: row.result_version)
        assert [row.result_version for row in rows] == [1, 2]
        assert rows[0].superseded_by_id == rows[1].id and rows[1].revises_id == rows[0].id
        assert rows[1].review_state == "pending"

    # 同结构的第二张表（新瓶子）沿用已发布流程，只新建方案
    fresh = (f"ELY-{uid}-03", f"ELY-{uid}-04")
    _, _, second = _table(loader, fresh)
    # 3 瓶要把每瓶分装量降到 15 mL：3 × 15 + 母瓶留样 5 = 50 mL，放得下约 53 mL 母液（3 × 20 mL 放不下，导入不通过）
    reused = loader.import_table(team["researcher"], context["template"], f"formula-{uid}-b.csv", second,
                                 {"bottles": 3, "volume": 15})
    assert reused["recipe"] == {**reused["recipe"], "id": outcome["recipe"], "reused": True, "state": "released"}
    assert reused["plan"]["id"] != outcome["plan"]
    assert reused["samples"] == [{"id": serial, "created": True} for serial in fresh]
    plan = team["researcher"].get(f"/plans/{reused['plan']['id']}")
    bottles = next(f for f in plan["factors"] if f["target"]["param"] == "bottles")
    assert bottles["levels"] == [3]
    loader.approve_plan(team["researcher"], team["qa"], reused["plan"]["id"])


def test_a_stir_no_bottle_needs_is_skipped_not_dispatched(client, reset_runtime, executor, db):
    """A 瓶的 FEC 是它最后一种料、B 瓶不加 FEC：「FEC 加料后制冷搅拌」本批一瓶都不用做——不下发、记为跳过，
    批次照常完成。LiPF6 之后两瓶都还有料，那一次两瓶都搅。"""
    from app.models import Command

    loader = _loader()
    team = loader.actors(loader.client_transport(client))
    context = loader.register(team, loader.load_line())
    uid = uuid4().hex[:6].upper()
    serials = (f"ELY-{uid}-11", f"ELY-{uid}-12")
    content = ("序列号,EC (g),EMC(g),LiPF6(g),FEC(g),LiDFP(g)\n"
               f"{serials[0]},20,40,8,2,0\n{serials[1]},20,40,8,0,0.6\n").encode("utf-8")
    outcome = loader.run(team, context, f"formula-{uid}-skip.csv", content, {}, pump=executor, rounds=200)
    detail = outcome["detail"]
    assert detail["state"] == "done", detail.get("failure_reason")

    steps = detail["snapshot"]["steps"]
    index = {step["name"]: position for position, step in enumerate(steps)}
    assert "LiDFP 加料后制冷搅拌" not in index, "阶段最后一种料加完不搅"
    skipped = steps[index["FEC 加料后制冷搅拌"]]["step_id"]
    runs = {row["step_id"]: row for row in detail["step_runs"] if row["state"] not in {"superseded", "cancelled"}}
    assert runs[skipped]["state"] == "skipped" and "本批没有这样的在用样本" in runs[skipped]["reason"], runs[skipped]
    assert {row["state"] for step_id, row in runs.items() if step_id != skipped} == {"completed"}
    db.expire_all()
    commands = db.query(Command).filter(Command.batch_id == outcome["batch"]).all()
    assert not [row for row in commands if row.step_index == index["FEC 加料后制冷搅拌"]], "跳过的一步不下发"
    wells = {row["well"] for row in detail["samples"]}
    checkpoints = {cp["step_index"]: cp["payload"] for cp in detail["checkpoints"]}
    assert set(checkpoints[index["LiPF6 加料后制冷搅拌"]]["params"]["wells"]) == wells
