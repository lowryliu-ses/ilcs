"""逐孔回报的遥测：每个点按孔位关联样本（与设备回报结果同一口径：这一步的设备孔位 → 在用样本），按这一孔自己的设备时间
入库；不带孔位的点照旧按整条回执的时间记。"""
from datetime import timedelta

from test_failure_paths import running_batch  # noqa: F401  （复用 fixture）


def test_per_well_telemetry_lands_on_each_sample_at_its_own_time(operator, running_batch, executor):
    from app.adapters.base import CommandResult, TelemetryPoint
    from app.core.clock import now
    from app.core.context import system_context
    from app.core.db import SessionLocal
    from app.domain.steps import normalize
    from app.models import Batch, Command, Telemetry
    from app.services.batch_service import BatchService
    from app.services.execution_service import ExecutionService

    for _ in range(3):
        executor()
    with SessionLocal() as db:
        batch = db.get(Batch, running_batch)
        command = db.query(Command).filter(Command.batch_id == batch.id, Command.type == "dispatch").first()
        assert command is not None, "第一个设备步骤应当已经生成指令"
        step = normalize(batch.recipe_snapshot["steps"])[command.step_index]
        ctx = system_context(batch.org_id, "执行器")
        targets = BatchService(db, ctx)._step_targets(batch, step)
        wells = sorted(targets)[:2]
        assert len(wells) == 2, targets
        start = now().replace(microsecond=0)
        points = tuple(
            TelemetryPoint("temp", 60.5 + index, 60.0 + index, well, start + timedelta(seconds=5 * index))
            for index, well in enumerate(wells)
        )
        result = CommandResult(
            command_id=command.id, state="done", device_ts=start + timedelta(seconds=30), quality="good",
            telemetry=(*points, TelemetryPoint("pressure", 1.0)), origin="real:test-wells",
        )
        ExecutionService(db, ctx).record_telemetry(batch, command, step, result)
        db.commit()

        rows = db.query(Telemetry).filter(Telemetry.command_id == command.id,
                                          Telemetry.origin == "real:test-wells").all()
        per_well = {(row.sample_id, row.setpoint, row.device_ts) for row in rows if row.metric == "temp"}
        assert per_well == {
            (targets[well].physical_sample_id or targets[well].id, 60.0 + index, start + timedelta(seconds=5 * index))
            for index, well in enumerate(wells)
        }, "逐孔的点落到各自的样本上、按各孔自己的时间记"
        pressure = next(row for row in rows if row.metric == "pressure")
        assert pressure.sample_id == "" and pressure.device_ts == result.device_ts, "不带孔位的点照旧按整条回执记"
