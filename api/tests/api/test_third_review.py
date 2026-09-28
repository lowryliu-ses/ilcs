"""核心链路三次评审（2026-09-28）回归：评审 T01–T08 与补充复核 A01–A24。

每条用例写的是目标行为。对应工作包修复之前标为 xfail(strict=True)：修好后用例转为通过，strict 模式
会提醒去掉标记。场景来自评审附件与补充复核的复现脚本（output/reviews/2026-09-28）。
排程类的服务层用例在 test_third_review_scheduling.py，纯领域用例在 tests/domain/test_schedule_consistency.py。
"""
import os
import subprocess
import uuid
from uuid import uuid4

import pytest

from invariants import assert_consistent
from test_core_chain_review import scripted  # noqa: F401
from test_second_review import (  # noqa: F401
    _commands, _detail, _dispatch, _new_batch, _placements, _register, _reshape, _session, _split_step,
    _target_factor, _verify, _with_robot, clean_labware, devices, measured, running_batch,
)
from test_automation_extensions import _run
from test_core_chain_review import _resume
from test_schema_guard import scratch_database  # noqa: F401
from test_sop_controls import PDF, approved_plan, publish_sop, released_recipe
from test_workflow import single_condition_task  # noqa: F401


def pending(package: str):
    return pytest.mark.xfail(strict=True, reason=f"{package} 待修")


# ---------- 工具 ----------


def _assist_station(operator, batch_id: str, step_index: int) -> str:
    return next(
        row["station_id"] for row in _detail(operator, batch_id)["allocations"]
        if row["step_index"] == step_index and row["kind"] == "assist"
    )


def _set_connected(station_id: str, connected: bool) -> None:
    from app.models import Adapter

    with _session() as session:
        session.get(Adapter, station_id).connected = connected
        session.commit()


def _ready_split(operator, batch_id: str, executor, step_id: str, attempt: int = 1):
    import time

    detail = {}
    for _ in range(35):
        executor()
        detail = _detail(operator, batch_id)
        run = next((row for row in detail["step_runs"]
                    if row["step_id"] == step_id and row["attempt"] == attempt and row["state"] == "ready"), None)
        if run:
            return detail, run
        time.sleep(0.03)
    raise AssertionError(f"{step_id} 第 {attempt} 次没有就绪：{detail.get('state')} {detail.get('failure_reason')}")


def _plate_b(operator, batch_id: str) -> dict:
    board = _register(operator, "HOTEL-01/S03", type_id="LT-PLATE-96")
    bound = operator.post(f"/api/batches/{batch_id}/labware", {"labware_id": board["id"], "role": "B"})
    assert bound.status_code == 200, bound.text
    return board


def _sign_dispatch(operator, batch_id: str):
    return operator.post(f"/api/batches/{batch_id}/dispatch", {
        "manual_review": True, "signature_id": operator.sign("批准执行", target=batch_id),
    })


def _migrate(scratch_database: str, revision: str) -> None:
    from conftest import API_DIR

    result = subprocess.run(
        [str(API_DIR / ".venv" / "bin" / "alembic"), "upgrade", revision],
        cwd=API_DIR, capture_output=True, text=True,
        env={**os.environ, "ILCS_DATABASE_URL": scratch_database},
    )
    assert result.returncode == 0, result.stderr


def _copy_into(engine, source_rows, transform=lambda table_name, values: values) -> None:
    """把主测试库里的行按旧结构复制进一次性库；外键指向的行递归一并复制。

    旧结构里可能有后来删掉的列（如 0036 删的 stations.cal_due）：从主库取外键指向的行时只取主库还有的列，
    缺的非空列由 `transform` 按旧结构补上。
    """
    from sqlalchemy import MetaData, and_, select

    from app.models import Base

    old = MetaData()
    old.reflect(bind=engine, only=sorted({name for name, _ in source_rows}))
    copied = set()
    with _session() as source_session, engine.begin() as connection:
        def copy_row(table, values):
            identity = (table.name, tuple(values[col.name] for col in table.primary_key.columns))
            if identity in copied:
                return
            for constraint in table.foreign_key_constraints:
                elements = list(constraint.elements)
                if any(values.get(element.parent.name) is None for element in elements):
                    continue
                parent = elements[0].column.table
                if (parent.name, tuple(values[element.parent.name] for element in elements)) in copied:
                    continue
                current = Base.metadata.tables.get(parent.name)
                columns = [column for column in parent.columns if current is None or column.name in current.c]
                predicate = and_(*(element.column == values[element.parent.name] for element in elements))
                referenced = source_session.execute(select(*columns).where(predicate)).mappings().one()
                copy_row(parent, transform(parent.name, dict(referenced)))
            connection.execute(table.insert().values({k: v for k, v in values.items() if k in table.c}))
            copied.add(identity)

        for table_name, values in source_rows:
            copy_row(old.tables[table_name], transform(table_name, values))


# ---------- WP0 活性看门狗 ----------


def test_liveness_watchdog_reports_a_running_batch_with_nothing_to_advance(operator, executor, reset_runtime):
    """运行中的批次没有开着的步骤、在途指令或待处理事件：超过宽限期报警；不再运行后条件自动复位。"""
    import sys

    from app.models import Alarm, Batch

    settings = sys.modules["app.core.config"].settings
    original = settings.stall_alarm_sec
    settings.stall_alarm_sec = 0
    batch_id = _new_batch(operator)
    key = f"batch:{batch_id}:stalled"
    try:
        with _session() as session:
            session.get(Batch, batch_id).state = "running"
            session.commit()
        executor()
        with _session() as session:
            alarm = session.query(Alarm).filter(Alarm.condition_key == key).one()
            assert alarm.condition_active and "没有开着的步骤" in alarm.message
            session.get(Batch, batch_id).state = "aborted"
            session.commit()
        executor()
        with _session() as session:
            assert not session.query(Alarm).filter(Alarm.condition_key == key).one().condition_active
    finally:
        settings.stall_alarm_sec = original


def test_liveness_watchdog_waits_out_the_grace_period(operator, executor, reset_runtime):
    """刚发生的状态变化还在宽限期内：不报警。"""
    from app.models import Alarm, Batch, StepRun

    batch_id = _new_batch(operator)
    with _session() as session:
        batch = session.get(Batch, batch_id)
        batch.state = "running"
        session.add(StepRun(org_id=batch.org_id, batch_id=batch_id, step_id="s01", step_index=0,
                            kind="device", state="completed"))
        session.commit()
    executor()
    with _session() as session:
        assert session.query(Alarm).filter(Alarm.condition_key == f"batch:{batch_id}:stalled").count() == 0


# ---------- T01 协同设备可用性（WP2） ----------


def test_disconnected_assist_blocks_the_start(operator, devices, db, executor):
    """排程后协同 AGV 适配器失联、工位对象仍是 idle：开跑检查列出它，下发被执行门拒绝。"""
    from app.models import Station

    board = devices["use"]("ST-05", "AGV-01", "AGV-02")
    batch_id = _new_batch(operator)
    _reshape(db, batch_id, _with_robot)
    assert operator.post(f"/api/batches/{batch_id}/schedule", {}).status_code == 200
    helper = _assist_station(operator, batch_id, 0)
    _set_connected(helper, False)
    try:
        with _session() as session:
            assert session.get(Station, helper).status == "idle"
        checks = operator.get(f"/api/batches/{batch_id}/preflight?manual_review=true").json()["checks"]
        blocked = [row for row in checks if row["state"] == "blocked"]
        assert any(helper in (row.get("detail") or "") for row in blocked), checks
        response = _sign_dispatch(operator, batch_id)
        assert response.status_code in {409, 423}, response.text
        executor()
        assert not [call for call in board["calls"] if call[1] == "dispatch"]
    finally:
        _set_connected(helper, True)


@pytest.mark.parametrize("condition", [{"connected": False}, {"accepts_commands": False}])
def test_assist_unavailable_before_delivery_stops_the_main_action(operator, devices, db, executor, condition):
    """开跑之后、投递之前协同设备失联或拒绝动作：主动作不投递，批次挂起；主设备没有收到指令。"""
    from app.models import Adapter

    board = devices["use"]("ST-05", "AGV-01", "AGV-02")
    batch_id = _new_batch(operator)
    _reshape(db, batch_id, _with_robot)
    _dispatch(operator, batch_id)
    helper = _assist_station(operator, batch_id, 0)
    with _session() as session:
        adapter = session.get(Adapter, helper)
        for key, value in condition.items():
            setattr(adapter, key, value)
        session.commit()
    try:
        executor()
        command = next(row for row in _commands(operator, batch_id, "dispatch") if row["step_index"] == 0)
        assert (command["state"], command["delivery_state"]) == ("unknown", "unreachable"), command
        assert helper in command["error"] and "协同资源" in command["error"]
        assert not [call for call in board["calls"] if call[1] == "dispatch"]
        assert _detail(operator, batch_id)["state"] == "fault"
    finally:
        with _session() as session:
            adapter = session.get(Adapter, helper)
            adapter.connected, adapter.accepts_commands = True, True
            session.commit()


