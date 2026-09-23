"""中控吞吐相关的三处修正：并行通道守门、指令队列只取可投递项、推进器按组织领取事件。"""
from datetime import timedelta

from test_failure_paths import running_batch  # noqa: F401
from tests.conftest import ISOLATED_ORG, ORG


def _new_batch(operator) -> str:
    created = operator.post("/api/batches", {"plan_id": "EP-205-01"})
    assert created.status_code == 201, created.text
    return created.json()["id"]


def _allocate(db, batch_id: str, station_id: str, start, minutes: int = 45, step_index: int = 3):
    from app.models import Allocation

    db.add(Allocation(
        batch_id=batch_id, step_index=step_index, station_id=station_id,
        starts_at=start, ends_at=start + timedelta(minutes=minutes), kind="work",
    ))


def test_multi_channel_station_accepts_overlap_up_to_its_channels(operator, reset_runtime, db):
    """8 通道充放电柜同时承接两个批次是正常排程，守门不能把它当成冲突拒绝。"""
    from app.core.clock import now
    from app.core.context import system_context
    from app.services.schedule_service import ScheduleService

    first, second = _new_batch(operator), _new_batch(operator)
    start = now() + timedelta(hours=30)
    _allocate(db, first, "ST-07", start)
    _allocate(db, second, "ST-07", start + timedelta(minutes=5))
    # 单通道工位仍然是任意重叠即冲突
    _allocate(db, first, "ST-06", start, step_index=2)
    _allocate(db, second, "ST-06", start + timedelta(minutes=10), step_index=2)
    db.commit()

    service = ScheduleService(db, system_context(ORG, "测试"))
    stations = {row["station_id"] for row in service.conflicts()}
    assert "ST-07" not in stations
    assert "ST-06" in stations


def test_multi_channel_station_still_refuses_overlap_beyond_capacity(operator, reset_runtime, db):
    from app.core.clock import now
    from app.core.context import system_context
    from app.services.schedule_service import ScheduleService

    filler, extra = _new_batch(operator), _new_batch(operator)
    start = now() + timedelta(hours=40)
    for channel in range(8):
        _allocate(db, filler, "ST-07", start, step_index=channel)
    _allocate(db, extra, "ST-07", start + timedelta(minutes=1))
    db.commit()

    clashes = [
        row for row in ScheduleService(db, system_context(ORG, "测试"))._overlaps()
        if row["station_id"] == "ST-07" and row["b"]["batch_id"] == extra
    ]
    assert len(clashes) == 8, "第 9 个同时进行的时间窗超出 8 通道，逐一报与在用通道的重叠"
    assert clashes[0]["channels"] == 8


def test_executor_queue_skips_commands_not_yet_due(running_batch, db):
    """未到时间窗的指令不进候选：它们多于 LIMIT 时，已到点的指令也必须取得到。"""
    from app.core.clock import now
    from app.models import Batch, Command
    from app.repositories.execution import CommandRepository

    batch = db.get(Batch, running_batch)
    later = now() + timedelta(hours=2)
    for index in range(60):
        db.add(Command(
            org_id=batch.org_id, batch_id=batch.id, station_id="ST-05", capability="cap.vacuum_dry",
            params={}, type="dispatch", step_index=0, not_before=later,
            created_at=now() - timedelta(minutes=30) + timedelta(seconds=index),
        ))
    due = Command(
        org_id=batch.org_id, batch_id=batch.id, station_id="ST-05", capability="cap.vacuum_dry",
        params={}, type="dispatch", step_index=0, not_before=now() - timedelta(minutes=1),
    )
    db.add(due)
    db.commit()
    try:
        picked = CommandRepository(db).pending(50)
        assert due.id in {command.id for command in picked}
        assert all(command.not_before is None or command.not_before <= now() for command in picked)
    finally:
        db.query(Command).filter(Command.batch_id == batch.id, Command.state == "sent").update(
            {"state": "cancelled", "delivery_state": "not_sent"}, synchronize_session=False
        )
        db.commit()


def test_closed_gate_only_offers_safety_commands_first(running_batch, db):
    from app.core.clock import now
    from app.models import Batch, Command
    from app.repositories.execution import CommandRepository

    batch = db.get(Batch, running_batch)
    early = now() - timedelta(minutes=10)
    action = Command(
        org_id=batch.org_id, batch_id=batch.id, station_id="ST-05", capability="cap.vacuum_dry",
        params={}, type="dispatch", step_index=0, created_at=early,
    )
    hold = Command(
        org_id=batch.org_id, batch_id=batch.id, station_id="ST-05", capability="cap.vacuum_dry",
        params={}, type="hold", step_index=0, created_at=early + timedelta(minutes=5),
    )
    db.add_all([action, hold])
    db.commit()
    try:
        repository = CommandRepository(db)
        closed = [command.id for command in repository.pending(50, dispatch_open=False)]
        assert hold.id in closed and action.id not in closed
        opened = [command.id for command in repository.pending(50)]
        assert opened.index(hold.id) < opened.index(action.id), "保持 / 终止先于动作指令投递"
    finally:
        db.query(Command).filter(Command.id.in_([action.id, hold.id])).update(
            {"state": "cancelled", "delivery_state": "not_sent"}, synchronize_session=False
        )
        db.commit()


def test_advancer_does_not_claim_other_organizations_events(db):
    """推进器逐组织运行；领到别的组织的事件会因找不到步骤实例被永久判成 rejected。"""
    from app.core.context import system_context
    from app.models import WorkflowEvent
    from app.services.workflow_service import WorkflowService

    foreign = WorkflowEvent(
        org_id=ISOLATED_ORG, batch_id="B-FOREIGN", step_run_id="SR-FOREIGN",
        event_key="foreign:claim-test", event_type="device_ack", payload={},
    )
    db.add(foreign)
    db.commit()
    try:
        WorkflowService(db, system_context(ORG, "后台推进器")).tick()
        db.refresh(foreign)
        assert foreign.state == "pending", foreign.error
    finally:
        db.delete(foreign)
        db.commit()
