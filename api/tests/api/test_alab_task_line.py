"""C 公司 A-Lab 电解液产线的整任务方式端到端：导入脚本按 line-alab.json 登记（上位机工位 EL-ALAB、整任务能力
cap.ely.run、配液模板 FT-ELY-02 的 task 写法、SOP-ELY-02），配方表由模拟的上游系统用服务身份提交，
人照旧评审、批准、签名下发，执行器把整份任务交给上位机（这里是内置模拟），跑完上游按请求编号查进度与结果。

- 流程只有 4 步：3 个人工节点 + 1 个整任务步骤，它按加料顺序一步投完全部组分（materials 占用加料位 m01…）；
- 下发给设备的请求带全部物料（按加料顺序）与每个孔位上的瓶身序列号；
- 每种料一条消耗入账，量 = 各瓶之和 = 预留，没有偏差 / 拒绝报警；
- 每瓶 3 个数值指标 + 1 条拉曼谱写成检测结果，复核后进正式统计（标明是模拟示意值）；
- 上游系统看得到方案已批准、批次完成、每瓶的结果与报告。
"""
import csv
import importlib.util
import io
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "load-electrolyte-line.py"
LINE = ROOT / "scripts" / "lines" / "c-electrolyte" / "line-alab.json"


def _loader():
    spec = importlib.util.spec_from_file_location("load_electrolyte_line_alab", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _table(line: dict, uid: str) -> tuple[list[str], dict[str, dict[str, Decimal]], bytes]:
    """产线定义里的缺省配方表（3 瓶），瓶身序列号加上本次的后缀：一瓶一配方，同一会话里不撞号。"""
    path = Path(line["_path"]).parent / line["formula"]
    rows = list(csv.reader(io.StringIO(path.read_text(encoding="utf-8"))))
    header, body = rows[0], rows[1:]
    names = [cell.split("(")[0].strip() for cell in header[1:]]
    amounts = {f"{row[0]}-{uid}": {name: Decimal(value) for name, value in zip(names, row[1:])} for row in body}
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(header)
    for serial, values in amounts.items():
        writer.writerow([serial, *(f"{values[name]:f}" for name in names)])
    return names, amounts, out.getvalue().encode("utf-8-sig")


def test_alab_task_line_runs_one_task_step_submitted_by_the_upstream_system(client, reset_runtime, executor, db):
    from app.models import Alarm, Batch, Command
    from app.core.context import system_context
    from app.services.execution_service import ExecutionService

    loader = _loader()
    transport = loader.client_transport(client)
    team = loader.actors(transport)
    line = loader.load_line(LINE)
    context = loader.register(team, line)
    areas = {row["id"]: row["name"] for row in team["engineer"].get("/islands")}
    assert areas[10] == "A-Lab 整线（上位机）"
    template = team["researcher"].get(f"/formulation-templates/{context['template']['id']}")
    assert template["code"] == "FT-ELY-02" and template["check"]["ok"], template["check"]

    uid = uuid4().hex[:6].upper()
    names, amounts, content = _table(line, uid)
    serials = list(amounts)
    upstream = loader.upstream_for(team, transport, "FT-ELY-02")
    outcome = loader.run(team, context, f"formula-alab-{uid}.csv", content, {}, pump=executor, rounds=200,
                         upstream=upstream)
    detail = outcome["detail"]
    batch_id = outcome["batch"]
    assert detail["state"] == "done", detail.get("failure_reason")

    # 4 步：人工上料、分装物料、扫码核对，然后整任务一步投完全部组分（按加料顺序占用加料位）
    steps = detail["snapshot"]["steps"]
    assert [step["name"] for step in steps] == ["过渡舱载入空瓶", "手动分装物料", "手动扫码核对瓶身序列号",
                                                "A-Lab 上位机执行实验任务"]
    task = steps[-1]
    assert [row["material"] for row in task["materials"]] == names, "液体（表格列顺序）在前、锂盐与添加剂在后"
    assert [row["param"] for row in task["materials"]] == [f"m{index:02d}" for index in range(1, 13)]
    assert detail["sop_snapshot"]["code"] == "SOP-ELY-02"

    # 下发给设备的请求：全部物料按加料顺序、每个孔位上的瓶身序列号、每瓶自己的用量
    db.expire_all()
    command = db.query(Command).filter(Command.batch_id == batch_id, Command.step_index == 3,
                                       Command.type == "dispatch").one()
    batch = db.get(Batch, batch_id)
    hooks = ExecutionService(db, system_context(batch.org_id, "测试"))._step_hooks(batch, command)
    assert [row["name"] for row in hooks["materials"]] == names and "material" not in hooks
    assert all(row["unit"] == "g" for row in hooks["materials"])
    wells = {row["physical_sample_id"]: row["well"] for row in detail["samples"]}
    assert hooks["samples"] == {well: serial for serial, well in wells.items()}
    per_well = command.params["wells"]
    for serial, well in wells.items():
        assert {row["name"]: per_well[well][row["param"]] for row in hooks["materials"]} == {
            name: float(amounts[serial][name]) if amounts[serial][name] % 1 else int(amounts[serial][name])
            for name in names
        }
        assert per_well[well]["bottles"] == 2 and per_well[well]["volume"] == 20

    # 消耗：每种料一条入账，量 = 各瓶之和 = 预留；没有偏差 / 拒绝报警
    reservations = {row["material"]: row for row in detail["reservations"]}
    for name in names:
        total = sum(values[name] for values in amounts.values()).quantize(Decimal("0.000001"))
        assert Decimal(reservations[name]["qty"]) == total and Decimal(reservations[name]["consumed_qty"]) == total, name
    alarms = db.query(Alarm).filter(Alarm.source_id == batch_id).all()
    assert not [a.message for a in alarms if a.condition_key.startswith(("command:", "data:"))], \
        [a.message for a in alarms]

    # 每瓶 3 个数值指标 + 1 条拉曼谱，复核通过；报告已发布
    results = outcome["results"]
    assert len(results) == 4 * len(serials), [row.get("metric_code") for row in results]
    assert {row["metric_code"] for row in results} == {"ely_conductivity", "ely_density", "ely_viscosity", "ely_raman"}
    assert all(row["station_id"] == "EL-ALAB" and row["review_state"] == "approved" for row in results)
    assert team["qa"].get(f"/reports/{outcome['report']}")["state"] == "published"

    # 上游系统按请求编号查到的进度：方案已批准、批次完成，每瓶的结果进正式统计（标明模拟示意值）
    progress = outcome["progress"]
    assert progress["plan"]["approval_state"] == "approved"
    assert [row["state"] for row in progress["batches"]] == ["done"]
    assert progress["samples"] == serials
    assert len(progress["results"]) == 4 * len(serials)
    assert all(row["official"] and row["simulated"] for row in progress["results"]), progress["results"]
    spectra = [row for row in progress["results"] if row["metric"] == "ely_raman"]
    assert all(row["value"] is None and row["series_points"] > 0 for row in spectra)
    assert progress["reports"], "报告挂在批次上，上游看得到"
    # 完整的谱图按结果编号取；别的结果编号（不属于这次提交）一律 404
    base = f"/runtime/formulation-templates/FT-ELY-02/imports/{progress['request_id']}/results"
    spectrum = upstream.call("GET", f"{base}/{spectra[0]['result_id']}/series")
    assert spectrum["metric_code"] == "ely_raman" and spectrum["x_label"] == "拉曼位移"
    assert sum(len(trace["y"]) for trace in spectrum["traces"]) == spectra[0]["series_points"]
    status, _ = transport("GET", f"{base}/no-such-result/series", None, None,
                          {"X-Service-Source": upstream.source, "X-Service-Secret": upstream.secret})
    assert status == 404