def test_assist_calibration_is_checked_for_the_capability_it_performs(operator, db, reset_runtime, monkeypatch):
    """协同工位按它在这一步承担的协同能力核校准；搬运能力不核（承运工位没有校准档案）。"""
    from sqlalchemy.orm.attributes import flag_modified

    from app.core.context import system_context
    from app.domain.resources import AssetSpec
    from app.models import Batch, Command, Station
    from app.services.execution_service import ExecutionService

    batch_id = _new_batch(operator)
    batch = db.get(Batch, batch_id)
    steps = [dict(step) for step in batch.recipe_snapshot["steps"]]
    steps[0]["assist"] = ["cap.assemble", "cap.transfer"]
    batch.recipe_snapshot = {**batch.recipe_snapshot, "steps": steps}
    flag_modified(batch, "recipe_snapshot")
    db.commit()
    service = ExecutionService(db, system_context("ORG-001"))
    monkeypatch.setattr(service, "_asset_spec", lambda asset: AssetSpec(
        asset_id=asset.id, name=f"{asset.asset_no} {asset.name}", state=asset.state, capacity=1,
        calibration_applicable=True, calibration_exempt_reason="", calibrations=(),
    ))
    command = Command(org_id=batch.org_id, batch_id=batch_id, station_id="ST-05", capability=steps[0]["cap"],
                      params={}, type="dispatch", step_index=0, assist_station_ids=["ST-06"])
    problem = service._helper_blocker(batch, command, db.get(Station, "ST-06"), "ST-06")
    assert "ST-06" in problem and "cap.assemble" in problem, problem


# ---------- T02 / T03 有效样本与分装（WP3） ----------


def test_repeated_split_sends_only_the_latest_children(operator, clean_labware, executor, db):
    """同板连续两次分装：下一条设备指令只含最后一代子样的孔位，不含已拆分母样仍占着的孔。"""
    from app.models import Command

    batch_id = _new_batch(operator)
    _plate_b(operator, batch_id)
    _reshape(db, batch_id, lambda steps: [
        _split_step("s01"), _split_step("s02"),
        {"step_id": "s03", "name": "二次分装后注液", "cap": "cap.assemble",
         "params": {"electrolyte": 60}, "dur": 30, "labware": "B"},
    ])
    _target_factor(db, batch_id, "s03", "electrolyte")
    _dispatch(operator, batch_id)
    detail, first = _ready_split(operator, batch_id, executor, "s01")
    first_wells = [f"{row}{col}" for row in "CD" for col in range(1, 9)]
    response = operator.post(f"/api/step-runs/{first['id']}/split", {
        "placements": _placements(detail, first_wells), "labware_role": "B",
    })
    assert response.status_code == 200, response.text
    detail, second = _ready_split(operator, batch_id, executor, "s02")
    second_wells = [f"{row}{col}" for row in "EFGH" for col in range(1, 9)]
    placements = _placements(detail, second_wells)
    response = operator.post(f"/api/step-runs/{second['id']}/split", {"placements": placements, "labware_role": "B"})
    assert response.status_code == 200, response.text
    command = next(row for row in _commands(operator, batch_id, "dispatch") if row["step_index"] == 2)
    with _session() as session:
        sent = set(session.get(Command, command["id"]).params["wells"])
    assert sent == {row["well"] for row in placements}
    assert_consistent(batch_id)


def _rework_to_split_steps() -> list[dict]:
    return [
        _split_step("s01"),
        {"step_id": "s02", "name": "检测分装样品", "cap": "cap.assemble",
         "params": {"electrolyte": 60}, "dur": 30, "labware": "B"},
        {"step_id": "s03", "kind": "gate", "name": "不合格重新分装",
         "gate": {"source_step_id": "s02", "field": "water_ppm", "max": 20, "scope": "batch",
                  "on_fail": "rework", "rework_to": "s01", "max_rework": 1}},
    ]


def test_validation_rejects_rework_across_a_physical_split():
    """实体分装不可逆：返工目标在分装之前（或就是分装）时，流程校验报错。"""
    from app.domain.steps import gate_issues

    steps = _rework_to_split_steps()
    issues = gate_issues(steps[2], steps, 2)
    assert any("实体分装" in text for text in issues), issues


def test_frozen_rework_across_a_physical_split_holds_for_disposition(
    operator, clean_labware, executor, db, measured,
):
    """修复前固化的批次运行中触发这类返工：不自动返工，批次挂起待人工处置，子样仍在用。"""
    from app.models import Sample

    batch_id = _new_batch(operator)
    _plate_b(operator, batch_id)
    _reshape(db, batch_id, lambda _: _rework_to_split_steps())
    measured["s02"] = [{"water_ppm": 50}]
    _dispatch(operator, batch_id)
    detail, first = _ready_split(operator, batch_id, executor, "s01")
    wells = [f"{row}{col}" for row in "CD" for col in range(1, 9)]
    response = operator.post(f"/api/step-runs/{first['id']}/split", {
        "placements": _placements(detail, wells), "labware_role": "B",
    })
    assert response.status_code == 200, response.text
    detail = _run(operator, batch_id, executor, rounds=30, until=("paused", "fault", "done", "aborted"))
    assert detail["state"] == "paused", detail["failure_reason"]
    assert "实体分装" in detail["failure_reason"]
    assert [row["attempt"] for row in detail["step_runs"] if row["step_id"] == "s01"] == [1]
    assert len(_commands(operator, batch_id, "dispatch")) == 1
    with _session() as session:
        active = [row for row in session.query(Sample).filter(Sample.batch_id == batch_id).all()
                  if row.state not in {"failed", "split"}]
    assert len(active) == 16


def test_physical_split_without_active_samples_is_refused(operator, clean_labware, executor, db):
    """没有在用母样时确认实体分装：拒绝，不把「0 个样本」登记成完整分装，也不生成设备动作。"""
    from app.models import Sample

    batch_id = _new_batch(operator)
    _plate_b(operator, batch_id)
    _reshape(db, batch_id, lambda steps: [
        _split_step("s01"),
        {"step_id": "s02", "name": "注液", "cap": "cap.assemble", "params": {"electrolyte": 60}, "dur": 30,
         "labware": "B"},
    ])
    _dispatch(operator, batch_id)
    _, run = _ready_split(operator, batch_id, executor, "s01")
    with _session() as session:
        for sample in session.query(Sample).filter(Sample.batch_id == batch_id).all():
            sample.state = "failed"
        session.commit()
    response = operator.post(f"/api/step-runs/{run['id']}/split", {"placements": [], "labware_role": "B"})
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "split_no_active_samples"
    assert not _commands(operator, batch_id, "dispatch")


def test_validation_rejects_a_loop_across_a_physical_split():
    from app.domain.steps import branch_issues

    steps = [
        _split_step("s01"),
        {"step_id": "s02", "name": "检测", "cap": "cap.assemble", "params": {}, "dur": 30},
        {"step_id": "s03", "kind": "branch", "name": "是否重做", "branch": {
            "mode": "measure", "source_step_id": "s02", "field": "w", "max_loops": 1,
            "cases": [{"key": "again", "label": "重做", "min": 20, "loop_to": "s01"},
                      {"key": "ok", "label": "合格", "max": 20}],
        }},
    ]
    issues = branch_issues(steps[2], steps, 2)
    assert any("实体分装" in text for text in issues), issues


def test_rework_over_a_logical_split_restores_the_parents(operator, running_batch, executor, measured, db):
    """系统内分组被返工：子样作废，母样恢复在用，重做时按母样重新拆分，子样数量正确。"""
    from test_gate_split import _append

    measured["s04"] = [{"water_ppm": 35}, {"water_ppm": 12}]
    _append(
        running_batch,
        {"step_id": "s05", "kind": "split", "name": "系统分组", "split": {"count": 2, "child_type": "子样"}},
        {"step_id": "s06", "kind": "gate", "name": "水分质检",
         "gate": {"source_step_id": "s04", "field": "water_ppm", "max": 20, "scope": "batch",
                  "on_fail": "rework", "rework_to": "s04", "max_rework": 1}},
    )
    detail = _run(operator, running_batch, executor, rounds=30)
    assert detail["state"] == "done", detail["failure_reason"]
    samples = detail["samples"]
    parents = [row for row in samples if row["state"] == "split"]
    live = [row for row in samples if row["state"] not in {"split", "failed"}]
    retired = [row for row in samples if row["state"] == "failed"]
    assert len(parents) == 8 and len(live) == 16 and len(retired) == 16, [row["state"] for row in samples]


def test_split_step_without_active_samples_holds_the_batch(operator, running_batch, executor, db):
    """拆分节点开出时没有在用样本：不登记「0 个样本」的拆分，批次挂起待人工处置。"""
    from app.models import Sample
    from test_gate_split import _append

    _append(running_batch, {"step_id": "s05", "kind": "split", "name": "系统分组",
                            "split": {"count": 2, "child_type": "子样"}})
    with _session() as session:
        for sample in session.query(Sample).filter(Sample.batch_id == running_batch).all():
            sample.state = "failed"
        session.commit()
    detail = _run(operator, running_batch, executor, rounds=16, until=("paused", "done", "fault"))
    assert detail["state"] == "paused", detail["failure_reason"]
    assert "没有可分装的在用样本" in detail["failure_reason"]


# ---------- T04 SOP 按实际采用版本校验（WP8） ----------


def test_new_batch_refuses_an_adopted_sop_that_no_longer_covers_the_recipe(researcher, qa, operator, reset_runtime):
    code = f"SOP-T04-{uuid4().hex[:6]}"
    old = publish_sop(researcher, qa, code, "v1", capability_scope=["cap.mix"])
    recipe = released_recipe(researcher, qa, old["id"])
    plan = approved_plan(researcher, qa, recipe)
    publish_sop(researcher, qa, code, "v2", capability_scope=["cap.test"])
    created = operator.post("/api/batches", {"plan_id": plan})
    assert created.status_code == 409, created.text
    assert created.json()["detail"]["code"] == "sop_scope_mismatch"


