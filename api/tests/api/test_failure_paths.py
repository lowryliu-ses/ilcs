"""一期验收用例：设备失联、重复指令、物料不足、硬时限无法满足，以及保持 / 恢复 / 终止。"""
import pytest


def close_gate(station_id: str = "ST-05", *, interlock: bool = True) -> None:
    from app.core.db import SessionLocal
    from app.models import Adapter

    with SessionLocal() as db:
        adapter = db.get(Adapter, station_id)
        adapter.site_interlock = interlock
        db.commit()


@pytest.fixture()
def running_batch(operator, reset_runtime):
    created = operator.post("/api/batches", {"plan_id": "EP-205-01"})
    assert created.status_code == 201, created.text
    batch_id = created.json()["id"]
    scheduled = operator.post(f"/api/batches/{batch_id}/schedule", {})
    assert scheduled.status_code == 200, scheduled.text
    dispatched = operator.post(
        f"/api/batches/{batch_id}/dispatch",
        {"manual_review": True, "signature_id": operator.sign("批准执行", target=batch_id)},
    )
    assert dispatched.status_code == 200, dispatched.text
    return batch_id


def test_site_interlock_closes_gate_and_blocks_control(operator, reset_runtime):
    created = operator.post("/api/batches", {"plan_id": "EP-205-01"})
    batch_id = created.json()["id"]
    close_gate()

    gate = operator.get("/api/gate").json()
    assert not gate["open"] and "公共保护联锁" in gate["reasons"][0]

    blocked = operator.post(f"/api/batches/{batch_id}/schedule", {})
    assert blocked.status_code == 423

    close_gate(interlock=False)
    assert operator.get("/api/gate").json()["open"]
    assert operator.post(f"/api/batches/{batch_id}/schedule", {}).status_code == 200


def test_material_shortage_blocks_creation_with_reason(operator, reset_runtime):
    from app.core.db import SessionLocal
    from app.models import Lot

    with SessionLocal() as db:
        lot = db.get(Lot, "LOT-ELY-0611")
        original = lot.qty
        lot.qty = "0.0001"
        db.commit()

    try:
        rejected = operator.post("/api/batches", {"plan_id": "EP-205-01"})
        assert rejected.status_code == 409
        assert "电解液 LP57 可用量不足" in rejected.json()["detail"]["message"]
    finally:
        with SessionLocal() as db:
            db.get(Lot, "LOT-ELY-0611").qty = original
            db.commit()


def test_faulted_station_blocks_scheduling_with_step_reason(operator, reset_runtime):
    from app.core.db import SessionLocal
    from app.models import Station

    created = operator.post("/api/batches", {"plan_id": "EP-205-01"})
    batch_id = created.json()["id"]
    with SessionLocal() as db:
        db.get(Station, "ST-06").status = "fault"
        db.commit()

    rejected = operator.post(f"/api/batches/{batch_id}/schedule", {})
    assert rejected.status_code == 409
    assert "无可执行工位" in rejected.json()["detail"]["message"]
    assert rejected.json()["detail"]["step_index"] == 2

    queue = {row["batch_id"]: row for row in operator.get("/api/schedule/queue").json()}
    assert queue[batch_id]["schedulable"] is False
    assert "ST-06 故障" in queue[batch_id]["blocker"]


def test_hold_then_resume_shifts_downstream(operator, running_batch, executor):
    held = operator.post(f"/api/batches/{running_batch}/hold", {"reason": "核对真空度读数"})
    assert held.status_code == 200 and held.json()["state"] == "paused"

    evaluation = operator.get(f"/api/batches/{running_batch}/recovery-options").json()
    assert [row["key"] for row in evaluation["preconditions"]] == [
        "cause_cleared", "checkpoint", "hold_window", "downstream"
    ]
    options = {o["id"]: o for o in evaluation["options"]}
    assert options["resume"]["allowed"] and options["retry"]["allowed"]

    unverified = operator.post(
        f"/api/batches/{running_batch}/recover",
        {"strategy": "resume", "verified": False, "signature_id": operator.sign("已核实实际量与设备状态")},
    )
    assert unverified.status_code == 409

    resumed = operator.post(
        f"/api/batches/{running_batch}/recover",
        {"strategy": "resume", "verified": True, "signature_id": operator.sign("已核实实际量与设备状态")},
    )
    assert resumed.status_code == 200 and resumed.json()["state"] == "running"

    for _ in range(12):
        executor()
    assert operator.get(f"/api/batches/{running_batch}").json()["state"] == "done"


