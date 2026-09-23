"""执行安全回归：保持 / 终止与在途指令、执行门、正式环境禁用模拟、推进重试。

每条用例对应一个曾经能直接造成重复物理动作、联锁失效或批次卡死的缺陷。
"""
from datetime import timedelta

import pytest

from test_failure_paths import close_gate, running_batch  # noqa: F401  （复用 fixture）
from test_workflow import single_condition_task  # noqa: F401


def _dispatch_ledger(batch_id: str) -> list[tuple[str, int, str]]:
    """设备侧真正收到过的动作指令：(类型, 步骤, 台账状态)。"""
    from app.core.db import SessionLocal
    from app.models import AdapterExecution, Command

    with SessionLocal() as db:
        rows = (
            db.query(Command.type, Command.step_index, AdapterExecution.state)
            .join(AdapterExecution, AdapterExecution.command_id == Command.id)
            .filter(Command.batch_id == batch_id, Command.type.in_(["dispatch", "resume", "retry"]))
            .all()
        )
    return [(row[0], row[1], row[2]) for row in rows]


def _resume(operator, batch_id: str):
    return operator.post(
        f"/api/batches/{batch_id}/recover",
        {"strategy": "resume", "verified": True,
         "signature_id": operator.sign("已核实实际量与设备状态")},
    )


# ---------- 保持撤回排队指令 ----------


def test_hold_withdraws_queued_dispatch_and_resume_dispatches_once(operator, running_batch, executor):
    """保持时首条动作还在队列里：撤回它；续跑是首次下发，设备只动一次。"""
    held = operator.post(f"/api/batches/{running_batch}/hold", {"reason": "核对真空度"})
    assert held.status_code == 200, held.text
    assert held.json()["state"] == "paused"
    assert held.json()["withdrawn"] == 1
    assert held.json()["device_hold_command_id"] == "", "设备没收到动作，不需要设备侧保持"

    assert executor()["executed"] == 0
    commands = operator.get(f"/api/batches/{running_batch}").json()["commands"]
    assert [(c["type"], c["state"], c["delivery_state"]) for c in commands] == [
        ("dispatch", "cancelled", "not_sent")
    ]
    assert _dispatch_ledger(running_batch) == []

    resumed = _resume(operator, running_batch)
    assert resumed.status_code == 200, resumed.text
    commands = operator.get(f"/api/batches/{running_batch}").json()["commands"]
    assert commands[-1]["type"] == "dispatch", "从未送达的动作不能以「继续」发给设备"

    for _ in range(12):
        executor()
    assert operator.get(f"/api/batches/{running_batch}").json()["state"] == "done"
    step_zero = [row for row in _dispatch_ledger(running_batch) if row[1] == 0]
    assert step_zero == [("dispatch", 0, "done")], "第 0 步只能被物理执行一次"


def test_executor_withdraws_action_when_batch_left_running(operator, running_batch, executor):
    """并发兜底：保持请求与执行器领取交错时，执行器按锁内读到的批次状态撤回动作。"""
    from app.core.db import SessionLocal
    from app.models import Batch

    with SessionLocal() as db:
        db.get(Batch, running_batch).state = "paused"
        db.commit()

    assert executor()["executed"] == 1
    command = operator.get(f"/api/batches/{running_batch}").json()["commands"][0]
    assert (command["state"], command["delivery_state"]) == ("cancelled", "not_sent")
    assert _dispatch_ledger(running_batch) == []


# ---------- 执行门与联锁 ----------


def _other_station(batch_id: str) -> str:
    from app.core.db import SessionLocal
    from app.models import Adapter, Command

    with SessionLocal() as db:
        used = {c.station_id for c in db.query(Command).filter(Command.batch_id == batch_id)}
        return next(a.station_id for a in db.query(Adapter).order_by(Adapter.station_id) if a.station_id not in used)