def test_preflight_blocks_a_frozen_sop_scope_mismatch(researcher, qa, operator, db, reset_runtime):
    """修复前已建的批次：固化的 SOP 范围不覆盖流程的设备能力时，开跑检查阻断。"""
    from sqlalchemy.orm.attributes import flag_modified

    from app.models import Batch

    code = f"SOP-T04B-{uuid4().hex[:6]}"
    version = publish_sop(researcher, qa, code, "v1", capability_scope=["cap.mix"])
    plan = approved_plan(researcher, qa, released_recipe(researcher, qa, version["id"]))
    created = operator.post("/api/batches", {"plan_id": plan})
    assert created.status_code == 201, created.text
    batch_id = created.json()["id"]
    batch = db.get(Batch, batch_id)
    batch.sop_snapshot = {**batch.sop_snapshot, "capability_scope": ["cap.test"]}
    flag_modified(batch, "sop_snapshot")
    db.commit()
    assert operator.post(f"/api/batches/{batch_id}/schedule", {}).status_code == 200
    checks = operator.get(f"/api/batches/{batch_id}/preflight?manual_review=true").json()["checks"]
    sop = next(row for row in checks if row["key"] == "sop")
    assert sop["state"] == "blocked" and "cap.mix" in sop["detail"], sop
    assert _sign_dispatch(operator, batch_id).status_code == 409


# ---------- T06 历史「结论未知」回填（WP4） ----------


def test_upgrade_backfills_legacy_unknown_outcomes(operator, scratch_database):
    """在 0031 结构写入上版的三类未决指令，真实升级到最新：
    台账 unknown 的回填 unknown、继续占用工位；台账 failed 的回填 failed、放行；没有台账的保持空串，
    出现在迁移核对的「结论待核查的指令」里。"""
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session

    from app.core.clock import now
    from app.models import AdapterExecution, Batch, Command, Plan, Recipe, Station
    from app.repositories.execution import CommandRepository, still_occupying
    from app.services.migration_report_service import MigrationReportService

    batch_id = _new_batch(operator)
    unknown_id, failed_id, orphan_id = (str(uuid.uuid4()) for _ in range(3))
    with _session() as session:
        batch = session.get(Batch, batch_id)
        batch.state = "fault"
        for command_id, station in ((unknown_id, "ST-05"), (failed_id, "ST-06"), (orphan_id, "ST-07")):
            session.add(Command(
                id=command_id, org_id=batch.org_id, batch_id=batch.id, station_id=station,
                capability="cap.vacuum_dry", params={}, type="dispatch", step_index=0, state="unknown",
                delivery_state="delivered", started_at=now(), error="上版回执",
            ))
        session.flush()
        for command_id, station, state in ((unknown_id, "ST-05", "unknown"), (failed_id, "ST-06", "failed")):
            session.add(AdapterExecution(
                command_id=command_id, station_id=station, state=state,
                result={"command_id": command_id, "state": state, "device_ts": now().isoformat(),
                        "quality": "uncertain", "delivered": {}, "error": "", "origin": "real:legacy"},
            ))
        session.commit()
        batch = session.get(Batch, batch_id)
        order = [
            *((Station.__table__, Station.id == station) for station in ("ST-05", "ST-06", "ST-07")),
            (Recipe.__table__, Recipe.id == batch.recipe_id),
            (Plan.__table__, Plan.id == batch.plan_id),
            (Batch.__table__, Batch.id == batch.id),
            *((Command.__table__, Command.id == command_id) for command_id in (unknown_id, failed_id, orphan_id)),
            *((AdapterExecution.__table__, AdapterExecution.command_id == command_id) for command_id in (unknown_id, failed_id)),
        ]
        source_rows = [(table.name, dict(session.execute(select(table).where(condition)).mappings().one()))
                       for table, condition in order]
        # 主测试库里的这几条只是复制来源：不能留给后面的用例（执行器会把它们当成在途的未知动作）
        session.query(AdapterExecution).filter(AdapterExecution.command_id.in_([unknown_id, failed_id])).delete(
            synchronize_session=False)
        session.query(Command).filter(Command.id.in_([unknown_id, failed_id, orphan_id])).update(
            {"state": "cancelled"}, synchronize_session=False)
        session.get(Batch, batch_id).state = "aborted"
        session.commit()

    def restore(table_name, values):
        if table_name == "batches":
            return {**values, "state": "fault"}
        if table_name == "commands":
            return {**values, "state": "unknown"}
        if table_name == "stations":
            # 0036 之前工位上还有这两列（非空、无缺省）：按旧结构补上占位值
            return {**values, "cal_due": "", "positions": 1}
        return values

    _migrate(scratch_database, "0031_automation_extensions")
    engine = create_engine(scratch_database)
    try:
        _copy_into(engine, source_rows, restore)
        _migrate(scratch_database, "head")
        with Session(engine) as session:
            legacy, failed, orphan = (session.get(Command, key) for key in (unknown_id, failed_id, orphan_id))
            assert (legacy.outcome, failed.outcome, orphan.outcome) == ("unknown", "failed", "")
            assert still_occupying(legacy) and not still_occupying(failed)
            assert [row.id for row in CommandRepository(session).occupying(["ST-05", "ST-06", "ST-07"])] == [unknown_id]
            line = next(row for row in MigrationReportService(session).reconcile()["lines"]
                        if row["key"] == "unsettled_outcome")
            assert not line["ok"] and orphan_id[:8] in line["detail"] and unknown_id[:8] not in line["detail"], line
    finally:
        engine.dispose()


# ---------- T07 SOP 步骤稳定标识（WP9） ----------


def _publish_steps(researcher, qa, code: str, version: str, steps: list[dict]) -> dict:
    uploaded = researcher.upload("/api/files", f"{code}-{version}.pdf", PDF, "application/pdf")
    assert uploaded.status_code == 201, uploaded.text
    created = researcher.post("/api/sops", {
        "code": code, "title": "SOP 映射复核", "version": version,
        "file_id": uploaded.json()["id"], "capability_scope": ["cap.mix"],
    })
    assert created.status_code == 201, created.text
    row = created.json()
    saved = researcher.put(f"/api/sops/{row['id']}/steps", {"steps": steps, "row_version": row["row_version"]})
    assert saved.status_code == 200, saved.text
    assert researcher.post(f"/api/sops/{row['id']}/submit").status_code == 200
    approved = qa.post(f"/api/sops/{row['id']}/decision", {
        "conclusion": "approved", "signature_id": qa.sign("批准 SOP", target=row["id"]),
    })
    assert approved.status_code == 200, approved.text
    return approved.json()


MIX = {"title": "混匀", "kind": "device", "capability": "cap.mix", "params": {"rpm": 2000},
       "duration_min": 5, "instructions": "混匀过程中保持盖子关闭", "checks": []}
CLEAN = {"title": "清洗容器", "kind": "manual", "duration_min": 5,
         "instructions": "开机前清洗容器并确认干燥", "checks": ["已清洗"]}


def test_superseding_sop_that_inserts_a_step_keeps_the_guide_on_the_same_step(
    researcher, qa, operator, reset_runtime,
):
    code = f"SOP-T07-{uuid4().hex[:6]}"
    old = _publish_steps(researcher, qa, code, "v1", [MIX])
    plan = approved_plan(researcher, qa, released_recipe(researcher, qa, old["id"], sop_step=1))
    _publish_steps(researcher, qa, code, "v2", [CLEAN, MIX])
    created = operator.post("/api/batches", {"plan_id": plan})
    assert created.status_code == 201, created.text
    detail = operator.get(f"/api/batches/{created.json()['id']}").json()
    assert detail["sop_snapshot"]["version"] == "v2"
    assert detail["snapshot"]["steps"][0]["cap"] == "cap.mix"
    assert detail["steps"][0]["sop_guide"]["title"] == "混匀"


def _draft_steps(researcher, version: dict, steps: list[dict]) -> dict:
    saved = researcher.put(f"/api/sops/{version['id']}/steps", {"steps": steps, "row_version": version["row_version"]})
    assert saved.status_code == 200, saved.text
    return saved.json()


def _approve_draft(researcher, qa, version_id: str) -> dict:
    assert researcher.post(f"/api/sops/{version_id}/submit").status_code == 200
    approved = qa.post(f"/api/sops/{version_id}/decision", {
        "conclusion": "approved", "signature_id": qa.sign("批准 SOP", target=version_id),
    })
    assert approved.status_code == 200, approved.text
    return approved.json()


