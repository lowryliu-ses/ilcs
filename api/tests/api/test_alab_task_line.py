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

import pytest

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


class _Engineer:
    """只答实验区与工位列表、记下改名请求的假账号：实验区登记的规则不用起服务就能核对。"""

    def __init__(self, islands: dict[int, str], stations: dict[str, int]) -> None:
        self.islands = islands
        self.stations = stations
        self.renamed: list[tuple[str, dict]] = []

    def get(self, path: str):
        if path == "/islands":
            return [{"id": key, "name": name} for key, name in self.islands.items()]
        assert path == "/stations", path
        return [{"id": key, "island": island, "retired": False} for key, island in self.stations.items()]

    def put(self, path: str, body: dict) -> None:
        self.renamed.append((path, body))


def test_line_does_not_rename_an_area_that_other_stations_already_use():
    """实验区编号全库共用：line 文件写的编号已经是别的实验区（上面有别的工位）时报错，不改名把那些工位挂到这条线下。"""
    loader = _loader()
    line = loader.load_line(LINE)
    area = line["islands"][0]["id"]
    taken = _Engineer({area: "电池测试区"}, {"ST-NW-01": area, "EL-ALAB": area})
    with pytest.raises(loader.Failed, match="ST-NW-01"):
        loader.register_islands(taken, line)
    assert taken.renamed == []

    # 只有这条线自己的工位（line 文件里改了实验区名称）、或编号还没用过：照常改名 / 登记
    ours = _Engineer({area: "旧名称"}, {"EL-ALAB": area, "ST-NW-01": area + 1})
    loader.register_islands(ours, line)
    fresh = _Engineer({}, {"ST-NW-01": area + 1})
    loader.register_islands(fresh, line)
    assert ours.renamed == fresh.renamed == [(f"/islands/{area}", {"name": "A-Lab 整线（上位机）"})]


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
    assert areas[12] == "A-Lab 整线（上位机）"
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


# ---------- 走真实接入链路：设备仓库 alab-electrolyte 模块的网关 + 假上位机 ----------

MODULE_PACKAGES = {"driver", "simulator"}


def _purge_module_packages() -> dict:
    """设备模块的 driver / simulator 包各模块同名：换模块前把已经导入的拿掉，用完再放回原来的。"""
    import sys

    removed = {name: module for name, module in sys.modules.items() if name.split(".")[0] in MODULE_PACKAGES}
    for name in removed:
        sys.modules.pop(name, None)
    return removed


@pytest.fixture()
def alab_gateway(tmp_path, monkeypatch):
    """本进程里起 alab-electrolyte 模块的网关（HTTPS + 令牌），后面接进程内的假上位机（时间压缩到几秒跑完一个任务）。"""
    import sys

    from app.core.config import settings
    from sim_harness import require_devices

    devices = require_devices()
    module = devices / "gateway" / "alab-electrolyte"
    if not (module / "gateway.py").is_file():
        pytest.skip(f"设备仓库 {devices} 里还没有 alab-electrolyte 模块")
    saved = _purge_module_packages()
    monkeypatch.syspath_prepend(str(devices / "gateway"))
    monkeypatch.syspath_prepend(str(module))
    from ilcs_gateway import serve
    from simulator import simulated_line

    secrets = tmp_path / "secrets"
    monkeypatch.setattr(settings, "adapter_credential_root", str(secrets))
    line = simulated_line(time_scale=0.0004)
    server = serve(line, device_id="SIM-ALAB-01", state_dir=tmp_path / "state", address="127.0.0.1", port=0,
                   token_file=secrets / "SIM-ALAB-01.token", cert=secrets / "SIM-ALAB-01.crt",
                   key=secrets / "SIM-ALAB-01.key", host_name="localhost")
    try:
        yield server, secrets, module
    finally:
        server.stop()
        _purge_module_packages()
        sys.modules.update(saved)


def _patch_adapter(engineer, station_id: str, **changes) -> dict:
    adapter = engineer.get(f"/stations/{station_id}/adapter")
    return engineer.patch(f"/stations/{station_id}/adapter", {
        **changes, "row_version": adapter["row_version"],
        "signature_id": engineer.sign("设备集成配置变更批准", station_id, adapter["row_version"]),
    })


