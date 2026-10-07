"""运行时：等待期间样本留在设备里、人工步骤占着工位——节点记下占的是哪台，执行器数设备占用时算上它。"""
from app.core.context import system_context
from app.models import Command
from app.services.execution_service import ExecutionService
from test_graph_workflow import _dispatch, _graph_batch


def _open_runs(operator, batch_id: str, executor, step_id: str, rounds: int = 40) -> dict:
    for _ in range(rounds):
        executor()
        runs = operator.get(f"/api/batches/{batch_id}").json()["step_runs"]
        opened = [row for row in runs if row["step_id"] == step_id]
        if opened:
            return opened[-1]
    raise AssertionError(f"{step_id} 一直没有开出")


def test_holding_wait_takes_the_device_it_waits_in(operator, reset_runtime, db, executor):
    def holding(steps):
        dry = {**steps[0], "step_id": "s01"}
        cool = {"step_id": "s02", "name": "炉内冷却", "kind": "wait", "dur": 60,
                "wait_for": {"mode": "duration"}, "resource": {"holds_station": True}}
        again = {**steps[0], "step_id": "s03", "name": "冷却后复烘"}
        return [dry, cool, again]

    batch_id = _graph_batch(operator, db, holding)
    _dispatch(operator, batch_id)
    detail = operator.get(f"/api/batches/{batch_id}").json()
    windows = [row for row in detail["allocations"] if row["kind"] == "work"]
    dry_window = next(row for row in windows if row["step_index"] == 0)
    cool_window = next(row for row in windows if row["step_index"] == 1)
    assert cool_window["station_id"] == dry_window["station_id"]
    assert cool_window["starts_at"] == dry_window["ends_at"], "等待紧接着设备步骤，同一台工位"

    cool = _open_runs(operator, batch_id, executor, "s02")
    assert cool["station_id"] == dry_window["station_id"]

    # 别的批次要在这台设备上动作：样本还在里面，不能投递；本批次冷却之后的那一步不受它挡（它本来就等冷却结束）
    service = ExecutionService(db, system_context("ORG-001"))
    other = Command(station_id=cool["station_id"], batch_id="B-OTHER", capability="cap.vacuum_dry", type="dispatch",
                    step_index=0, units=1)
    mine = Command(station_id=cool["station_id"], batch_id=batch_id, capability="cap.vacuum_dry", type="dispatch",
                   step_index=2, units=1)
    assert service._occupied(other)
    assert not service._occupied(mine)
    db.rollback()


def test_manual_step_on_a_named_station_holds_it_while_open(operator, reset_runtime, db, executor):
    def manual_at_station(steps):
        return [{"step_id": "s01", "name": "人工装样", "kind": "manual", "dur": 30,
                 "form": [{"key": "ok", "label": "已装样", "type": "bool"}], "resource": {"station": "ST-06"}}]

    batch_id = _graph_batch(operator, db, manual_at_station)
    _dispatch(operator, batch_id)
    windows = [row for row in operator.get(f"/api/batches/{batch_id}").json()["allocations"] if row["kind"] == "work"]
    assert [(row["step_index"], row["station_id"]) for row in windows] == [(0, "ST-06")]

    run = _open_runs(operator, batch_id, executor, "s01")
    assert run["station_id"] == "ST-06"
    service = ExecutionService(db, system_context("ORG-001"))
    other = Command(station_id="ST-06", batch_id="B-OTHER", capability="cap.assemble", type="dispatch",
                    step_index=0, units=1)
    assert service._occupied(other)
    db.rollback()