def test_revision_copied_from_the_effective_version_keeps_step_identity(researcher, qa, operator, reset_runtime):
    """从生效版本复制步骤的修订：标识随步骤带过去，前面插入一步后，生成的流程节点仍对得上原来那一步。"""
    code = f"SOP-T07K-{uuid4().hex[:6]}"
    # 从 SOP 生成的流程直接拿 SOP 步骤的参数，要满足能力的参数要求
    v1 = _publish_steps(researcher, qa, code, "v1", [{**MIX, "params": {"temp": 25, "rpm": 2000}}])
    assert all(step.get("key") for step in v1["steps"]), "保存时补上稳定标识"
    generated = researcher.post(f"/api/sops/{v1['id']}/generate-recipe", {"name": "按 SOP 生成"})
    assert generated.status_code in {200, 201}, generated.text
    recipe_id = generated.json()["recipe_id"]
    recipe = researcher.get(f"/api/recipes/{recipe_id}").json()
    assert recipe["steps"][0]["sop_step_key"] == v1["steps"][0]["key"]
    uploaded = researcher.upload("/api/files", f"{code}-v2.pdf", PDF, "application/pdf")
    v2 = researcher.post("/api/sops", {
        "code": code, "title": "SOP 映射复核", "version": "v2", "file_id": uploaded.json()["id"],
        "capability_scope": ["cap.mix"], "copy_steps": True,
    })
    assert v2.status_code == 201, v2.text
    copied = v2.json()
    assert [step["key"] for step in copied["steps"]] == [v1["steps"][0]["key"]]
    edited = _draft_steps(researcher, copied, [CLEAN, *copied["steps"]])
    assert edited["steps"][1]["key"] == v1["steps"][0]["key"] and edited["steps"][0]["key"]
    _approve_draft(researcher, qa, copied["id"])
    patched = researcher.patch(f"/api/recipes/{recipe_id}", {"risk": "RA-sop v1", "bom": []})
    assert patched.status_code == 200, patched.text
    submitted = researcher.post(f"/api/recipes/{recipe_id}/submit")
    assert submitted.status_code == 200, submitted.text
    for target, meaning in (("approved", "批准"), ("released", "发布")):
        moved = qa.post(f"/api/recipes/{recipe_id}/transition",
                        {"target_state": target, "signature_id": qa.sign_recipe(meaning, recipe_id)})
        assert moved.status_code == 200, moved.text
    created = operator.post("/api/batches", {"plan_id": approved_plan(researcher, qa, recipe_id)})
    assert created.status_code == 201, created.text
    detail = operator.get(f"/api/batches/{created.json()['id']}").json()
    assert detail["sop_snapshot"]["version"] == "v2"
    assert detail["steps"][0]["sop_guide"]["title"] == "混匀" and detail["steps"][0]["sop_guide"]["index"] == 2


def test_ambiguous_sop_step_mapping_blocks_new_batches(researcher, qa, operator, reset_runtime):
    """新版里同类型同能力的步骤不止一步、标题也对不上：映射失效，流程的 SOP 检查不通过，新批次被阻止。"""
    code = f"SOP-T07B-{uuid4().hex[:6]}"
    old = _publish_steps(researcher, qa, code, "v1", [MIX])
    recipe_id = released_recipe(researcher, qa, old["id"], sop_step=1)
    plan = approved_plan(researcher, qa, recipe_id)
    premix = {**MIX, "title": "预混"}
    remix = {**MIX, "title": "复混"}
    _publish_steps(researcher, qa, code, "v2", [premix, remix])
    checks = researcher.get(f"/api/recipes/{recipe_id}").json()["checks"]
    sop = next(row for row in checks if row["key"] == "sop")
    assert sop["ok"] is False and "映射失效" in sop["detail"], sop
    created = operator.post("/api/batches", {"plan_id": plan})
    assert created.status_code == 409 and created.json()["detail"]["code"] == "sop_mapping_broken", created.text


def test_upgrade_gives_existing_sop_steps_stable_keys(researcher, qa, scratch_database):
    """真实迁移：0034 结构里没有标识的 SOP 步骤，升级后每一步都有版本内唯一的标识。"""
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session

    from app.models import SopVersion

    version = _publish_steps(researcher, qa, f"SOP-MIG-{uuid4().hex[:6]}", "v1", [CLEAN, MIX])
    with _session() as session:
        row = dict(session.execute(select(SopVersion.__table__).where(SopVersion.id == version["id"])).mappings().one())

    def strip(table_name, values):
        if table_name == "sop_versions":
            return {**values, "steps": [{k: v for k, v in step.items() if k != "key"} for step in values["steps"]]}
        return values

    _migrate(scratch_database, "0034_backfill_command_outcome")
    engine = create_engine(scratch_database)
    try:
        _copy_into(engine, [("sop_versions", row)], strip)
        with Session(engine) as session:
            assert not any(step.get("key") for step in session.get(SopVersion, version["id"]).steps)
        _migrate(scratch_database, "head")
        with Session(engine) as session:
            keys = [step.get("key") for step in session.get(SopVersion, version["id"]).steps]
        assert all(keys) and len(set(keys)) == 2, keys
    finally:
        engine.dispose()


# ---------- A01 恢复不重复下发（WP1） ----------


def test_resume_after_an_unexecuted_hold_does_not_dispatch_the_step_again(
    operator, scripted, running_batch, db, executor,
):
    """保持未送达，现场核实「未执行」：动作仍在设备上跑。续跑只重新挂接它，设备只收到一次动作。"""
    from app.models import Checkpoint

    devices = scripted["use"]("ST-05")
    executor()
    dry = _commands(operator, running_batch, "dispatch")[0]
    devices["fail"][("ST-05", "hold")] = "unreachable"
    assert operator.post(f"/api/batches/{running_batch}/hold", {"reason": "核对读数"}).status_code == 200
    executor()
    hold = _commands(operator, running_batch, "hold")[0]
    assert (hold["state"], hold["delivery_state"]) == ("unknown", "maybe_sent")
    devices["fail"].clear()
    assert _verify(operator, hold["id"], "not_executed").status_code == 200
    options = operator.get(f"/api/batches/{running_batch}/recovery-options").json()["options"]
    retry = next(row for row in options if row["id"] == "retry")
    assert not retry["allowed"] and "仍在执行" in retry["reason"], retry
    resumed = _resume(operator, running_batch)
    assert resumed.status_code == 200, resumed.text
    for _ in range(3):
        executor()
    devices["finished"].add(dry["id"])
    for _ in range(4):
        executor()
    step0 = {row["id"] for row in _commands(operator, running_batch, "dispatch", "resume", "retry")
             if row["step_index"] == 0}
    calls = [call for call in devices["calls"] if call[1] in {"dispatch", "resume", "retry"} and call[2] in step0]
    assert calls == [("ST-05", "dispatch", dry["id"])], calls
    detail = _detail(operator, running_batch)
    assert [(row["attempt"], row["state"]) for row in detail["step_runs"] if row["step_index"] == 0] == [(1, "completed")]
    with _session() as session:
        assert session.query(Checkpoint).filter(
            Checkpoint.batch_id == running_batch, Checkpoint.step_index == 0,
        ).count() == 1
    assert_consistent(running_batch)


def test_resume_after_an_unexecuted_abort_reattaches_the_running_action(
    operator, scripted, running_batch, db, executor,
):
    """终止没送到设备、现场核实未执行：批次回到故障，续跑同样只是重新挂接仍在执行的动作。"""
    from test_core_chain_review import _abort

    devices = scripted["use"]("ST-05")
    executor()
    dry = _commands(operator, running_batch, "dispatch")[0]
    devices["fail"][("ST-05", "abort")] = "unreachable"
    assert _abort(operator, running_batch).status_code == 200
    executor()
    abort = _commands(operator, running_batch, "abort")[0]
    devices["fail"].clear()
    assert _verify(operator, abort["id"], "not_executed").status_code == 200
    assert _detail(operator, running_batch)["state"] == "fault"
    resumed = _resume(operator, running_batch)
    assert resumed.status_code == 200, resumed.text
    devices["finished"].add(dry["id"])
    for _ in range(5):
        executor()
    step0 = {row["id"] for row in _commands(operator, running_batch, "dispatch", "resume", "retry")
             if row["step_index"] == 0}
    calls = [call for call in devices["calls"] if call[1] in {"dispatch", "resume", "retry"} and call[2] in step0]
    assert calls == [("ST-05", "dispatch", dry["id"])], calls
    assert_consistent(running_batch)


def test_a_second_action_for_the_same_step_is_refused(operator, scripted, running_batch, db, executor):
    """下发入口的硬约束：这一步的动作还在设备上时，再发一条 dispatch 或不接续它的续跑都被拒。"""
    from app.core.context import system_context
    from app.core.errors import StateConflict
    from app.models import Batch
    from app.services.batch_service import BatchService

    scripted["use"]("ST-05")
    executor()
    assert _commands(operator, running_batch, "dispatch")[0]["state"] == "running"
    service = BatchService(db, system_context("ORG-001"))
    batch = db.get(Batch, running_batch)
    for kind in ("dispatch", "resume", "retry"):
        with pytest.raises(StateConflict) as refused:
            service.issue_command(batch, kind, 0)
        assert refused.value.code == "duplicate_action"
    db.rollback()


def test_engine_retry_of_a_refused_resume_takes_over_the_held_action(
    operator, admin, scripted, running_batch, db, executor,
):
    """A11：续跑指令投递时适配器失联被拒，异常引擎按策略重下：新指令仍接续被保持的动作，接手后原动作到此为止。"""
    from test_core_chain_review import _command
    from test_exception_engine import _disable_rules, _rule, _set_adapter

    _disable_rules(db)
    _rule(admin, action="retry", params={"max_attempts": 1, "delay_sec": 0}, match={"capability": "cap.vacuum_dry"})
    devices = scripted["use"]("ST-05")
    try:
        executor()
        dry = _commands(operator, running_batch, "dispatch")[0]
        assert operator.post(f"/api/batches/{running_batch}/hold", {"reason": "核对"}).status_code == 200
        executor()
        assert _command(db, dry["id"]).state == "held"
        assert _resume(operator, running_batch).status_code == 200
        _set_adapter(db, "ST-05", connected=False)
        executor()
        _set_adapter(db, "ST-05", connected=True)
        resumes = _commands(operator, running_batch, "resume")
        assert [row["state"] for row in resumes][:1] == ["not_executed"], resumes
        retried = resumes[-1]
        assert retried["target_command_id"] == dry["id"], retried
        for _ in range(2):
            executor()
        assert _command(db, dry["id"]).state == "superseded"
        assert _command(db, retried["id"]).state == "running"
        step0 = [call for call in devices["calls"] if call[1] in {"dispatch", "resume", "retry"}]
        assert step0 == [("ST-05", "dispatch", dry["id"]), ("ST-05", "resume", retried["id"])], step0
        assert_consistent(running_batch)
    finally:
        _disable_rules(db)


