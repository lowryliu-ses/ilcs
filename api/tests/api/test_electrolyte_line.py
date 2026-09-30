"""C 公司电解液产线端到端：导入脚本（scripts/load-electrolyte-line.py）经 TestClient 传输登记整条线，
按参考配方 + 一个变体（LiBF4=0、其余量略改）两瓶导入，走完审批、建批次、执行到完成。

与正式部署的调用序列完全相同，只是执行器由测试逐轮驱动（正式部署是常驻执行器）：
- 43 个步骤全部完成，并行段（准备段两路汇合、测试段两路分叉）顺序正确；
- 每种试剂一条消耗入账、量 = 各瓶之和 = 预留，没有偏差 / 拒绝报警；变体瓶 LiBF4 下发 0，消耗只含参考瓶的量；
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
    assert detail["sop_snapshot"]["code"] == "SOP-ELY-01" and detail["sop_snapshot"]["version"] == "v1"
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

    # 同结构的第二张表（新瓶子）沿用已发布流程，只新建方案
    fresh = (f"ELY-{uid}-03", f"ELY-{uid}-04")
    _, _, second = _table(loader, fresh)
    reused = loader.import_table(team["researcher"], context["template"], f"formula-{uid}-b.csv", second,
                                 {"bottles": 3})
    assert reused["recipe"] == {**reused["recipe"], "id": outcome["recipe"], "reused": True, "state": "released"}
    assert reused["plan"]["id"] != outcome["plan"]
    assert reused["samples"] == [{"id": serial, "created": True} for serial in fresh]
    plan = team["researcher"].get(f"/plans/{reused['plan']['id']}")
    bottles = next(f for f in plan["factors"] if f["target"]["param"] == "bottles")
    assert bottles["levels"] == [3]
    loader.approve_plan(team["researcher"], team["qa"], reused["plan"]["id"])
