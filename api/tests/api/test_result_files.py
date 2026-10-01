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