# ---------- A02 逐样本质检（WP6） ----------


def test_sample_gate_without_readings_goes_to_qa_and_keeps_every_sample(
    operator, qa, running_batch, executor, measured,
):
    from test_gate_split import _append, _gate, _gate_run

    measured["s04"] = [{"water_ppm": 12}]
    _append(running_batch, _gate(scope="sample", on_fail="rework", rework_to="s04", max_rework=1))
    detail = _run(operator, running_batch, executor, rounds=24, until=("paused", "done", "fault"))
    assert detail["state"] == "paused", detail["failure_reason"]
    assert {row["state"] for row in detail["samples"]} == {"running"}
    assert [row["attempt"] for row in detail["step_runs"] if row["step_id"] == "s04"] == [1], "无读数不自动返工"
    pending_gate = _gate_run(detail)[-1]
    decided = qa.post(f"/api/step-runs/{pending_gate['id']}/gate-decision", {
        "conclusion": "approved", "reason": "现场复核读数正常，放行",
        "signature_id": qa.sign("质检判定属实", target=pending_gate["id"]),
    })
    assert decided.status_code == 200, decided.text
    final = _run(operator, running_batch, executor, rounds=8)
    assert final["state"] == "done"
    assert "failed" not in {row["state"] for row in final["samples"]}


def test_sample_gate_all_out_of_range_reworks_every_sample(operator, running_batch, executor, measured):
    """全部超限且配置返工：不预先剔除，返工后第二轮对全部样本判定，而不是对零个样本判定。"""
    from test_gate_split import _append, _gate, _gate_run

    wells = [row["well"] for row in _detail(operator, running_batch)["samples"]]
    measured["s04"] = [
        {"wells": {well: {"loading": 25.0} for well in wells}},
        {"wells": {well: {"loading": 18.0} for well in wells}},
    ]
    _append(running_batch, _gate(field="loading", min=17, max=19, scope="sample", on_fail="rework",
                                 rework_to="s04", max_rework=1))
    detail = _run(operator, running_batch, executor, rounds=30)
    assert detail["state"] == "done", detail["failure_reason"]
    gates = _gate_run(detail)
    assert [row["state"] for row in gates] == ["failed", "completed"]
    assert "failed" not in {row["state"] for row in detail["samples"]}


def test_sample_gate_with_some_missing_readings_goes_to_qa_without_culling(
    operator, qa, running_batch, executor, measured,
):
    """部分样本没有读数：转人工，不剔除；QA 放行时指定剔除的样本才判失败。"""
    from test_gate_split import _append, _gate, _gate_run

    wells = [row["well"] for row in _detail(operator, running_batch)["samples"]]
    readings = {well: {"loading": 18.0} for well in wells[2:]}
    readings[wells[2]] = {"loading": 25.0}
    measured["s04"] = [{"wells": readings}]
    _append(running_batch, _gate(field="loading", min=17, max=19, scope="sample", on_fail="scrap"))
    detail = _run(operator, running_batch, executor, rounds=24, until=("paused", "done", "fault"))
    assert detail["state"] == "paused", detail["failure_reason"]
    assert {row["state"] for row in detail["samples"]} == {"running"}
    gate = _gate_run(detail)[-1]
    assert gate["form_data"]["undecided"] == wells[:2] and gate["form_data"]["failed"] == [wells[2]]
    everything = qa.post(f"/api/step-runs/{gate['id']}/gate-decision", {
        "conclusion": "approved", "reason": "全剔", "exclude_wells": wells,
        "signature_id": qa.sign("质检判定属实", target=gate["id"]),
    })
    assert everything.status_code == 422, "不能剔除全部在用样本"
    decided = qa.post(f"/api/step-runs/{gate['id']}/gate-decision", {
        "conclusion": "approved", "reason": "两孔漏测、一孔超限，剔除后放行", "exclude_wells": wells[:3],
        "signature_id": qa.sign("质检判定属实", target=gate["id"]),
    })
    assert decided.status_code == 200, decided.text
    final = _run(operator, running_batch, executor, rounds=8)
    assert final["state"] == "done"
    states = {row["well"]: row["state"] for row in final["samples"]}
    assert all(states[well] == "failed" for well in wells[:3])
    assert all(states[well] != "failed" for well in wells[3:])


# ---------- A03 图模式返工重开关卡（WP5） ----------


def test_graph_mode_gate_is_evaluated_again_after_rework(operator, db, executor, measured, reset_runtime):
    batch_id = _new_batch(operator)

    def shape(steps):
        chained, previous = [], None
        for step in steps:
            chained.append({**step, "after": [previous] if previous else []})
            previous = step["step_id"]
        chained.append({
            "step_id": "s05", "kind": "gate", "name": "水分质检", "after": [previous],
            "gate": {"source_step_id": "s04", "field": "water_ppm", "max": 20, "scope": "batch",
                     "on_fail": "rework", "rework_to": "s04", "max_rework": 2},
        })
        return chained

    _reshape(db, batch_id, shape)
    measured["s04"] = [{"water_ppm": 35}, {"water_ppm": 12}]
    _dispatch(operator, batch_id)
    detail = _run(operator, batch_id, executor, rounds=40)
    gates = [(row["attempt"], row["state"]) for row in detail["step_runs"] if row["kind"] == "gate"]
    assert gates == [(1, "failed"), (2, "completed")], gates
    assert detail["state"] == "done"


def _chain(steps):
    """把隐式的「依赖上一行」写成显式 after：流程进入依赖图模式，推进走前沿判定。"""
    chained, previous = [], None
    for step in steps:
        chained.append({**{k: v for k, v in step.items() if k != "hard"}, "after": [previous] if previous else []})
        previous = step["step_id"]
    return chained


def test_graph_mode_review_rejection_reopens_the_review_after_the_manual_redo(
    operator, qa, single_condition_task, executor, db,
):
    """A18：带依赖声明的审核退回。人工记录第二次提交后审核重开；中间的设备步骤不重跑；
    人工记录重做完之前审核不会被提前重开。"""
    from app.core.clock import now
    from app.models import StepRun

    batch_id = single_condition_task["batch_id"]
    _reshape(db, batch_id, _chain)

    def submit(run_id):
        response = operator.post(f"/api/step-runs/{run_id}/submit", {
            "form_data": {"weighed_g": 20.1, "balance_id": "BAL-01", "double_check": True},
            "checks": {"samples": True, "materials": True},
            "signature_id": operator.sign("人工记录确认", target=run_id),
        })
        assert response.status_code == 200, response.text

    def runs(kind):
        return [row for row in _detail(operator, batch_id)["step_runs"] if row["kind"] == kind]

    submit(runs("manual")[0]["id"])
    executor()
    with _session() as session:
        session.get(StepRun, runs("wait")[0]["id"]).due_at = now()
        session.commit()
    executor()
    review = runs("review")[0]
    assert review["state"] == "ready"
    rejected = qa.post(f"/api/step-runs/{review['id']}/review", {
        "conclusion": "rejected", "reason": "称量记录与物料批号不符",
        "signature_id": qa.sign("审核退回", target=review["id"]),
    })
    assert rejected.status_code == 200, rejected.text
    for _ in range(3):
        executor()
    assert [row["state"] for row in runs("review")] == ["failed"], "人工记录重做完之前审核不重开"
    manual = runs("manual")
    assert [row["attempt"] for row in manual] == [1, 2] and manual[1]["state"] == "ready"
    submit(manual[1]["id"])
    executor()
    reviews = runs("review")
    assert [(row["attempt"], row["state"]) for row in reviews] == [(1, "failed"), (2, "ready")], reviews
    approved = qa.post(f"/api/step-runs/{reviews[1]['id']}/review", {
        "conclusion": "approved", "reason": "", "signature_id": qa.sign("审核通过", target=reviews[1]["id"]),
    })
    assert approved.status_code == 200, approved.text
    detail = _run(operator, batch_id, executor, rounds=6)
    assert detail["state"] == "done", detail["failure_reason"]
    assert len([row for row in detail["step_runs"] if row["kind"] == "device"]) == 1, "设备步骤不重跑"


