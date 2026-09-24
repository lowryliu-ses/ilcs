"""数据质量：越界值入库打标、前后逻辑规则打标或拒收、结果带设备列、设备输出对照方法规则、遥测归属。"""
import pytest

from app.models import Alarm, StepRun, Telemetry
from test_graph_workflow import _dispatch, _graph_batch, _run

CAPACITY = "METRIC-discharge_capacity-v1"
DENSITY = "METRIC-areal_density-v1"


@pytest.fixture()
def task(operator, researcher, reset_runtime, request):
    sample_id = f"PS-DQ-{abs(hash(request.node.name)) % 100000:05d}"
    assert operator.post("/api/samples", {"id": sample_id, "source": "数据质量用例", "sample_type": "极片",
                                          "quantity": "5", "unit": "g"}).status_code == 201
    created = researcher.post("/api/analysis-tasks", {
        "physical_sample_id": sample_id, "method": "电性能测试", "method_version": "EC-02 v2",
        "required_metrics": [CAPACITY, DENSITY],
    })
    assert created.status_code == 201, created.text
    return created.json()["id"]


def test_out_of_range_value_is_stored_flagged_and_suspect(lims, researcher, task):
    response = lims.post("/api/integrations/results", {
        "event_id": "dq-range", "task_id": task, "station_id": "ST-07", "instrument_serial": "EC-TESTER-0001",
        "metrics": [{"metric_version_id": CAPACITY, "value": 450.0, "unit": "mAh/g"}],
    })
    assert response.status_code == 200, response.text
    result = response.json()["results"][0]
    assert result["quality"] == "suspect" and result["flags"][0]["code"] == "out_of_range"
    value = researcher.get(f"/api/analysis-tasks/{task}").json()["values"][0]
    assert value["station_id"] == "ST-07" and value["instrument"] == "EC-TESTER-0001"
    assert value["flags"] and not value["official"]


def test_unknown_station_is_refused(lims, task):
    response = lims.post("/api/integrations/results", {
        "event_id": "dq-station", "task_id": task, "station_id": "ST-NOPE",
        "metrics": [{"metric_version_id": CAPACITY, "value": 200.0, "unit": "mAh/g"}],
    })
    assert response.status_code == 404


def test_logic_rules_flag_or_reject(admin, lims, researcher, task):
    flag = admin.post("/api/metrics/data-rules", {
        "name": "比容量不超过面密度 × 10", "left_metric": "discharge_capacity", "op": "<=",
        "right_metric": "areal_density", "factor": 10,
    })
    reject = admin.post("/api/metrics/data-rules", {
        "name": "面密度必须大于 1", "left_metric": "areal_density", "op": ">", "right_value": 1, "severity": "reject",
    })
    assert flag.status_code == 201 and reject.status_code == 201, (flag.text, reject.text)
    try:
        refused = lims.post("/api/integrations/results", {
            "event_id": "dq-logic-1", "task_id": task,
            "metrics": [{"metric_version_id": DENSITY, "value": 0.5, "unit": "mg/cm2"}],
        })
        assert refused.status_code == 409 and "逻辑冲突（拒收）" in refused.text

        first = lims.post("/api/integrations/results", {
            "event_id": "dq-logic-2", "task_id": task,
            "metrics": [{"metric_version_id": DENSITY, "value": 15.0, "unit": "mg/cm2"}],
        })
        assert first.status_code == 200 and first.json()["results"][0]["flags"] == []
        # 200 > 15 × 10：后到的比容量被打标（逻辑冲突），先到的面密度不动
        second = lims.post("/api/integrations/results", {
            "event_id": "dq-logic-3", "task_id": task,
            "metrics": [{"metric_version_id": CAPACITY, "value": 200.0, "unit": "mAh/g"}],
        })
        assert second.status_code == 200, second.text
        flagged = second.json()["results"][0]
        assert flagged["quality"] == "suspect" and flagged["flags"][0]["code"] == "logic"
        assert flagged["flags"][0]["rule_id"] == flag.json()["id"]
    finally:
        for rule in (flag.json(), reject.json()):
            admin.patch(f"/api/metrics/data-rules/{rule['id']}", {"enabled": False, "row_version": rule["row_version"]})


def test_device_outputs_against_method_rules_and_telemetry_attribution(operator, db, reset_runtime, executor):
    def with_method(steps):
        first = dict(steps[0])
        first["method"] = {
            "id": "dm-test", "code": "DM-T", "version": 1, "name": "干燥", "program": "VD",
            "outputs": [{"key": "temp", "label": "箱温", "hi": 100}, {"key": "moisture", "label": "水分", "required": True}],
        }
        return [first, *steps[1:]]

    batch_id = _graph_batch(operator, db, with_method)
    _dispatch(operator, batch_id)
    detail = _run(operator, batch_id, executor)
    assert detail["state"] == "done", detail["failure_reason"]
    db.expire_all()
    run = db.query(StepRun).filter(StepRun.batch_id == batch_id, StepRun.step_index == 0).first()
    codes = {row["code"] for row in run.flags}
    assert codes == {"out_of_range", "output_missing"}, run.flags
    alarm = db.query(Alarm).filter(Alarm.condition_key.like(f"data:{batch_id}:%")).first()
    assert alarm is not None and alarm.category == "data"

    points = db.query(Telemetry).filter(Telemetry.batch_id == batch_id).all()
    assert points and all(point.step_id and point.command_id and point.step_index is not None for point in points)
    assert points[0].operator