def test_hold_and_abort_work_while_gate_is_closed(operator, running_batch, executor):
    """门关着时现场最需要能停下来：保持与终止不受执行门限制，动作指令留在队列不投递。"""
    close_gate(_other_station(running_batch))
    assert operator.get("/api/gate").json()["open"] is False

    assert executor()["executed"] == 0, "执行门关闭时动作指令不投递"
    queued = operator.get(f"/api/batches/{running_batch}").json()["commands"][0]
    assert (queued["state"], queued["delivery_state"]) == ("sent", "queued")

    held = operator.post(f"/api/batches/{running_batch}/hold", {"reason": "联锁排查"})
    assert held.status_code == 200, held.text

    aborted = operator.post(
        f"/api/batches/{running_batch}/abort",
        {"reason": "联锁未解除，安全终止", "signature_id": operator.sign("安全终止", target=running_batch)},
    )
    assert aborted.status_code == 200, aborted.text
    assert aborted.json()["state"] == "aborted"
    assert _dispatch_ledger(running_batch) == []

    resumed_elsewhere = operator.post("/api/batches", {"plan_id": "EP-205-01"}).json()["id"]
    blocked = operator.post(f"/api/batches/{resumed_elsewhere}/schedule", {})
    assert blocked.status_code == 423, "排程仍受执行门约束"


def test_interlock_counts_even_when_device_refuses_commands(operator, device, reset_runtime):
    """设备心跳报「不接受指令 + 联锁」时，联锁不能因为不接受指令而被忽略。"""
    station_id = "ST-05"
    reported = device.post(
        f"/api/runtime/stations/{station_id}/heartbeat",
        {"connected": True, "site_interlock": True, "accepts_commands": False},
    )
    assert reported.status_code == 200, reported.text
    gate = operator.get("/api/gate").json()
    assert gate["open"] is False
    assert any("公共保护联锁" in reason for reason in gate["reasons"])


# ---------- 正式环境禁用模拟 ----------


def test_production_refuses_simulation_adapters(operator, reset_runtime, monkeypatch):
    from app.adapters import AdapterError, adapter_for
    from app.adapters.registry import reset_cache
    from app.core.config import settings
    from app.core.db import SessionLocal
    from app.models import Adapter

    created = operator.post("/api/batches", {"plan_id": "EP-205-01"}).json()["id"]
    assert operator.post(f"/api/batches/{created}/schedule", {}).status_code == 200

    monkeypatch.setattr(settings, "environment", "production")
    reset_cache()
    try:
        with SessionLocal() as db:
            record = db.get(Adapter, "ST-05")
            assert record.kind == "simulation"
            with pytest.raises(AdapterError, match="禁止模拟执行"):
                adapter_for(record)

        refused = operator.post(
            f"/api/batches/{created}/dispatch",
            {"manual_review": True, "signature_id": operator.sign("批准执行", target=created)},
        )
        assert refused.status_code == 409, refused.text
        assert refused.json()["detail"]["code"] == "simulation_adapter"
        assert settings.simulate_heartbeat is False, "正式环境默认不补模拟心跳"

        monkeypatch.setattr(settings, "executor_simulate_heartbeat", True)
        assert any("SIMULATE_HEARTBEAT" in issue for issue in settings.production_issues())
    finally:
        reset_cache()


# ---------- 推进事件：瞬时错误重试、保存点 ----------


def _submit_manual(operator, run_id: str):
    return operator.post(
        f"/api/step-runs/{run_id}/submit",
        {
            "form_data": {"weighed_g": 20.1, "balance_id": "BAL-01", "double_check": True},
            "checks": {"samples": True, "materials": True},
            "signature_id": operator.sign("人工记录确认", target=run_id),
        },
    )


def test_transient_advance_failure_keeps_record_and_retries(
    operator, single_condition_task, executor, monkeypatch,
):
    """推进时数据库抖动：人工记录与签名不丢，事件退避后重试，不永久卡住。"""
    from app.core.db import SessionLocal
    from app.models import StepRun, WorkflowEvent
    from app.services.workflow_service import WorkflowService

    batch_id = single_condition_task["batch_id"]
    run = operator.get(f"/api/batches/{batch_id}").json()["step_runs"][0]
    original = WorkflowService._apply
    failures = {"left": 1}

    def flaky(self, event, step_run, batch):
        if failures["left"]:
            failures["left"] -= 1
            raise RuntimeError("数据库连接中断")
        return original(self, event, step_run, batch)

    monkeypatch.setattr(WorkflowService, "_apply", flaky)
    submitted = _submit_manual(operator, run["id"])
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["advance"]["processed"] is False
    assert submitted.json()["advance"]["attempts"] == 1

    with SessionLocal() as db:
        stored = db.get(StepRun, run["id"])
        assert stored.form_data["values"]["balance_id"] == "BAL-01", "推进失败不能回滚已提交的人工记录"
        assert stored.submitted_by
        event = db.query(WorkflowEvent).filter(WorkflowEvent.step_run_id == run["id"]).one()
        assert (event.state, event.attempts) == ("pending", 1)
        assert event.available_at > event.created_at, "按退避时间重排，不立即自旋"
        event.available_at = event.created_at - timedelta(seconds=1)
        db.commit()

    executor()
    detail = operator.get(f"/api/batches/{batch_id}").json()
    assert [row["kind"] for row in detail["step_runs"]][:2] == ["manual", "device"]
    assert detail["step_runs"][0]["state"] == "completed"