def test_graph_rework_leaves_an_unrelated_parallel_branch_alone(operator, db, executor, measured, reset_runtime):
    """A17：列表位置夹在返工目标与关卡之间、却与关卡无关的并行分支，不被作废，也不重跑。"""
    batch_id = _new_batch(operator)

    def shape(steps):
        dry, weigh, assemble, test = (dict((k, v) for k, v in step.items() if k != "hard") for step in steps)
        return [
            {**dry, "after": []},
            {**assemble, "after": []},
            {**weigh, "after": [dry["step_id"]]},
            {"step_id": "s05", "kind": "gate", "name": "称重质检", "after": [weigh["step_id"]],
             "gate": {"source_step_id": weigh["step_id"], "field": "water_ppm", "max": 20, "scope": "batch",
                      "on_fail": "rework", "rework_to": dry["step_id"], "max_rework": 1}},
            {**test, "after": ["s05", assemble["step_id"]]},
        ]

    _reshape(db, batch_id, shape)
    measured["s02"] = [{"water_ppm": 35}, {"water_ppm": 12}]
    _dispatch(operator, batch_id)
    detail = _run(operator, batch_id, executor, rounds=50)
    assert detail["state"] == "done", detail["failure_reason"]
    by_step = {}
    for row in detail["step_runs"]:
        by_step.setdefault(row["step_index"], []).append(row["state"])
    assert by_step[1] == ["completed"], by_step
    assert by_step[0] == ["superseded", "completed"] and by_step[2] == ["superseded", "completed"], by_step
    assert len([row for row in _commands(operator, batch_id, "dispatch") if row["step_index"] == 1]) == 1


def test_recovery_does_not_reopen_an_untaken_branch(operator, db, executor, reset_runtime):
    """A19：保持期间已选路径走完，恢复时批次结束；没走的分支不被当成待办重新下发。"""
    from app.core.clock import now
    from app.models import StepRun

    def shape(steps):
        dry, weigh, assemble, test = (dict((k, v) for k, v in step.items() if k != "hard") for step in steps)
        return [
            {**dry, "after": []},
            {"step_id": "b1", "name": "是否直接组装", "kind": "branch", "after": [dry["step_id"]],
             "branch": {"mode": "manual", "cases": [{"key": "assemble", "label": "直接组装"},
                                                   {"key": "weigh", "label": "先称重"}]}},
            {**assemble, "after": ["b1"], "when": {"b1": "assemble"}},
            {"step_id": "w9", "name": "静置", "kind": "wait", "dur": 600, "after": [assemble["step_id"]],
             "wait_for": {"mode": "duration"}},
            {**weigh, "after": ["b1"], "when": {"b1": "weigh"}},
        ]

    batch_id = _new_batch(operator)
    _reshape(db, batch_id, shape)
    _dispatch(operator, batch_id)
    detail = _run(operator, batch_id, executor, rounds=8, until=("never",))
    choice = next(row for row in detail["step_runs"] if row["step_id"] == "b1")
    chosen = operator.post(f"/api/step-runs/{choice['id']}/branch-decision", {
        "case": "assemble", "reason": "极片已称过", "row_version": choice["row_version"],
    })
    assert chosen.status_code == 200, chosen.text
    detail = _run(operator, batch_id, executor, rounds=12, until=("never",))
    wait = next(row for row in detail["step_runs"] if row["step_id"] == "w9")
    assert wait["state"] == "waiting", detail["step_runs"]
    assert operator.post(f"/api/batches/{batch_id}/hold", {"reason": "交班核对"}).status_code == 200
    with _session() as session:
        session.get(StepRun, wait["id"]).due_at = now()
        session.commit()
    executor()
    assert _detail(operator, batch_id)["state"] == "paused"
    resumed = _resume(operator, batch_id)
    assert resumed.status_code == 200, resumed.text
    detail = _detail(operator, batch_id)
    assert detail["state"] == "done", detail["failure_reason"]
    assert not [row for row in _commands(operator, batch_id, "dispatch") if row["step_index"] == 4]


# ---------- A20 人工判定后回到运行（WP7） ----------


def _gate_beside_a_waiting_branch(steps):
    """一支：干燥 → 质检（不合格保持）；另一支：静置 → 组装；最后测试等两支。"""
    dry, weigh, assemble, test = (dict((k, v) for k, v in step.items() if k != "hard") for step in steps)
    return [
        {**dry, "after": []},
        {"step_id": "g1", "kind": "gate", "name": "干燥水分质检", "after": [dry["step_id"]],
         "gate": {"source_step_id": dry["step_id"], "field": "water_ppm", "max": 20, "scope": "batch",
                  "on_fail": "hold"}},
        {"step_id": "w1", "name": "静置", "kind": "wait", "dur": 600, "after": [], "wait_for": {"mode": "duration"}},
        {**assemble, "after": ["w1"]},
        {**test, "after": ["g1", assemble["step_id"]]},
    ]


def test_gate_approval_releases_a_device_step_parked_during_the_hold(operator, qa, db, executor, measured, reset_runtime):
    from app.core.clock import now
    from app.models import StepRun

    batch_id = _new_batch(operator)
    _reshape(db, batch_id, _gate_beside_a_waiting_branch)
    measured["s01"] = [{"water_ppm": 50}]
    _dispatch(operator, batch_id)
    detail = _run(operator, batch_id, executor, rounds=20, until=("paused", "fault", "done"))
    assert detail["state"] == "paused", detail["failure_reason"]
    wait = next(row for row in detail["step_runs"] if row["step_id"] == "w1")
    with _session() as session:
        session.get(StepRun, wait["id"]).due_at = now()
        session.commit()
    executor()
    parked = next(row for row in _detail(operator, batch_id)["step_runs"] if row["step_index"] == 3)
    assert parked["state"] == "pending", "保持中开出的设备步骤先挂起"
    gate = next(row for row in _detail(operator, batch_id)["step_runs"] if row["step_id"] == "g1")
    decided = qa.post(f"/api/step-runs/{gate['id']}/gate-decision", {
        "conclusion": "approved", "reason": "复测 12 ppm，放行",
        "signature_id": qa.sign("质检判定属实", target=gate["id"]),
    })
    assert decided.status_code == 200, decided.text
    assert [row for row in _commands(operator, batch_id, "dispatch") if row["step_index"] == 3], "放行后挂起的设备步骤被下发"
    detail = _run(operator, batch_id, executor, rounds=30)
    assert detail["state"] == "done", detail["failure_reason"]


def test_gate_approval_does_not_erase_a_fault_on_another_branch(operator, qa, devices, db, executor):
    board = devices["use"]("ST-05", "ST-06")

    def shape(steps):
        dry, weigh, assemble, test = (dict((k, v) for k, v in step.items() if k != "hard") for step in steps)
        return [
            {**dry, "after": []},
            {"step_id": "g1", "kind": "gate", "name": "干燥水分质检", "after": [dry["step_id"]],
             "gate": {"source_step_id": dry["step_id"], "field": "water_ppm", "max": 20, "scope": "batch",
                      "on_fail": "hold"}},
            {**assemble, "after": []},
            {**test, "after": ["g1", assemble["step_id"]]},
        ]

    batch_id = _new_batch(operator)
    _reshape(db, batch_id, shape)
    _dispatch(operator, batch_id)
    executor()
    acting = {row["station_id"]: row["id"] for row in _commands(operator, batch_id, "dispatch")}
    board["finished"].add(acting["ST-05"])
    detail = _run(operator, batch_id, executor, rounds=6, until=("paused", "fault"))
    assert detail["state"] == "paused" and "质检关卡待人工判断" in detail["failure_reason"], detail["failure_reason"]
    board["query"][acting["ST-06"]] = "failed"
    detail = _run(operator, batch_id, executor, rounds=6, until=("fault",))
    assert detail["state"] == "fault"
    gate = next(row for row in detail["step_runs"] if row["step_id"] == "g1")
    decided = qa.post(f"/api/step-runs/{gate['id']}/gate-decision", {
        "conclusion": "approved", "reason": "干燥读数复核合格",
        "signature_id": qa.sign("质检判定属实", target=gate["id"]),
    })
    assert decided.status_code == 200, decided.text
    detail = _detail(operator, batch_id)
    assert detail["state"] == "fault", "另一条分支的故障不能被关卡判定一并解除"
    events = operator.get(f"/api/exceptions?batch_id={batch_id}").json()
    assert any(row["source_type"] == "command" and row["state"] in {"open", "manual"} for row in events), events
    assert operator.get(f"/api/batches/{batch_id}/recovery-options").status_code == 200


def test_approving_one_of_two_held_gates_keeps_the_batch_paused(operator, qa, devices, db, executor):
    """两条并行分支上的关卡都挂起：判定其中一个，批次仍保持（另一个还在等 QA）；两个都判定后才回到运行。"""
    board = devices["use"]("ST-05", "ST-06")
    batch_id = _new_batch(operator)

    def shape(steps):
        dry, weigh, assemble, test = (dict((k, v) for k, v in step.items() if k != "hard") for step in steps)
        return [
            {**dry, "after": []},
            {"step_id": "g1", "kind": "gate", "name": "干燥水分质检", "after": [dry["step_id"]],
             "gate": {"source_step_id": dry["step_id"], "field": "water_ppm", "max": 20, "scope": "batch",
                      "on_fail": "hold"}},
            {**assemble, "after": []},
            {"step_id": "g2", "kind": "gate", "name": "封口气密质检", "after": [assemble["step_id"]],
             "gate": {"source_step_id": assemble["step_id"], "field": "leak", "max": 1, "scope": "batch",
                      "on_fail": "hold"}},
            {**test, "after": ["g1", "g2"]},
        ]

    _reshape(db, batch_id, shape)
    _dispatch(operator, batch_id)
    executor()
    acting = [row["id"] for row in _commands(operator, batch_id, "dispatch")]
    assert len(acting) == 2, acting
    # 两台设备同一轮回报完成，回执里都没有测量值：两个关卡都转人工
    board["finished"].update(acting)
    detail = _run(operator, batch_id, executor, rounds=6, until=("never",))
    gates = {row["step_id"]: row for row in detail["step_runs"] if row["kind"] == "gate"}
    assert set(gates) == {"g1", "g2"} and {row["state"] for row in gates.values()} == {"ready"}, detail["step_runs"]
    assert detail["state"] == "paused"

    def approve(run):
        decided = qa.post(f"/api/step-runs/{run['id']}/gate-decision", {
            "conclusion": "approved", "reason": "复测合格", "signature_id": qa.sign("质检判定属实", target=run["id"]),
        })
        assert decided.status_code == 200, decided.text

    approve(gates["g1"])
    assert _detail(operator, batch_id)["state"] == "paused", "另一个关卡还在等判定"
    approve(gates["g2"])
    detail = _run(operator, batch_id, executor, rounds=30)
    assert detail["state"] == "done", detail["failure_reason"]


