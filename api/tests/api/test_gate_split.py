"""质检关卡与样本拆分：在批次快照末尾追加节点，用模拟设备回执控制测量值。"""
import pytest

from test_failure_paths import running_batch  # noqa: F401  （复用 fixture）


@pytest.fixture()
def measured(monkeypatch):
    """模拟设备回执里的测量值：按投递顺序依次取值，取完沿用最后一个。"""
    from app.adapters.simulation import SimulationAdapter

    values: dict[str, list] = {}
    original = SimulationAdapter.submit

    def submit(self, request):
        result = original(self, request)
        queue = values.get(request.step_id)
        if not queue:
            return result
        extra = queue.pop(0) if len(queue) > 1 else queue[0]
        from dataclasses import replace

        patched = replace(result, delivered={**result.delivered, **extra})
        self._ledger[request.command_id] = patched
        return patched

    monkeypatch.setattr(SimulationAdapter, "submit", submit)
    return values


def _append(batch_id: str, *steps: dict) -> None:
    from app.core.db import SessionLocal
    from app.models import Batch

    with SessionLocal() as db:
        batch = db.get(Batch, batch_id)
        snapshot = dict(batch.recipe_snapshot)
        snapshot["steps"] = [*snapshot["steps"], *steps]
        batch.recipe_snapshot = snapshot
        db.commit()


def _gate(**overrides) -> dict:
    gate = {"source_step_id": "s04", "field": "water_ppm", "max": 20, "scope": "batch", "on_fail": "hold"}
    gate.update(overrides)
    return {"step_id": "s05", "kind": "gate", "name": "水分质检", "gate": gate}


def _run(operator, batch_id, executor, rounds=16):
    for _ in range(rounds):
        executor()
    return operator.get(f"/api/batches/{batch_id}").json()


def _gate_run(detail):
    return [row for row in detail["step_runs"] if row["kind"] == "gate"]


def test_gate_passes_when_measurement_in_range(operator, running_batch, executor, measured):
    measured["s04"] = [{"water_ppm": 12}]
    _append(running_batch, _gate())
    detail = _run(operator, running_batch, executor)
    assert detail["state"] == "done"
    assert _gate_run(detail)[0]["state"] == "completed"


def test_gate_reworks_then_hands_over_to_qa(operator, qa, running_batch, executor, measured):
    """不合格返工一次仍不合格：转 QA 判定；QA 签名放行后流程结束。"""
    measured["s04"] = [{"water_ppm": 35}]
    _append(running_batch, _gate(on_fail="rework", rework_to="s04", max_rework=1))
    detail = _run(operator, running_batch, executor)
    assert detail["state"] == "paused"
    s04_runs = [row for row in detail["step_runs"] if row["step_id"] == "s04"]
    assert [row["state"] for row in s04_runs] == ["superseded", "completed"], "返工重做测量来源步骤，旧结论作废保留"
    gates = _gate_run(detail)
    assert [row["state"] for row in gates] == ["failed", "ready"]

    pending = gates[-1]
    denied = operator.post(f"/api/step-runs/{pending['id']}/gate-decision", {
        "conclusion": "approved", "reason": "试试", "signature_id": operator.sign("判定", target=pending["id"]),
    })
    assert denied.status_code == 403, "质检判定只允许 QA"
    decided = qa.post(f"/api/step-runs/{pending['id']}/gate-decision", {
        "conclusion": "approved", "reason": "复测 KF 18 ppm，偏差来自取样，放行",
        "signature_id": qa.sign("质检判定属实", target=pending["id"]),
    })
    assert decided.status_code == 200, decided.text
    assert operator.get(f"/api/batches/{running_batch}").json()["state"] == "done"


def test_gate_scrap_faults_batch_and_fails_samples(operator, running_batch, executor, measured):
    measured["s04"] = [{"water_ppm": 50}]
    _append(running_batch, _gate(on_fail="scrap"))
    detail = _run(operator, running_batch, executor)
    assert detail["state"] == "fault"
    assert "报废" in detail["failure_reason"]
    assert all(sample["state"] == "failed" for sample in detail["samples"])


def test_missing_measurement_never_passes(operator, running_batch, executor):
    _append(running_batch, _gate(on_fail="rework", rework_to="s04", max_rework=2))
    detail = _run(operator, running_batch, executor)
    assert detail["state"] == "paused", "取不到数值不等于合格，转人工判断"
    assert "无法判定" in _gate_run(detail)[-1]["reason"]


def test_sample_scope_gate_rejects_individual_wells(operator, running_batch, executor, measured):
    detail = operator.get(f"/api/batches/{running_batch}").json()
    wells = [sample["well"] for sample in detail["samples"]]
    bad = wells[:2]
    measured["s04"] = [{"wells": {well: {"loading": 25.0 if well in bad else 18.0} for well in wells}}]
    _append(running_batch, _gate(field="loading", min=17, max=19, scope="sample"))
    detail = _run(operator, running_batch, executor)
    assert detail["state"] == "done", "部分剔除后其余样本继续"
    states = {sample["well"]: sample["state"] for sample in detail["samples"]}
    assert all(states[well] == "failed" for well in bad)
    assert all(states[well] != "failed" for well in wells if well not in bad)


def test_split_creates_children_with_lineage(operator, running_batch, executor, db):
    from app.models import PhysicalSample, Sample

    _append(running_batch, {"step_id": "s05", "kind": "split", "name": "分装扣电",
                            "split": {"count": 3, "child_type": "扣电"}})
    detail = _run(operator, running_batch, executor)
    assert detail["state"] == "done"
    parents = [s for s in db.query(Sample).filter(Sample.batch_id == running_batch) if s.state == "split"]
    children = [s for s in db.query(Sample).filter(Sample.batch_id == running_batch) if "-" in s.well]
    assert parents and len(children) == 3 * len(parents)
    child = db.get(PhysicalSample, children[0].physical_sample_id)
    assert child.parent_id == parents[0].physical_sample_id or child.parent_id in {
        p.physical_sample_id for p in parents
    }, "子样本谱系指向母样"
    assert child.sample_type == "扣电"
    assert {c.condition_group for c in children} <= {p.condition_group for p in parents}, "继承条件分组"


def test_recipe_validation_rejects_bad_gate_config():
    from app.domain.recipe_rules import validate_steps

    steps = [
        {"step_id": "s01", "kind": "manual", "name": "配液", "dur": 5,
         "form": [{"key": "v", "label": "v", "type": "number"}]},
        {"step_id": "s02", "kind": "gate", "name": "质检",
         "gate": {"source_step_id": "s01", "field": "", "on_fail": "rework", "rework_to": "s09"}},
    ]
    issues = validate_steps(steps, [], {})[1]["issues"]
    joined = "；".join(issues)
    assert "设备步骤" in joined and "测量字段" in joined and "下限或上限" in joined
    assert "返工必须回到关卡之前" in joined