def test_persistent_advance_failure_rejects_and_raises_alarm(
    operator, single_condition_task, executor, monkeypatch,
):
    from app.core.config import settings
    from app.core.db import SessionLocal
    from app.models import WorkflowEvent
    from app.services.workflow_service import WorkflowService

    batch_id = single_condition_task["batch_id"]
    run = operator.get(f"/api/batches/{batch_id}").json()["step_runs"][0]

    def broken(self, event, step_run, batch):
        raise RuntimeError("连接池耗尽")

    monkeypatch.setattr(WorkflowService, "_apply", broken)
    monkeypatch.setattr(settings, "advance_max_attempts", 2)
    assert _submit_manual(operator, run["id"]).status_code == 200

    with SessionLocal() as db:
        event = db.query(WorkflowEvent).filter(WorkflowEvent.step_run_id == run["id"]).one()
        event.available_at = event.created_at - timedelta(seconds=1)
        db.commit()
    executor()

    with SessionLocal() as db:
        event = db.query(WorkflowEvent).filter(WorkflowEvent.step_run_id == run["id"]).one()
        assert (event.state, event.attempts) == ("rejected", 2)
    alarms = [a for a in operator.get("/api/alarms").json() if a["source_id"] == batch_id]
    assert any("无法推进" in a["message"] for a in alarms), "卡住的批次必须报警"


# ---------- 保持中走完最后一步 ----------


def test_paused_batch_is_not_marked_done_by_final_step(
    operator, qa, single_condition_task, executor,
):
    """保持中审核通过了最后一步：批次仍由操作员控制，恢复评估确认后才结束。"""
    from app.core.clock import now
    from app.core.db import SessionLocal
    from app.models import StepRun

    batch_id = single_condition_task["batch_id"]
    run = operator.get(f"/api/batches/{batch_id}").json()["step_runs"][0]
    assert _submit_manual(operator, run["id"]).status_code == 200
    for _ in range(3):
        executor()
    runs = operator.get(f"/api/batches/{batch_id}").json()["step_runs"]
    wait = next(row for row in runs if row["kind"] == "wait")

    assert operator.post(f"/api/batches/{batch_id}/hold", {"reason": "核对匀浆记录"}).status_code == 200
    with SessionLocal() as db:
        db.get(StepRun, wait["id"]).due_at = now() - timedelta(seconds=1)
        db.commit()
    executor()

    review = next(
        row for row in operator.get(f"/api/batches/{batch_id}").json()["step_runs"]
        if row["kind"] == "review"
    )
    decided = qa.post(
        f"/api/step-runs/{review['id']}/review",
        {"conclusion": "approved", "row_version": review["row_version"],
         "signature_id": qa.sign("流程审核", target=review["id"], object_version=review["row_version"])},
    )
    assert decided.status_code == 200, decided.text
    assert operator.get(f"/api/batches/{batch_id}").json()["state"] == "paused"

    resumed = _resume(operator, batch_id)
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["state"] == "done"
    detail = operator.get(f"/api/batches/{batch_id}").json()
    assert len([row for row in detail["step_runs"] if row["kind"] == "review"]) == 1, (
        "恢复时不能把已完成的最后一步再开一遍"
    )


# ---------- 排程与校准输入 ----------


def test_schedule_accepts_offset_timestamps(operator, reset_runtime):
    from app.core.clock import now

    created = operator.post("/api/batches", {"plan_id": "EP-205-01"}).json()["id"]
    local = (now() + timedelta(hours=2)).replace(microsecond=0)
    offset = f"{(local + timedelta(hours=8)).isoformat()}+08:00"
    scheduled = operator.post(f"/api/batches/{created}/schedule", {"start_from": offset})
    assert scheduled.status_code == 200, scheduled.text
    first = min(a["starts_at"] for a in operator.get(f"/api/batches/{created}").json()["allocations"])
    assert first.startswith(local.isoformat(timespec="minutes")[:13]), "带偏移的时间按 UTC 换算"