# ---------- A04 在途批次按固化版本确认 SOP（WP8） ----------


def test_inflight_batch_on_a_superseded_sop_can_be_acknowledged_for_that_batch(
    researcher, qa, operator, reset_runtime,
):
    code = f"SOP-A04-{uuid4().hex[:6]}"
    v1 = publish_sop(researcher, qa, code, "v1", capability_scope=["cap.mix"], requires_training_ack=True)
    plan = approved_plan(researcher, qa, released_recipe(researcher, qa, v1["id"]))
    created = operator.post("/api/batches", {"plan_id": plan})
    assert created.status_code == 201, created.text
    batch_id = created.json()["id"]
    publish_sop(researcher, qa, code, "v2", capability_scope=["cap.mix"], requires_training_ack=True)
    assert operator.post(f"/api/batches/{batch_id}/schedule", {}).status_code == 200

    def ack_check():
        checks = operator.get(f"/api/batches/{batch_id}/preflight?manual_review=true").json()["checks"]
        return next(row for row in checks if row["key"] == "sop_ack")

    assert ack_check()["state"] == "blocked"
    assert operator.post(f"/api/sops/{v1['id']}/acknowledge").status_code == 409, "通用确认仍拒绝已被取代的版本"
    acked = operator.post(f"/api/batches/{batch_id}/sop-ack")
    assert acked.status_code == 200, acked.text
    assert ack_check()["state"] == "pass"
    assert _sign_dispatch(operator, batch_id).status_code == 200


# ---------- A05 整批重排同步计划起点（WP10） ----------


def test_full_reschedule_moves_the_planned_start_so_a_tail_reschedule_waits_for_the_wait(operator, devices, db):
    from datetime import timedelta

    from app.core.clock import now
    from app.models import Batch

    devices["use"]("ST-05", "ST-06")

    def shape(steps):
        dry, weigh, assemble, test = steps
        return [
            {"step_id": "prep", "name": "备料静置", "kind": "wait", "dur": 60, "after": [],
             "wait_for": {"mode": "duration"}},
            {**dry, "after": ["prep"]},
            {**test, "after": [dry["step_id"]]},
        ]

    batch_id = _new_batch(operator)
    _reshape(db, batch_id, shape)
    assert operator.post(f"/api/batches/{batch_id}/schedule", {}).status_code == 200
    later = (now() + timedelta(hours=5)).replace(microsecond=0)
    moved = operator.post(f"/api/batches/{batch_id}/reschedule", {"from_step": 0, "start_from": later.isoformat()})
    assert moved.status_code == 200, moved.text
    with _session() as session:
        assert session.get(Batch, batch_id).planned_start_at == later
    tail = operator.post(f"/api/batches/{batch_id}/reschedule", {
        "from_step": 1, "start_from": (later + timedelta(minutes=10)).isoformat(),
    })
    assert tail.status_code == 200, tail.text
    from app.models import Allocation

    with _session() as session:
        dry = session.query(Allocation).filter(
            Allocation.batch_id == batch_id, Allocation.step_index == 1, Allocation.kind == "work",
        ).one()
        assert dry.starts_at >= later + timedelta(minutes=60), (dry.starts_at, later)
    assert_consistent(batch_id)


# ---------- A10 / A15 / A16 阅读确认与子流程 SOP（WP8） ----------


def _released(researcher, qa, steps: list[dict], sop_version_id: str = "") -> str:
    recipe_id = researcher.post("/api/recipes", {"name": f"SOP 用例流程 {uuid4().hex[:4]}", "plate": 4}).json()["id"]
    body = {"risk": "RA-sop v1", "bom": [], "steps": steps}
    if sop_version_id:
        body["sop_version_id"] = sop_version_id
    patched = researcher.patch(f"/api/recipes/{recipe_id}", body)
    assert patched.status_code == 200, patched.text
    assert researcher.post(f"/api/recipes/{recipe_id}/submit").status_code == 200
    for target, meaning in (("approved", "批准"), ("released", "发布")):
        moved = qa.post(f"/api/recipes/{recipe_id}/transition",
                        {"target_state": target, "signature_id": qa.sign_recipe(meaning, recipe_id)})
        assert moved.status_code == 200, moved.text
    return recipe_id


MANUAL_ONLY = [{"kind": "manual", "name": "人工核对", "dur": 10,
                "form": [{"key": "checked", "label": "已核对", "type": "bool", "required": True}]}]


def _ack_check(operator, batch_id: str) -> dict:
    checks = operator.get(f"/api/batches/{batch_id}/preflight?manual_review=true").json()["checks"]
    return next(row for row in checks if row["key"] == "sop_ack")


def test_manual_only_sop_flow_still_requires_the_read_confirmation(researcher, qa, operator, reset_runtime):
    """A10 / A16：纯人工流程没有需资质的节点，阅读确认照样要；提交人工记录的人也要确认过。"""
    code = f"SOP-A10-{uuid4().hex[:6]}"
    version = publish_sop(researcher, qa, code, "v1", requires_training_ack=True)
    plan = approved_plan(researcher, qa, _released(researcher, qa, MANUAL_ONLY, version["id"]))
    batch_id = operator.post("/api/batches", {"plan_id": plan}).json()["id"]
    assert operator.post(f"/api/batches/{batch_id}/schedule", {}).status_code == 200
    assert _ack_check(operator, batch_id)["state"] == "blocked"
    assert _sign_dispatch(operator, batch_id).status_code == 409
    assert operator.post(f"/api/batches/{batch_id}/sop-ack").status_code == 200
    assert _ack_check(operator, batch_id)["state"] == "pass"
    assert _sign_dispatch(operator, batch_id).status_code == 200
    run = next(row for row in _detail(operator, batch_id)["step_runs"] if row["kind"] == "manual")
    body = {"form_data": {"checked": True}, "checks": {"samples": True}}
    refused = researcher.post(f"/api/step-runs/{run['id']}/submit", {
        **body, "signature_id": researcher.sign("人工记录确认", target=run["id"]),
    })
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "sop_ack_required", refused.text
    submitted = operator.post(f"/api/step-runs/{run['id']}/submit", {
        **body, "signature_id": operator.sign("人工记录确认", target=run["id"]),
    })
    assert submitted.status_code == 200, submitted.text


def test_recovery_requires_the_executor_to_have_confirmed_the_frozen_sop(researcher, qa, operator, db, reset_runtime):
    """A16：恢复运行时再核一次阅读确认（改派、撤销之后执行人可能没有确认记录）。"""
    from app.models import SopAck

    code = f"SOP-A16-{uuid4().hex[:6]}"
    version = publish_sop(researcher, qa, code, "v1", capability_scope=["cap.mix"], requires_training_ack=True)
    plan = approved_plan(researcher, qa, released_recipe(researcher, qa, version["id"]))
    batch_id = operator.post("/api/batches", {"plan_id": plan}).json()["id"]
    assert operator.post(f"/api/batches/{batch_id}/schedule", {}).status_code == 200
    assert operator.post(f"/api/sops/{version['id']}/acknowledge").status_code == 200
    assert _sign_dispatch(operator, batch_id).status_code == 200
    assert operator.post(f"/api/batches/{batch_id}/hold", {"reason": "交班"}).status_code == 200
    db.query(SopAck).filter(SopAck.sop_version_id == version["id"]).delete(synchronize_session=False)
    db.commit()
    refused = _resume(operator, batch_id)
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "sop_ack_required", refused.text
    assert operator.post(f"/api/batches/{batch_id}/sop-ack").status_code == 200
    assert _resume(operator, batch_id).status_code == 200


def test_subflow_sop_is_frozen_checked_and_required_for_new_batches(researcher, qa, operator, reset_runtime):
    """A15：子流程关联的 SOP 按生效版本解析并固化；开跑检查与阅读确认覆盖它；没有生效版本时不建批次。"""
    code = f"SOP-A15-{uuid4().hex[:6]}"
    version = publish_sop(researcher, qa, code, "v1", capability_scope=["cap.mix"], requires_training_ack=True)
    sub = released_recipe(researcher, qa, version["id"])
    top = _released(researcher, qa, [
        {"step_id": "s01", "name": "前处理", "kind": "subflow", "subflow": {"recipe_id": sub}},
        {"step_id": "s02", "kind": "device", "name": "复混", "cap": "cap.mix",
         "params": {"temp": 25, "rpm": 2000}, "dur": 5},
    ])
    plan = approved_plan(researcher, qa, top)
    created = operator.post("/api/batches", {"plan_id": plan})
    assert created.status_code == 201, created.text
    batch_id = created.json()["id"]
    detail = _detail(operator, batch_id)
    assert detail["sop_snapshot"] == {} and detail["snapshot"]["subflows"][0]["sop"]["sop_version_id"] == version["id"]
    assert operator.post(f"/api/batches/{batch_id}/schedule", {}).status_code == 200
    checks = operator.get(f"/api/batches/{batch_id}/preflight?manual_review=true").json()["checks"]
    sop = next(row for row in checks if row["key"] == "sop")
    assert sop["state"] == "pass" and "子流程" in sop["detail"], sop
    assert _ack_check(operator, batch_id)["state"] == "blocked"
    acked = operator.post(f"/api/batches/{batch_id}/sop-ack")
    assert acked.status_code == 200 and acked.json()["acked"], acked.text
    assert _ack_check(operator, batch_id)["state"] == "pass"
    retired = qa.post(f"/api/sops/{version['id']}/retire", {"reason": "工艺停用"})
    assert retired.status_code == 200, retired.text
    refused = operator.post("/api/batches", {"plan_id": approved_plan(researcher, qa, top)})
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "sop_not_effective", refused.text