def test_adapter_loss_faults_batch_and_raises_alarm(operator, device, running_batch, executor):
    from app.core.db import SessionLocal
    from app.models import Adapter

    with SessionLocal() as db:
        db.get(Adapter, "ST-05").connected = False
        db.commit()

    executor()
    executor()

    detail = operator.get(f"/api/batches/{running_batch}").json()
    assert detail["state"] == "fault"
    assert "不自动重试" in detail["failure_reason"]
    assert any(command["state"] == "unknown" for command in detail["commands"])

    alarm = next(a for a in operator.get("/api/alarms").json() if a["source_id"] == running_batch)
    assert alarm["state"] == "active" and alarm["condition_active"]

    # 触发原因未解除时，续跑与重试都不可用
    options = {o["id"]: o for o in operator.get(f"/api/batches/{running_batch}/recovery-options").json()["options"]}
    assert not options["resume"]["allowed"] and options["abort"]["allowed"]

    # 确认报警不解除阻断；只有设备侧条件恢复才行
    assert operator.post(f"/api/alarms/{alarm['id']}/ack").status_code == 200
    still_blocked = operator.get(f"/api/batches/{running_batch}/recovery-options").json()
    assert not still_blocked["preconditions"][0]["ok"]

    assert operator.post(f"/api/alarms/{alarm['id']}/close").status_code == 409, "条件未恢复不能关闭"
    # 条件恢复是设备侧事件，走服务认证，不是人工按钮
    assert operator.client.post(f"/api/alarms/{alarm['id']}/condition-cleared").status_code == 401
    assert device.post(f"/api/alarms/{alarm['id']}/condition-cleared").status_code == 200

    with SessionLocal() as db:
        db.get(Adapter, "ST-05").connected = True
        db.commit()

    cleared = operator.get(f"/api/batches/{running_batch}/recovery-options").json()
    assert cleared["preconditions"][0]["ok"]
    assert operator.post(f"/api/alarms/{alarm['id']}/close").status_code == 200


def test_restart_reconciliation_holds_mismatched_command(operator, running_batch):
    from app.core.db import SessionLocal
    from app.models import Adapter, Command
    from app.services.execution_service import ExecutorLoop

    with SessionLocal() as db:
        command = db.query(Command).filter(Command.batch_id == running_batch).first()
        command.state = "running"
        command.delivery_state = "delivered"
        db.get(Adapter, command.station_id).current_command_id = "cmd-来自另一次运行"
        db.commit()

    with SessionLocal() as db:
        assert ExecutorLoop(db).reconcile() == 1
        db.commit()

    detail = operator.get(f"/api/batches/{running_batch}").json()
    assert detail["state"] == "fault"
    assert "重启对账不一致" in detail["failure_reason"]


def test_real_async_adapter_is_polled_without_faking_telemetry(
    operator, running_batch, executor, monkeypatch,
):
    """真实长任务先 accepted、下一轮 query 完成；真实设备无数据时绝不合成曲线。"""
    from app.adapters.base import AdapterContract, CommandResult
    from app.adapters.registry import REAL_IMPLEMENTATIONS, reset_cache
    from app.core.clock import now
    from app.core.db import SessionLocal
    from app.models import Adapter, Command, Telemetry

    class AsyncRealAdapter:
        query_count = 0

        def __init__(self, record):
            self.contract = AdapterContract(
                kind="real", protocol="test-async", version="1",
                supports_query=True, supports_dedup=True,
            )

        def healthcheck(self):
            return {"reachable": True}

        def submit(self, request):
            return CommandResult(
                command_id=request.command_id, state="accepted", device_ts=now(),
                quality="good", origin="real:test-async",
            )

        def query(self, command_id):
            type(self).query_count += 1
            return CommandResult(
                command_id=command_id, state="done", device_ts=now(), quality="good",
                telemetry=(("actual_speed", 119.8, 120.0),), origin="real:test-async",
            )

        def hold(self, request):
            return self.submit(request)

        def abort(self, request):
            return self.submit(request)

    monkeypatch.setitem(REAL_IMPLEMENTATIONS, "test_async", AsyncRealAdapter)
    with SessionLocal() as db:
        command = db.query(Command).filter(
            Command.batch_id == running_batch, Command.state == "sent"
        ).first()
        adapter = db.get(Adapter, command.station_id)
        original = {
            "kind": adapter.kind, "driver": adapter.driver, "protocol": adapter.protocol,
            "config_version": adapter.config_version,
        }
        adapter.kind = "real"
        adapter.driver = "test_async"
        adapter.protocol = "test-async"
        adapter.supports_query = True
        adapter.supports_dedup = True
        adapter.config_version += 1
        station_id = adapter.station_id
        command_id = command.id
        db.commit()
    reset_cache()

    try:
        first = executor()
        assert first["executed"] == 1 and first["polled"] == 0
        with SessionLocal() as db:
            command = db.get(Command, command_id)
            adapter = db.get(Adapter, station_id)
            assert command.state == "running"
            assert command.delivery_state == "delivered"
            assert adapter.current_command_id == command_id
            assert db.query(Telemetry).filter(Telemetry.batch_id == running_batch).count() == 0

        second = executor()
        assert second["polled"] == 1
        with SessionLocal() as db:
            command = db.get(Command, command_id)
            points = db.query(Telemetry).filter(
                Telemetry.batch_id == running_batch,
                Telemetry.origin == "real:test-async",
            ).all()
            assert command.state == "done"
            assert AsyncRealAdapter.query_count == 1
            assert [(point.metric, point.value, point.setpoint) for point in points] == [
                ("actual_speed", 119.8, 120.0)
            ]
    finally:
        with SessionLocal() as db:
            adapter = db.get(Adapter, station_id)
            for key, value in original.items():
                setattr(adapter, key, value)
            adapter.current_command_id = ""
            db.commit()
        reset_cache()


