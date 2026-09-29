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