# ---------- A12 手动重排看上游（WP11） ----------


def test_manual_reschedule_cannot_start_before_the_upstream_batch_ends(researcher, operator, db, reset_runtime):
    from datetime import timedelta

    from app.core.clock import now
    from app.models import Allocation
    from test_core_chain_review import _task, _task_batch

    upstream, downstream = _task(researcher), _task(researcher)
    assert researcher.put(
        f"/api/experiment-tasks/{downstream['id']}/dependencies", {"depends_on": [upstream["id"]]},
    ).status_code == 200
    up_batch, down_batch = _task_batch(operator, upstream["id"]), _task_batch(operator, downstream["id"])
    assert operator.post(f"/api/batches/{up_batch}/schedule", {}).status_code == 200
    assert operator.post(f"/api/batches/{down_batch}/schedule", {}).status_code == 200
    with _session() as session:
        upstream_end = max(row.ends_at for row in session.query(Allocation).filter(
            Allocation.batch_id == up_batch, Allocation.kind == "work"))
    moved = operator.post(f"/api/batches/{down_batch}/reschedule", {
        "from_step": 0, "start_from": (now() + timedelta(minutes=5)).isoformat(),
    })
    assert moved.status_code == 200, moved.text
    with _session() as session:
        first = min(row.starts_at for row in session.query(Allocation).filter(
            Allocation.batch_id == down_batch, Allocation.kind == "work"))
    assert first >= upstream_end, (first, upstream_end)
    assert_consistent(down_batch)


# ---------- A13 / A14 / A24 撤回、终止与拒发收尾（WP12） ----------


def test_withdrawing_a_transfer_also_withdraws_the_action_waiting_for_it(operator, running_batch, db, executor):
    """A13：批次不在运行时执行器撤回排队的转运，等它的设备动作一并撤回，不留在队列里挡住续跑。"""
    from app.models import Batch, Command

    with _session() as session:
        batch = session.get(Batch, running_batch)
        session.query(Command).filter(Command.batch_id == running_batch, Command.state == "sent").update(
            {"state": "cancelled", "delivery_state": "not_sent"}, synchronize_session=False)
        transfer = Command(org_id=batch.org_id, batch_id=running_batch, station_id="AGV-01", capability="cap.transfer",
                           params={}, type="transfer", step_index=1)
        session.add(transfer)
        session.flush()
        action = Command(org_id=batch.org_id, batch_id=running_batch, station_id="ST-05", capability="cap.weigh",
                         params={}, type="dispatch", step_index=1, after_command_id=transfer.id)
        session.add(action)
        batch.state = "paused"
        session.commit()
        transfer_id, action_id = transfer.id, action.id
    executor()
    with _session() as session:
        assert session.get(Command, transfer_id).state == "cancelled"
        waiting = session.get(Command, action_id)
        assert (waiting.state, waiting.delivery_state) == ("cancelled", "not_sent"), waiting.error


def test_abort_leaves_the_labware_of_an_unsettled_transfer_marked_lost(operator, clean_labware, db, reset_runtime):
    """A14：终止收尾时，报了失败或结论不明的转运：载具可能已被拿起，位置标为未知，不能当成还在原位。"""
    from app.core.context import system_context
    from app.models import Batch, Command, Labware
    from app.services.execution_service import ExecutionService

    batch_id = _new_batch(operator)
    board = _register(operator, "HOTEL-01/S03", type_id="LT-PLATE-96")
    assert operator.post(f"/api/batches/{batch_id}/labware", {"labware_id": board["id"]}).status_code == 200
    with _session() as session:
        batch = session.get(Batch, batch_id)
        batch.state = "aborting"
        session.add(Command(
            org_id=batch.org_id, batch_id=batch_id, station_id="AGV-01", capability="cap.transfer", params={},
            type="transfer", step_index=0, state="unknown", delivery_state="delivered", outcome="failed",
            labware_id=board["id"], error="适配器明确失败：夹爪故障",
        ))
        session.commit()
    with _session() as session:
        outcome = ExecutionService(session, system_context("ORG-001")).finish_abort_if_stopped(batch_id)
        session.commit()
    assert outcome["finished"] and outcome["state"] == "aborted", outcome
    with _session() as session:
        labware = session.get(Labware, board["id"])
        assert labware.state == "lost" and labware.location_id is None


def test_hard_window_refusal_is_recorded_as_never_delivered(
    operator, scripted, running_batch, db, executor, monkeypatch,
):
    """A24：投递前发现硬时限已被触碰：设备没见过这条指令，按未投递记，不要求现场核查。

    硬时限何时被触碰取决于检查点时刻，这里直接让称重的硬时限判为已触碰，只看拒发怎么记。
    """
    from app.models import Command
    from app.services.execution_service import ExecutionService

    monkeypatch.setattr(
        ExecutionService, "check_hard_window",
        lambda self, batch, command: "硬时限 15 min 已被触碰（超出 100 min），批次挂起" if command.step_index == 1 else None,
    )
    devices = scripted["use"]("ST-05")
    executor()
    dry = _commands(operator, running_batch, "dispatch")[0]
    devices["finished"].add(dry["id"])
    for _ in range(3):
        executor()
    weigh = next(row for row in _commands(operator, running_batch, "dispatch") if row["step_index"] == 1)
    with _session() as session:
        refused = session.get(Command, weigh["id"])
        assert (refused.state, refused.delivery_state, refused.started_at) == ("unknown", "unreachable", None)
        assert "硬时限" in refused.error
    assert not [call for call in devices["calls"] if call[2] == weigh["id"]], "设备没有收到这条指令"
    evaluation = operator.get(f"/api/batches/{running_batch}/recovery-options").json()
    assert evaluation["blind_retry_allowed"], "设备没见过的指令不要求现场核查"


# ---------- A22 / A23 样本类型与 SOP 生效时间（WP13） ----------


def test_sop_effective_date_cannot_be_backdated_silently(researcher, qa, reset_runtime):
    from datetime import datetime, timedelta

    from app.models import SopVersion

    code = f"SOP-A23-{uuid4().hex[:6]}"
    v1 = publish_sop(researcher, qa, code, "v1", capability_scope=["cap.mix"])
    with _session() as session:
        # v1 早已生效（一个月前）：回溯到两小时前仍晚于它的起始，是「回溯取代」而不是「发布即作废」
        session.get(SopVersion, v1["id"]).effective_from = datetime.utcnow() - timedelta(days=30)
        session.commit()

    def draft(version):
        uploaded = researcher.upload("/api/files", f"{code}-{version}.pdf", PDF, "application/pdf")
        created = researcher.post("/api/sops", {"code": code, "title": f"{code} 指导书", "version": version,
                                                "file_id": uploaded.json()["id"]})
        assert created.status_code == 201, created.text
        assert researcher.post(f"/api/sops/{created.json()['id']}/submit").status_code == 200
        return created.json()["id"]

    def approve(version_id, effective_from, reason=""):
        return qa.post(f"/api/sops/{version_id}/decision", {
            "conclusion": "approved", "effective_from": effective_from.isoformat(), "reason": reason,
            "signature_id": qa.sign("批准 SOP", target=version_id),
        })

    v2 = draft("v2")
    dead = approve(v2, datetime.utcnow() - timedelta(days=400))
    assert dead.status_code == 422 and dead.json()["detail"]["code"] == "sop_effective_before_current", dead.text
    silent = approve(v2, datetime.utcnow() - timedelta(hours=2))
    assert silent.status_code == 422 and silent.json()["detail"]["code"] == "sop_backdate_reason_required", silent.text
    explained = approve(v2, datetime.utcnow() - timedelta(hours=2), "现场已于两小时前按新版执行，补登发布")
    assert explained.status_code == 200, explained.text


def test_untyped_samples_are_reported_as_unchecked_not_in_scope(researcher, qa, operator, reset_runtime):
    """A22：SOP 限定了样本类型，批次样本没登记类型：开跑检查提醒无法核对，不显示「在适用范围内」。"""
    code = f"SOP-A22-{uuid4().hex[:6]}"
    version = publish_sop(researcher, qa, code, "v1", capability_scope=["cap.mix"], sample_types=["极片"])
    batch_id = operator.post("/api/batches", {
        "plan_id": approved_plan(researcher, qa, released_recipe(researcher, qa, version["id"])),
    }).json()["id"]
    assert operator.post(f"/api/batches/{batch_id}/schedule", {}).status_code == 200
    checks = operator.get(f"/api/batches/{batch_id}/preflight?manual_review=true").json()["checks"]
    sop = next(row for row in checks if row["key"] == "sop")
    assert sop["state"] == "warn" and "没有登记样本类型" in sop["detail"], sop