def test_alab_task_runs_through_the_device_module_gateway_and_fake_controller(client, reset_runtime, executor, db,
                                                                               alab_gateway):
    """EL-ALAB 套用模块的接入模板、连到网关：一条整任务指令交给（假）上位机，按瓶回收实加量、读数与谱图。"""
    import json
    import time

    from app.models import Alarm

    server, secrets, module = alab_gateway
    loader = _loader()
    transport = loader.client_transport(client)
    team = loader.actors(transport)
    line = loader.load_line(LINE)
    context = loader.register(team, line)
    engineer, qa = team["engineer"], team["qa"]

    profile = json.loads((module / "profile.json").read_text(encoding="utf-8"))
    imported = [row for row in loader._items(engineer.get("/device-templates"))
                if row["code"] == profile["code"] and row["revision"] == profile["revision"]]
    template = imported[0] if imported else engineer.post(
        "/device-templates/import", {"filename": "profile.json", "document": profile})
    if template["state"] == "draft":
        template = qa.post(f"/device-templates/{template['id']}/release", {
            "row_version": template["row_version"],
            "signature_id": qa.sign("发布设备接入模板", template["id"], template["row_version"]),
        })
    adapter = _patch_adapter(
        engineer, "EL-ALAB", template_id=template["id"],
        template_connection={"base_url": f"https://localhost:{server.port}/api/v1",
                             "ca_file": str(secrets / "SIM-ALAB-01.crt"), "expected_device_id": "SIM-ALAB-01"},
        credential_ref=f"file://{secrets / 'SIM-ALAB-01.token'}",
    )
    assert adapter["driver"] == "http_json_v1", adapter
    try:
        # 执行器自动跑只读级验收：假上位机自报为模拟器，只读级就放行；读得到上位机的配方目录与型号
        for _ in range(20):
            executor()
            gate = engineer.get("/stations/EL-ALAB/adapter/acceptance")["gate"]
            if gate["required"] == "":
                break
        assert gate["required"] == "", gate
        described = engineer.post("/stations/EL-ALAB/adapter/describe")
        assert described.get("reported_model") == "ALAB-ELY-3" and not described.get("warning"), described
        assert {row.get("program") for row in described["methods"]} >= {"ELY-STD"}

        uid = uuid4().hex[:6].upper()
        names, amounts, content = _table(line, uid)
        serials = list(amounts)
        upstream = loader.upstream_for(team, transport, "FT-ELY-02")

        def pump():
            executor()
            time.sleep(0.05)

        outcome = loader.run(team, context, f"formula-alab-gw-{uid}.csv", content, {}, pump=pump, rounds=600,
                             upstream=upstream)
        detail = outcome["detail"]
        assert detail["state"] == "done", detail.get("failure_reason")
        task = next(cp["payload"] for cp in detail["checkpoints"] if cp["step_index"] == 3)
        delivered = task["delivered"]
        assert task["station_id"] == "EL-ALAB" and delivered["alab_task_id"] and delivered["recipe"] == "ELY-STD"
        wells = {row["physical_sample_id"]: row["well"] for row in detail["samples"]}
        rows = delivered["wells"]
        assert {rows[well]["bottle"] for well in rows} == set(serials), "上位机按瓶身序列号认瓶、按瓶回报"
        # 每瓶的实加量就是这瓶的配方（±0.3 % 以内）；某瓶某种料是 0 就没加
        for serial, well in wells.items():
            dosed = rows[well]["dosed"]
            for name in names:
                target = float(amounts[serial][name])
                if target == 0:
                    assert name not in dosed, (serial, name)
                else:
                    assert abs(dosed[name] - target) <= target * 0.003 + 0.0001, (serial, name, dosed[name], target)
            assert len(rows[well]["raman_spectrum"]["y"]) > 100
        readings = {serial: rows[well]["conductivity_mS_cm"] for serial, well in wells.items()}
        assert len(set(readings.values())) == len(serials), "各瓶按自己的组分算，读数不一样"

        # 消耗按上位机回报的实加量逐种入账（与预留差零点几毫克的部分自动追加预留），没有被拒的
        consumed = {row["material"]: float(row["consumed_qty"]) for row in detail["reservations"]}
        for name in names:
            actual = sum(rows[well]["dosed"].get(name, 0) for well in rows)
            assert abs(consumed[name] - actual) < 1e-6, (name, consumed[name], actual)
        db.expire_all()
        alarms = db.query(Alarm).filter(Alarm.source_id == detail["id"]).all()
        assert not [a.message for a in alarms if "消耗被拒" in a.message or a.condition_key.startswith("data:")], \
            [a.message for a in alarms]
        results = [row for row in outcome["progress"]["results"] if row["metric"] != "ely_raman"]
        assert len(results) == 3 * len(serials) and all(row["official"] for row in results)
        # 执行中看得到进展：在途回执里的进度一路落库（值没变的不重复记），最后到 100
        from app.models import Command, Telemetry

        command = db.query(Command).filter(Command.batch_id == detail["id"], Command.step_index == 3,
                                           Command.type == "dispatch").one()
        rows = db.query(Telemetry).filter(Telemetry.command_id == command.id, Telemetry.metric == "progress").all()
        live = [row.value for row in sorted((row for row in rows if row.received_at), key=lambda row: row.received_at)]
        assert len(live) >= 3 and live == sorted(live), live
        assert len(live) == len(set(live)), "值没变的不重复记"
        assert max(row.value for row in rows) == 100
    finally:
        _patch_adapter(engineer, "EL-ALAB", kind="simulation", driver="simulation", protocol="内置模拟",
                       config={"simulate_outputs": True}, credential_ref="", template_id="")