def test_abort_releases_reservations_and_marks_samples(operator, running_batch, executor):
    # 首条动作指令还在队列里：设备侧从未收到任何动作，终止不需要等设备确认
    aborted = operator.post(
        f"/api/batches/{running_batch}/abort",
        {"reason": "浆料异常，安全终止", "signature_id": operator.sign("安全终止", target=running_batch)},
    )
    assert aborted.status_code == 200 and aborted.json()["state"] == "aborted"

    assert executor()["executed"] == 0

    detail = operator.get(f"/api/batches/{running_batch}").json()
    assert detail["state"] == "aborted"
    assert [(c["type"], c["state"]) for c in detail["commands"]] == [("dispatch", "cancelled")]
    assert all(sample["state"] == "failed" for sample in detail["samples"])
    assert all(r["state"] == "released" for r in detail["reservations"])
    assert any(event["action"] == "终止批次" and event["sign"] for event in detail["audit"])


def test_abort_before_dispatch_terminates_without_device_confirmation(operator, reset_runtime):
    """未下发的批次没有设备动作可确认，终止必须立即完成并归还工位时间窗。"""
    created = operator.post("/api/batches", {"plan_id": "EP-205-01"})
    batch_id = created.json()["id"]
    assert operator.post(f"/api/batches/{batch_id}/schedule", {}).status_code == 200

    aborted = operator.post(
        f"/api/batches/{batch_id}/abort",
        {"reason": "计划取消", "signature_id": operator.sign("安全终止", target=batch_id)},
    )
    assert aborted.status_code == 200 and aborted.json()["state"] == "aborted"

    detail = operator.get(f"/api/batches/{batch_id}").json()
    assert detail["allocations"] == [], "时间窗未释放，后续批次会撞上幽灵占用"
    assert all(r["state"] == "released" for r in detail["reservations"])
    assert not detail["commands"], "从未下发，不应产生设备指令"

    # 时间窗已归还，同一条计划可以立刻重排一个新批次
    replacement = operator.post("/api/batches", {"plan_id": "EP-205-01"})
    assert replacement.status_code == 201
    assert operator.post(f"/api/batches/{replacement.json()['id']}/schedule", {}).status_code == 200


def test_command_without_adapter_is_surfaced_not_silently_queued(operator, running_batch, executor):
    """指令落到没有适配器的工位时必须挂起报警，而不是在队列里静默堆积。"""
    from app.core.db import SessionLocal
    from app.models import Command, Station

    with SessionLocal() as db:
        # “没有适配器”应是一个合法工位缺少 Adapter 登记，而不是让 Command 指向
        # 不存在的 Station；后者在 PostgreSQL 上会被外键正确拒绝。
        station_id = "ST-NO-ADAPTER"
        if not db.get(Station, station_id):
            db.add(
                Station(
                    id=station_id, org_id="ORG-001", name="无适配器测试工位",
                    status="idle", positions=1, clean=True, limits={},
                )
            )
            db.flush()
        command = db.query(Command).filter(Command.batch_id == running_batch).first()
        command.state = "sent"
        command.station_id = station_id
        db.commit()

    executor()
    executor()

    detail = operator.get(f"/api/batches/{running_batch}").json()
    assert detail["state"] == "fault"
    assert "没有可用适配器" in detail["failure_reason"]


def test_unpausable_capability_refuses_hold(operator, reset_runtime):
    """注液封口不可中断：能力规则直接拒绝保持请求。"""
    from app.core.db import SessionLocal
    from app.models import Batch

    created = operator.post("/api/batches", {"plan_id": "EP-205-01"})
    batch_id = created.json()["id"]
    operator.post(f"/api/batches/{batch_id}/schedule", {})
    operator.post(
        f"/api/batches/{batch_id}/dispatch",
        {"manual_review": True, "signature_id": operator.sign("批准执行", target=batch_id)},
    )
    with SessionLocal() as db:
        from app.models import StepRun

        batch = db.get(Batch, batch_id)
        batch.current_step = 2  # 注液封口组装
        # 步骤实例跟着走：保持判据看的是当前在跑的那一步
        run = (
            db.query(StepRun)
            .filter(StepRun.batch_id == batch_id)
            .order_by(StepRun.step_index.desc())
            .first()
        )
        run.step_index = 2
        run.step_id = "s03"
        run.kind = "device"
        run.state = "running"
        run.step_snapshot = batch.recipe_snapshot["steps"][2]
        db.commit()

    rejected = operator.post(f"/api/batches/{batch_id}/hold", {"reason": "手套箱水含量复核"})

    assert rejected.status_code == 409
    assert "不可中断" in rejected.json()["detail"]["message"]