def test_pass_calibration_requires_expiry(admin, reset_runtime):
    asset = admin.post("/api/assets", {"asset_no": "AS-EXPIRY-1", "name": "需有效期资产"}).json()
    rejected = admin.post(
        f"/api/assets/{asset['id']}/calibrations",
        {"result": "pass", "certificate_file_id": "FILE-ANY"},
    )
    assert rejected.status_code == 422
    assert rejected.json()["detail"]["code"] == "calibration_expiry_required"


# ---------- 异步真实设备：在途动作上的保持与终止 ----------


@pytest.fixture()
def async_device(running_batch, monkeypatch):
    """把首步工位换成异步真实驱动：动作指令先 accepted，由 `finish` 控制何时完成。"""
    from app.adapters.base import AdapterContract, CommandResult
    from app.adapters.registry import REAL_IMPLEMENTATIONS, reset_cache
    from app.core.clock import now
    from app.core.db import SessionLocal
    from app.models import Adapter, Command

    state = {"finished": set(), "calls": []}

    class SlowDevice:
        def __init__(self, record):
            self.contract = AdapterContract(kind="real", protocol="test-slow", supports_query=True)

        def healthcheck(self):
            return {"reachable": True}

        def _result(self, command_id, value):
            return CommandResult(
                command_id=command_id, state=value, device_ts=now(), quality="good",
                origin="real:test-slow",
            )

        def submit(self, request):
            state["calls"].append((request.type, request.command_id))
            return self._result(request.command_id, "accepted")

        def query(self, command_id):
            return self._result(command_id, "done" if command_id in state["finished"] else "running")

        def hold(self, request):
            state["calls"].append(("hold", request.target_command_id))
            return self._result(request.command_id, "done")

        def abort(self, request):
            state["calls"].append(("abort", request.target_command_id))
            return self._result(request.command_id, "done")

    monkeypatch.setitem(REAL_IMPLEMENTATIONS, "test_slow", SlowDevice)
    with SessionLocal() as db:
        command = db.query(Command).filter(Command.batch_id == running_batch).one()
        adapter = db.get(Adapter, command.station_id)
        original = {key: getattr(adapter, key) for key in ("kind", "driver", "protocol", "config_version")}
        adapter.kind, adapter.driver, adapter.protocol = "real", "test_slow", "test-slow"
        adapter.config_version += 1
        station_id = adapter.station_id
        db.commit()
    reset_cache()
    try:
        yield state
    finally:
        with SessionLocal() as db:
            adapter = db.get(Adapter, station_id)
            for key, value in original.items():
                setattr(adapter, key, value)
            adapter.current_command_id = ""
            db.commit()
        reset_cache()


def test_hold_on_in_flight_device_action_is_not_faulted_by_reconcile(
    operator, running_batch, async_device, executor,
):
    """保持指令不能覆盖工位的「当前指令」：否则下一轮对账会把仍在执行的动作判成不一致。"""
    executor()
    dispatch = operator.get(f"/api/batches/{running_batch}").json()["commands"][0]
    assert dispatch["state"] == "running"

    held = operator.post(f"/api/batches/{running_batch}/hold", {"reason": "核对读数"})
    assert held.status_code == 200, held.text
    assert held.json()["device_hold_command_id"], "设备在动作，必须发保持指令"

    executor()
    executor()
    detail = operator.get(f"/api/batches/{running_batch}").json()
    assert detail["state"] == "paused", detail["failure_reason"]
    assert ("hold", dispatch["id"]) in async_device["calls"], "保持指令要指明针对的在途动作"
    assert async_device["calls"].count(("dispatch", dispatch["id"])) == 1


def test_abort_on_in_flight_action_waits_for_device_confirmation(
    operator, running_batch, async_device, executor,
):
    executor()
    dispatch_id = operator.get(f"/api/batches/{running_batch}").json()["commands"][0]["id"]

    aborted = operator.post(
        f"/api/batches/{running_batch}/abort",
        {"reason": "异常终止", "signature_id": operator.sign("安全终止", target=running_batch)},
    )
    assert aborted.status_code == 200 and aborted.json()["state"] == "aborting"

    executor()
    detail = operator.get(f"/api/batches/{running_batch}").json()
    assert detail["state"] == "aborted"
    states = {c["id"]: c["state"] for c in detail["commands"]}
    assert states[dispatch_id] == "cancelled", "设备确认终止后，在途动作随之结束"
    assert ("abort", dispatch_id) in async_device["calls"]
    assert any(event["action"] == "设备确认终止" for event in detail["audit"])
