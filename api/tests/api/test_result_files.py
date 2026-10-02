"""结果文件接收器：检测软件导出的文件 → 关联检测任务与样本 → 原始文件上传 + 结果回传。走真实 API（TestClient）。"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
DEVICES = ROOT / "devices"  # simulators、connectors 包所在的目录
if str(DEVICES) not in sys.path:
    sys.path.insert(0, str(DEVICES))

CAPACITY = "METRIC-discharge_capacity-v1"
DENSITY = "METRIC-areal_density-v1"


class ClientHttp:
    """接收器的 HTTP 口换成 TestClient：请求照样过认证、授权与校验。"""

    def __init__(self, client, headers: dict):
        self.client = client
        self.headers = headers

    def post_json(self, path: str, payload: dict):
        response = self.client.post(path, json=payload, headers=self.headers)
        return response.status_code, response.json()

    def post_file(self, path: str, filename: str, content: bytes, media_type: str, fields: dict):
        response = self.client.post(path, files={"file": (filename, content, media_type)}, data=fields,
                                    headers=self.headers)
        return response.status_code, response.json()


@pytest.fixture()
def task(operator, researcher, reset_runtime, request):
    sample_id = f"PS-RF-{abs(hash(request.node.name)) % 100000:05d}"
    created = operator.post("/api/samples", {"id": sample_id, "source": "结果文件用例", "sample_type": "极片",
                                             "quantity": "5", "unit": "g"})
    assert created.status_code == 201, created.text
    analysis = researcher.post("/api/analysis-tasks", {
        "physical_sample_id": sample_id, "method": "电性能测试", "method_version": "EC-02 v2",
        "required_metrics": [CAPACITY, DENSITY],
    })
    assert analysis.status_code == 201, analysis.text
    return {"sample": sample_id, "task": analysis.json()["id"]}


def _receiver(tmp_path, client, lims, **overrides):
    from connectors.result_files.receiver import Receiver

    profile = {
        "name": "ec-export", "pattern": r"^(?P<task_id>[^_]+)__(?P<sample_id>[^_]+)__.*\.csv$",
        "format": "csv", "row": "last", "instrument_serial": "EC-TESTER-0001", "parser_version": "ec-csv-1",
        "metrics": {"容量(mAh/g)": {"metric_version_id": CAPACITY, "unit": "mAh/g"},
                    "面密度": {"metric_version_id": DENSITY, "unit": "mg/cm2"}},
        "media_type": "text/csv",
    }
    profile.update(overrides.pop("profile", {}))
    config = {"inbox": str(tmp_path / "inbox"), "settle_sec": 0, "profiles": [profile], **overrides}
    return Receiver(config, http=ClientHttp(client, lims.headers))


def test_export_file_is_parsed_uploaded_ingested_and_archived(tmp_path, client, lims, researcher, task):
    receiver = _receiver(tmp_path, client, lims)
    name = f"{task['task']}__{task['sample']}__run1.csv"
    (receiver.inbox / name).write_text("圈数,容量(mAh/g),面密度\n1,201.0,22.1\n2,205.4,22.6\n", encoding="utf-8")

    assert receiver.run_once()["archived"] == 1
    assert not (receiver.inbox / name).exists() and list(receiver.archive.rglob(name))

    detail = researcher.get(f"/api/analysis-tasks/{task['task']}").json()
    assert detail["state"] == "collected" and detail["missing_metrics"] == []
    values = {row["metric_code"]: row for row in detail["values"]}
    assert float(values["discharge_capacity"]["value"]) == 205.4, "取最后一行"
    assert values["discharge_capacity"]["raw_file_id"], "原始文件已上传并关联"
    assert values["discharge_capacity"]["collected_at"], "采集时间取文件修改时间"
    assert all(row["review_state"] == "pending" for row in detail["values"]), "入账仍待复核，不越过审核"

    # 同一个文件又被导出一次：事件号按内容摘要，回放原结果，不重复入账
    (receiver.inbox / name).write_text("圈数,容量(mAh/g),面密度\n1,201.0,22.1\n2,205.4,22.6\n", encoding="utf-8")
    assert receiver.run_once()["archived"] == 1
    assert len(researcher.get(f"/api/analysis-tasks/{task['task']}").json()["values"]) == 2


def test_rejected_files_move_aside_with_a_reason(tmp_path, client, lims, task):
    receiver = _receiver(tmp_path, client, lims)
    (receiver.inbox / "AT-NOPE__PS-X__a.csv").write_text("容量(mAh/g),面密度\n1,2\n", encoding="utf-8")
    (receiver.inbox / f"{task['task']}__{task['sample']}__b.csv").write_text("容量(mAh/g)\n1\n", encoding="utf-8")
    counts = receiver.run_once()
    assert counts["rejected"] == 2 and counts["archived"] == 0
    reasons = sorted(path.read_text(encoding="utf-8") for path in receiver.rejected.glob("*.reason.txt"))
    assert any("HTTP 404" in reason for reason in reasons), "检测任务不存在：ILCS 明确拒绝"
    assert any("没有列" in reason for reason in reasons), "缺列：文件本身不合格"


def test_files_still_being_written_wait_and_na_is_not_zero(tmp_path, client, lims, researcher, task):
    waiting = _receiver(tmp_path, client, lims, settle_sec=60)
    name = f"{task['task']}__{task['sample']}__na.csv"
    (waiting.inbox / name).write_text("容量(mAh/g),面密度\n205.0,NA\n", encoding="utf-8")
    assert waiting.run_once()["waiting"] == 1
    assert (waiting.inbox / name).exists(), "还在写的文件不碰"

    receiver = _receiver(tmp_path, client, lims)
    assert receiver.run_once()["archived"] == 1
    detail = researcher.get(f"/api/analysis-tasks/{task['task']}").json()
    density = next(row for row in detail["values"] if row["metric_code"] == "areal_density")
    assert density["value"] in (None, "") and "没有给出数值" in (density.get("not_measured_reason") or "")


def test_unmatched_files_are_left_alone(tmp_path, client, lims):
    receiver = _receiver(tmp_path, client, lims)
    (receiver.inbox / "notes.txt").write_text("不是导出文件", encoding="utf-8")
    assert receiver.run_once() == {"archived": 0, "rejected": 0, "waiting": 0, "retry": 0, "ignored": 1}
    assert (receiver.inbox / "notes.txt").exists()


def test_curve_columns_become_a_curve_result_split_by_cycle(tmp_path, client, lims, admin, operator, researcher, reset_runtime):
    """曲线型指标取整列：x、y 两列按「圈数」分成几条；单位行这类不是数的行跳过；同一文件的最后一行照样给数值指标。"""
    from uuid import uuid4

    suffix = uuid4().hex[:6]
    curve = admin.post("/api/metrics", {"code": f"rf_curve_{suffix}", "name": "充放电曲线", "value_type": "series",
                                        "unit": "V", "rules": {"x_label": "比容量", "x_unit": "mAh/g"}})
    assert curve.status_code == 201, curve.text
    sample_id = f"PS-RFC-{suffix}"
    assert operator.post("/api/samples", {"id": sample_id, "source": "曲线用例", "sample_type": "扣电",
                                          "quantity": "1", "unit": "pcs"}).status_code == 201
    analysis = researcher.post("/api/analysis-tasks", {
        "physical_sample_id": sample_id, "method": "充放电", "method_version": "CYC-01",
        "required_metrics": [curve.json()["id"], CAPACITY],
    })
    assert analysis.status_code == 201, analysis.text
    task_id = analysis.json()["id"]
    receiver = _receiver(tmp_path, client, lims, profile={"metrics": {
        "曲线": {"metric_version_id": curve.json()["id"], "unit": "V",
                 "series": {"x": "容量(mAh/g)", "y": "电压(V)", "trace": "圈数"}},
        "容量(mAh/g)": {"metric_version_id": CAPACITY, "unit": "mAh/g"},
    }})
    name = f"{task_id}__{sample_id}__cycle.csv"
    (receiver.inbox / name).write_text(
        "圈数,容量(mAh/g),电压(V)\n-,mAh/g,V\n1,0,4.2\n1,100,3.8\n1,198,3.0\n2,0,4.2\n2,95,3.8\n2,190,3.0\n",
        encoding="utf-8",
    )
    assert receiver.run_once()["archived"] == 1
    detail = researcher.get(f"/api/analysis-tasks/{task_id}").json()
    assert detail["state"] == "collected", detail["missing_metrics"]
    values = {row["metric_definition_id"]: row for row in detail["values"]}
    stored = values[curve.json()["id"]]
    assert stored["series"]["trace_count"] == 2 and stored["series"]["points"] == 6, "单位行跳过，按圈分两条"
    full = researcher.get(f"/api/result-values/{stored['id']}/series").json()
    assert [trace["name"] for trace in full["traces"]] == ["1", "2"] and full["traces"][1]["x"] == [0, 95, 190]
    assert float(values[CAPACITY]["value"]) == 190.0, "数值指标照旧取最后一行"


def test_curve_profile_needs_csv_and_both_columns():
    from connectors.result_files.receiver import Profile

    base = {"name": "x", "pattern": r"(?P<task_id>.+)\.txt", "metrics": {"曲线": {"metric_version_id": "M", "series": {"x": "a"}}}}
    with pytest.raises(ValueError, match="要写"):
        Profile(base)
    with pytest.raises(ValueError, match="只有 csv"):
        Profile({**base, "format": "key_value", "metrics": {"曲线": {"metric_version_id": "M", "series": {"x": "a", "y": "b"}}}})


RETENTION = "METRIC-retention-v1"


def _cycling_records() -> list[dict]:
    """三圈的逐点记录：每个工步容量从 0 重新累计（NewareNDA 的口径）。圈 0 只有静置。"""
    rows = [{"Cycle": 0, "Step": 1, "Status": "Rest", "Voltage": 3.0, "Current(mA)": 0.0,
             "Charge_Capacity(mAh)": 0.0, "Discharge_Capacity(mAh)": 0.0}]
    step = 1
    for cycle, (charge, discharge) in enumerate([(3.6, 3.2), (3.25, 3.2), (3.18, 3.1)], start=1):
        step += 1
        for fraction in (0.0, 0.5, 1.0):  # 恒流充电
            rows.append({"Cycle": cycle, "Step": step, "Status": "CC_Chg", "Voltage": 3.0 + 1.2 * fraction,
                         "Current(mA)": 0.5, "Charge_Capacity(mAh)": charge * 0.9 * fraction,
                         "Discharge_Capacity(mAh)": 0.0, "Charge_Energy(mWh)": charge * 0.9 * fraction * 3.8})
        step += 1
        for fraction in (0.0, 1.0):  # 恒压段：容量从 0 重新累计
            rows.append({"Cycle": cycle, "Step": step, "Status": "CV_Chg", "Voltage": 4.2, "Current(mA)": 0.1,
                         "Charge_Capacity(mAh)": charge * 0.1 * fraction, "Discharge_Capacity(mAh)": 0.0})
        step += 1
        for fraction in (0.0, 0.5, 1.0):
            rows.append({"Cycle": cycle, "Step": step, "Status": "CC_DChg", "Voltage": 4.2 - 1.4 * fraction,
                         "Current(mA)": -0.5, "Charge_Capacity(mAh)": 0.0,
                         "Discharge_Capacity(mAh)": discharge * fraction, "Discharge_Energy(mWh)": discharge * fraction * 3.6})
    return rows


def test_cycle_summary_adds_up_steps_and_derives_efficiency_and_retention():
    from connectors.result_files import cycling

    rows = cycling.cycles(_cycling_records())
    assert [row["cycle"] for row in rows] == [1, 2, 3], "只有静置的圈 0 不列"
    assert rows[0]["charge_mAh"] == pytest.approx(3.6) and rows[0]["discharge_mAh"] == pytest.approx(3.2), "CC + CV 两段相加"
    assert rows[0]["ce_pct"] == pytest.approx(3.2 / 3.6 * 100, rel=1e-4)
    summary = cycling.summary(rows, active_mass_mg=16.0)
    assert summary["cycle_count"] == 3 and summary["first_discharge_mAh"] == pytest.approx(3.2)
    assert summary["retention_pct"] == pytest.approx(3.1 / 3.2 * 100, rel=1e-4)
    assert summary["first_ce_pct"] == pytest.approx(88.8889, rel=1e-4)
    assert summary["mean_ce_pct"] == pytest.approx((3.2 / 3.25 + 3.1 / 3.18) / 2 * 100, rel=1e-4), "第 2 圈起"
    assert summary["first_discharge_mAh_g"] == pytest.approx(200.0), "3.2 mAh ÷ 0.016 g"
    assert cycling.summary([])["retention_pct"] is None, "没有放电就没有保持率，不当 0"
    picked = cycling.select(_cycling_records(), cycles_wanted=[2], statuses=["CC_DChg"])
    assert len(picked) == 3 and {row["Cycle"] for row in picked} == {2}


def test_neware_file_becomes_capacity_retention_and_a_cycle_curve(tmp_path, client, lims, admin, operator, researcher,
                                                                   reset_runtime, monkeypatch):
    """Neware 数据文件：按每圈容量算首圈比容量、保持率，放电容量随圈数成一条曲线；文件原样上传。读文件换成固定记录
    （api/.venv 不带 NewareNDA；真文件的读法在 cycling.read_neware）。"""
    from uuid import uuid4

    from connectors.result_files import cycling, receiver as module

    suffix = uuid4().hex[:6]
    curve = admin.post("/api/metrics", {"code": f"rf_cycles_{suffix}", "name": "放电容量-圈数", "value_type": "series",
                                        "unit": "mAh", "rules": {"x_label": "圈数", "x_unit": "圈"}})
    assert curve.status_code == 201, curve.text
    sample_id = f"PS-NDA-{suffix}"
    assert operator.post("/api/samples", {"id": sample_id, "source": "Neware 数据用例", "sample_type": "扣电",
                                          "quantity": "1", "unit": "pcs"}).status_code == 201
    analysis = researcher.post("/api/analysis-tasks", {
        "physical_sample_id": sample_id, "method": "充放电", "method_version": "CYC-01",
        "required_metrics": [CAPACITY, RETENTION, curve.json()["id"]],
    })
    assert analysis.status_code == 201, analysis.text
    task_id = analysis.json()["id"]
    monkeypatch.setattr(cycling, "read_neware", lambda path: (_cycling_records(), None))
    from app.core.config import settings

    # .ndax 是 zip 包：原始文件要上传，现场得先把这个类型列进 ILCS_FILE_ALLOWED_TYPES（不默认放开二进制）
    monkeypatch.setattr(settings, "file_allowed_types", settings.file_allowed_types + ",application/zip")
    receiver = _receiver(tmp_path, client, lims, profile={
        "name": "neware", "pattern": r"^(?P<task_id>[^_]+)__(?P<sample_id>[^_]+)__.*\.ndax$", "format": "neware",
        "active_mass_mg": 16.0, "parser_version": "neware-nda-1", "media_type": "application/zip",
        "metrics": {
            "first_discharge_mAh_g": {"metric_version_id": CAPACITY, "unit": "mAh/g"},
            "retention_pct": {"metric_version_id": RETENTION, "unit": "%"},
            "放电容量曲线": {"metric_version_id": curve.json()["id"], "unit": "mAh",
                       "series": {"x": "cycle", "y": "discharge_mAh"}},
        },
    })
    assert module.cycling is cycling
    name = f"{task_id}__{sample_id}__21-1-3.ndax"
    (receiver.inbox / name).write_bytes(b"PK\x03\x04 fake ndax bytes")
    assert receiver.run_once()["archived"] == 1
    detail = researcher.get(f"/api/analysis-tasks/{task_id}").json()
    assert detail["state"] == "collected", detail["missing_metrics"]
    values = {row["metric_definition_id"]: row for row in detail["values"]}
    assert float(values[CAPACITY]["value"]) == pytest.approx(200.0)
    assert float(values[RETENTION]["value"]) == pytest.approx(96.875)
    full = researcher.get(f"/api/result-values/{values[curve.json()['id']]['id']}/series").json()
    assert full["traces"][0]["x"] == [1, 2, 3] and full["traces"][0]["y"] == pytest.approx([3.2, 3.2, 3.1])
    assert values[CAPACITY]["raw_file_id"], "原始数据文件已上传并关联"


def test_neware_record_curves_are_filtered_and_thinned():
    from connectors.result_files.receiver import Profile

    profile = Profile({"name": "n", "pattern": r"(?P<task_id>.+)\.ndax", "format": "neware", "metrics": {
        "放电曲线": {"metric_version_id": "M", "unit": "V", "series": {
            "table": "records", "x": "Discharge_Capacity(mAh)", "y": "Voltage", "trace": "Cycle",
            "cycles": [1, 3], "status": ["CC_DChg"], "max_points": 2}},
    }})
    entry = profile.metrics_from({}, [], _cycling_records())[0]
    traces = entry["value"]["traces"]
    assert [trace["name"] for trace in traces] == ["1", "3"], "只取第 1、3 圈的放电段"
    assert all(len(trace["x"]) == 2 for trace in traces) and traces[0]["x"] == [0.0, 3.2], "抽稀保留首尾、0 照收"
    with pytest.raises(ValueError, match="table 只有 neware"):
        Profile({"name": "c", "pattern": r"(?P<task_id>.+)\.csv", "metrics": {
            "x": {"metric_version_id": "M", "series": {"x": "a", "y": "b", "table": "records"}}}})


def test_a_cut_off_last_cycle_and_a_half_first_cycle_do_not_skew_retention():
    """测试中途停了：末圈放电不到前一圈一半，当作没跑完，不进保持率；开头只有半圈放电：基准取第一个完整的圈。"""
    from connectors.result_files import cycling

    rows = [
        {"cycle": 1, "charge_mAh": 0.0, "discharge_mAh": 0.05, "ce_pct": None},   # 只有放电的半圈
        {"cycle": 2, "charge_mAh": 3.3, "discharge_mAh": 3.2, "ce_pct": 96.97},
        {"cycle": 3, "charge_mAh": 3.25, "discharge_mAh": 3.1, "ce_pct": 95.38},
        {"cycle": 4, "charge_mAh": 3.2, "discharge_mAh": 0.4, "ce_pct": 12.5},    # 停在放电中途
    ]
    summary = cycling.summary(rows)
    assert summary["reference_cycle"] == 2 and summary["final_cycle"] == 3 and summary["last_cycle_partial"] == 1
    assert summary["retention_pct"] == pytest.approx(3.1 / 3.2 * 100, rel=1e-4)
    assert summary["mean_ce_pct"] == pytest.approx(95.38) and summary["last_discharge_mAh"] == 0.4, "原样的末圈照给"
    fixed = cycling.summary(rows, reference_cycle=3, incomplete_ratio=0)
    assert fixed["reference_cycle"] == 3 and fixed["final_cycle"] == 4 and fixed["retention_pct"] == pytest.approx(12.9032, rel=1e-4)


@pytest.mark.skipif(not __import__("os").environ.get("NEWARE_SAMPLE"), reason="要设 NEWARE_SAMPLE 指向一个真 .nda / .ndax，且装了 NewareNDA")
def test_a_real_neware_file_reads_and_adds_up():
    """可选：对真文件跑一遍（NewareNDA 仓库 tests/nda 下有样例）。按 (圈, 工步) 取最大再按圈相加，与 pandas 的同一算法逐圈对照。"""
    import os

    pytest.importorskip("NewareNDA")
    from connectors.result_files import cycling

    records, _ = cycling.read_neware(os.environ["NEWARE_SAMPLE"])
    rows = cycling.cycles(records)
    assert rows and all(row["charge_mAh"] >= 0 and row["discharge_mAh"] >= 0 for row in rows)
    import pandas

    frame = pandas.DataFrame(records)
    reference = frame.groupby(["Cycle", "Step"])[[cycling.CHARGE, cycling.DISCHARGE]].max().groupby(level=0).sum()
    reference = reference[(reference[cycling.CHARGE] > 0) | (reference[cycling.DISCHARGE] > 0)]
    assert [row["cycle"] for row in rows] == [int(cycle) for cycle in reference.index]
    for row in rows:
        assert row["discharge_mAh"] == pytest.approx(float(reference.loc[row["cycle"], cycling.DISCHARGE]), abs=1e-5)
