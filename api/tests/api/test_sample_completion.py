"""运行分配什么时候算完成：批次跑完、名下未取消的检测任务都采集齐（`AnalysisService.settle_assignment`）。

以前只有历史三指标回传会记完成：类型化结果（结果回传、人工录入、设备回报）写齐了，运行分配仍是 running，
结果分析「运行分配」与看板样品完成数一直是 0。已跑完批次的存量由迁移 0043 按同一规则回填。
"""
from test_core_chain_review import CAPACITY, _new_batch, _session
from test_schema_guard import scratch_database  # noqa: F401  （复用 fixture）
from test_station_asset_source import _alembic, _insert


def test_assignment_is_done_only_once_the_batch_finished_with_every_task_collected(researcher, operator, reset_runtime):
    from app.core.context import system_context
    from app.models import AnalysisTask, Batch, Sample
    from app.services.analysis_service import AnalysisService
    from app.services.workflow_service import WorkflowService

    batch_id = _new_batch(operator)
    with _session() as session:
        batch = session.get(Batch, batch_id)
        batch.state = "running"
        org = batch.org_id
        rows = session.query(Sample).filter(Sample.batch_id == batch_id).order_by(Sample.position).all()
        for row in rows:
            row.state = "running"
        collected, open_, failed, cancelled, bare = rows[:5]
        failed.state = "failed"

        def task(sample: Sample, state: str) -> str:
            row = AnalysisTask(org_id=org, sample_id=sample.id, physical_sample_id=sample.physical_sample_id,
                               method="EC", required_metrics=[CAPACITY], state=state)
            session.add(row)
            session.flush()
            return row.id

        first = task(collected, "collected")
        task(open_, "collected")
        pending = task(open_, "pending")
        task(failed, "collected")
        task(cancelled, "collected")
        task(cancelled, "cancelled")
        session.commit()
        ids = [row.id for row in (collected, open_, failed, cancelled, bare)]

    def states() -> list[str]:
        with _session() as session:
            return [session.get(Sample, key).state for key in ids]

    # 批次还在跑：结果都到齐了也不提前完成（保持、故障时的恢复评估与影响范围数的是没做完的样本）
    with _session() as session:
        analysis = AnalysisService(session, system_context(org))
        assert not any(analysis.settle_assignment(key) for key in ids)
        session.commit()
    assert states() == ["running", "running", "failed", "running", "running"]

    # 批次跑完：采集齐的记完成，取消的任务不算；有任务没采集齐、没有检测任务的仍在等结果；已失败的不动
    with _session() as session:
        WorkflowService(session, system_context(org)).finish_batch(session.get(Batch, batch_id))
        session.commit()
    assert states() == ["done", "running", "failed", "done", "running"]

    # 取消最后一个没采集齐的任务：这个运行分配的结果就到齐了
    dropped = researcher.post(f"/api/analysis-tasks/{pending}/cancel", {"reason": "改由另一台仪器测"})
    assert dropped.status_code == 200, dropped.text
    assert states() == ["done", "done", "failed", "done", "running"]

    # 批次跑完以后才回来的结果（人工录入同结果回传一条路）：采集齐的那一刻记完成
    created = researcher.post("/api/analysis-tasks", {
        "sample_id": ids[4], "method": "EC", "required_metrics": [CAPACITY],
    })
    assert created.status_code == 201, created.text
    entered = researcher.post(f"/api/analysis-tasks/{created.json()['id']}/results", {
        "event_id": f"late-{ids[4]}", "metrics": [{"metric_version_id": CAPACITY, "value": 204.0, "unit": "mAh/g"}],
    })
    assert entered.status_code == 200, entered.text
    assert states() == ["done", "done", "failed", "done", "done"]

    # 只进不退：完成之后再开重测，不改回去（与历史三指标回传一致）
    retest = researcher.post(f"/api/analysis-tasks/{first}/retests", {"reason": "复核发现读数可疑"})
    assert retest.status_code == 201, retest.text
    assert states()[0] == "done"

    listed = next(row for row in researcher.get("/api/results").json() if row["batch_id"] == batch_id)
    assert listed["sample_done"] == 4


def test_upgrade_marks_collected_assignments_of_finished_batches_done(scratch_database):
    """0043：已完成批次里检测任务都采集齐的运行分配回填为完成；在途批次、任务没采集齐、没有任务、已失败的不动。"""
    from sqlalchemy import MetaData, create_engine, text

    _alembic(scratch_database, "upgrade", "0042_formulation_templates")
    engine = create_engine(scratch_database)
    try:
        metadata = MetaData()
        metadata.reflect(bind=engine, only=["organizations", "recipes", "plans", "batches", "samples", "analysis_tasks"])
        cases = {
            # 运行分配：（批次，状态，名下检测任务的状态）
            "S-ALL": ("B-DONE", "running", ["collected"]),
            "S-WITH-CANCELLED": ("B-DONE", "running", ["collected", "cancelled"]),
            "S-OPEN": ("B-DONE", "running", ["collected", "pending"]),
            "S-BARE": ("B-DONE", "running", []),
            "S-ONLY-CANCELLED": ("B-DONE", "running", ["cancelled"]),
            "S-FAILED": ("B-DONE", "failed", ["collected"]),
            "S-IN-FLIGHT": ("B-RUN", "running", ["collected"]),
        }
        with engine.begin() as connection:
            _insert(connection, metadata, "organizations", id="ORG-M", name="迁移测试")
            _insert(connection, metadata, "recipes", id="R-M", org_id="ORG-M", name="迁移测试流程", steps=[])
            _insert(connection, metadata, "plans", id="P-M", org_id="ORG-M", recipe_id="R-M", name="迁移测试方案")
            for batch_id, state in (("B-DONE", "done"), ("B-RUN", "running")):
                _insert(connection, metadata, "batches", id=batch_id, org_id="ORG-M", plan_id="P-M", recipe_id="R-M",
                        state=state)
            for position, (sample_id, (batch_id, state, tasks)) in enumerate(cases.items()):
                _insert(connection, metadata, "samples", id=sample_id, org_id="ORG-M", batch_id=batch_id,
                        well=f"A{position + 1}", position=position, state=state)
                for index, task_state in enumerate(tasks):
                    _insert(connection, metadata, "analysis_tasks", id=f"{sample_id}-T{index}", org_id="ORG-M",
                            sample_id=sample_id, method="EC", required_metrics=[], state=task_state)

        _alembic(scratch_database, "upgrade", "head")
        with engine.connect() as connection:
            states = dict(connection.execute(text("SELECT id, state FROM samples")).all())
        assert states == {
            "S-ALL": "done", "S-WITH-CANCELLED": "done", "S-OPEN": "running", "S-BARE": "running",
            "S-ONLY-CANCELLED": "running", "S-FAILED": "failed", "S-IN-FLIGHT": "running",
        }
    finally:
        engine.dispose()
