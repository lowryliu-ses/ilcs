"""跨场景的不变量：回归用例在关键动作之后调用，任何一条不成立都说明状态之间已经不一致。

1. 本批次的时间窗不与其他批次在同一工位上超出通道数，也不让共享资产超容量（维护 / 校准预约计入）；
2. 还没开出的设备步骤不早于它的每个前驱的计划结束，也不早于上游批次结束；
3. 还会动作的指令，逐孔参数只落在在用样本的实体孔位上；
4. 同一步骤至多一条活动动作指令（续跑 / 重试接续被保持的动作是同一个动作，不算两条）；
5. 运行中的批次总有可推进的对象（与执行器活性检查同一判定）。
"""
from __future__ import annotations

from datetime import timedelta

TOLERANCE = timedelta(seconds=1)


def violations(db, batch_id: str, *, timing: bool = True) -> list[str]:
    from app.core.context import system_context
    from app.domain import graph
    from app.domain.liveness import stall_reason
    from app.domain.steps import normalize
    from app.models import Allocation, Batch, Command, Sample, StepRun, WorkflowEvent
    from app.repositories.execution import DISPATCHING, still_occupying
    from app.services.sample_service import SampleService
    from app.services.schedule_service import ScheduleService

    db.expire_all()
    batch = db.get(Batch, batch_id)
    assert batch is not None, batch_id
    ctx = system_context(batch.org_id, "不变量检查")
    schedule = ScheduleService(db, ctx)
    found: list[str] = []

    # 1 工位通道与共享资产
    for row in schedule._overlaps():
        pair = {row["a"]["batch_id"], row["b"]["batch_id"]}
        if batch_id in pair and len(pair) == 2:
            found.append(f"工位 {row['station_id']} 上 {' 与 '.join(sorted(pair))} 重叠 {row['overlap_min']} min")
    found.extend(f"资产超容量：{text}" for text in schedule._asset_overloads(batch_id))

    # 2 未开出的设备步骤不早于前驱与上游
    steps = normalize(batch.recipe_snapshot.get("steps") or [])
    if timing and steps and batch.state not in {"done", "aborted"}:
        frozen = schedule.frozen_steps(batch)
        ends = schedule.step_ends(batch)
        before = graph.predecessors(steps)
        work = [
            row for row in db.query(Allocation).filter(Allocation.batch_id == batch_id, Allocation.kind == "work").all()
            if row.step_index not in frozen
        ]
        for row in work:
            for parent in before[row.step_index] if row.step_index < len(before) else []:
                if parent in ends and row.starts_at + TOLERANCE < ends[parent]:
                    found.append(
                        f"第 {row.step_index + 1} 步 {row.starts_at:%H:%M} 开工，早于前驱第 {parent + 1} 步结束 "
                        f"{ends[parent]:%H:%M}"
                    )
        floor, _ = schedule.dependency_floor(batch)
        if floor is not None and work:
            first = min(row.starts_at for row in work)
            if first + TOLERANCE < floor:
                found.append(f"未开出步骤 {first:%H:%M} 开工，早于上游批次结束 {floor:%H:%M}")

    # 3 逐孔参数只落在在用样本上
    samples = db.query(Sample).filter(Sample.batch_id == batch_id).all()
    positions = SampleService(db, ctx).device_wells(batch_id)
    active = {positions.get(sample.id, sample.well) for sample in samples if sample.state not in {"failed", "split"}}
    commands = db.query(Command).filter(Command.batch_id == batch_id).order_by(Command.created_at).all()
    for command in commands:
        live = (command.state == "sent" and command.delivery_state == "queued") or command.state in {"accepted", "running"}
        wells = set((command.params or {}).get("wells") or {})
        if live and wells - active:
            found.append(f"指令 {command.id[:8]} 的孔位 {sorted(wells - active)} 不属于在用样本")

    # 4 同一步骤至多一条活动动作指令
    by_step: dict[int, list] = {}
    for command in commands:
        if command.type not in DISPATCHING:
            continue
        if (command.state == "sent" and command.delivery_state == "queued") or still_occupying(command):
            by_step.setdefault(command.step_index, []).append(command)
    for index, live in by_step.items():
        acting = [command for command in live if command.state != "held"]
        held = [command for command in live if command.state == "held"]
        if len(acting) > 1:
            found.append(f"第 {index + 1} 步同时有 {len(acting)} 条活动动作指令")
        elif acting and held and acting[0].target_command_id not in {row.id for row in held}:
            found.append(f"第 {index + 1} 步有被保持的动作，另一条动作指令却不是接续它")

    # 5 运行中的批次总有可推进的对象
    runs = db.query(StepRun).filter(StepRun.batch_id == batch_id).all()
    pending = db.query(WorkflowEvent).filter(
        WorkflowEvent.batch_id == batch_id, WorkflowEvent.state.in_(["pending", "processing"]),
    ).count()
    reason = stall_reason(batch.state, runs, commands, pending)
    if reason:
        found.append(f"批次停滞：{reason}")
    return found


def assert_consistent(batch_id: str, *, timing: bool = True) -> None:
    from app.core.db import SessionLocal

    with SessionLocal() as db:
        found = violations(db, batch_id, timing=timing)
    assert not found, "；".join(found)
