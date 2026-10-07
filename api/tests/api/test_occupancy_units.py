"""执行器数占用与排程同一口径（2026-10-07 通用中控评审 F1 与共享资产占用）。

- 按样本计通道的协同工位上，一条动作占在用样本数那么多份，不是 1 份——否则前一批还没做完，
  提前 15 分钟投递的后一批就能挤进来；
- 人工步骤、等待期间占着工位的份数：按样本计的工位按那批的在用样本数算，资产这一层也算；
- 本批次的占位只在它是这条动作的前驱时不算，并行分支上的照样算（排程也是把两者串开的）。
"""
from app.core.context import system_context
from app.models import Asset, Command, Station, StepRun
from app.repositories.batches import SampleRepository
from app.services.execution_service import ExecutionService
from test_graph_workflow import _graph_batch


def _service(db) -> ExecutionService:
    return ExecutionService(db, system_context("ORG-001"))


def _incoming(station_id: str, units: int = 1, batch_id: str = "B-OTHER", step_index: int = 0) -> Command:
    return Command(station_id=station_id, batch_id=batch_id, capability="cap.vacuum_dry", type="dispatch",
                   step_index=step_index, units=units)


def _holding(db, batch_id: str, station_id: str, step_id: str = "s99", kind: str = "manual") -> None:
    db.add(StepRun(org_id="ORG-001", batch_id=batch_id, step_id=step_id, step_index=0, kind=kind,
                   state="running", station_id=station_id))
    db.flush()


def test_a_per_sample_assist_station_counts_samples_not_one(operator, reset_runtime, db):
    batch_id = _graph_batch(operator, db, lambda steps: steps[:1])
    helper = db.get(Station, "AGV-01")
    helper.channel_unit, helper.channels, helper.asset_id = "sample", 8, ""
    db.add(Command(station_id="ST-05", batch_id=batch_id, capability="cap.vacuum_dry", type="dispatch",
                   step_index=0, units=5, assist_station_ids=["AGV-01"], state="running"))
    db.flush()
    try:
        assert _service(db)._occupied(_incoming("AGV-01", units=5)), "协同工位上那条动作占 5 份：5 + 5 > 8"
        assert not _service(db)._occupied(_incoming("AGV-01", units=3)), "5 + 3 = 8 放得下"
    finally:
        db.rollback()


def test_a_holding_step_counts_samples_on_a_per_sample_station(operator, reset_runtime, db):
    other = _graph_batch(operator, db, lambda steps: steps[:1])
    samples = len(SampleRepository(db, system_context("ORG-001")).active_for_batch(other))
    assert samples > 1
    station = db.get(Station, "ST-06")
    station.channel_unit, station.channels, station.asset_id = "sample", samples + 1, ""
    _holding(db, other, "ST-06")
    try:
        assert _service(db)._occupied(_incoming("ST-06", units=2)), f"人工步骤占 {samples} 份：{samples} + 2 > {samples + 1}"
        assert not _service(db)._occupied(_incoming("ST-06", units=1))
    finally:
        db.rollback()


def test_a_holding_step_on_one_station_loads_the_shared_asset(operator, reset_runtime, db):
    other = _graph_batch(operator, db, lambda steps: steps[:1])
    shared = db.get(Station, "ST-06").asset_id
    assert shared and int(db.get(Asset, shared).capacity or 1) == 1
    db.get(Station, "ST-05").asset_id = shared
    _holding(db, other, "ST-06")
    try:
        assert _service(db)._occupied(_incoming("ST-05")), "ST-06 上的人工步骤占着两台工位共用的那台资产的唯一一份"
    finally:
        db.rollback()


def test_only_predecessor_holds_of_the_same_batch_are_exempt(operator, reset_runtime, db):
    def graph(steps):
        dry = {**steps[0], "step_id": "s01", "after": []}
        cool = {"step_id": "s02", "name": "炉内冷却", "kind": "wait", "dur": 60, "after": ["s01"],
                "wait_for": {"mode": "duration"}, "resource": {"holds_station": True}}
        side = {**steps[0], "step_id": "s03", "name": "旁路烘干", "after": ["s01"]}
        later = {**steps[0], "step_id": "s04", "name": "冷却后复烘", "after": ["s02"]}
        return [dry, cool, side, later]

    batch_id = _graph_batch(operator, db, graph)
    db.get(Station, "ST-05").asset_id = ""
    _holding(db, batch_id, "ST-05", step_id="s02", kind="wait")
    try:
        assert not _service(db)._occupied(_incoming("ST-05", batch_id=batch_id, step_index=3)), \
            "冷却之后的那一步本来就要等冷却结束：本批次自己的前驱占位不挡它"
        assert _service(db)._occupied(_incoming("ST-05", batch_id=batch_id, step_index=2)), \
            "并行分支上的那一步不等冷却：样本还在设备里，同一批次的它也得等"
    finally:
        db.rollback()
