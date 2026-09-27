"""核心链路设计评审（2026-09-26）回归。

每条用例对应评审里的一个缺口（R1–R9）或补充问题，先在未修复的代码上确认会失败，修复后通过：
- R1 终止 / 保持逐设备下发、逐设备确认；可暂停判定看真正在设备上的那一步；保持后能续跑
- R2 执行器按实际占用投递：多通道不超发，结果未知、超时、已保持的动作保留占用
- R3 手动重排不动依赖图上已经开出的步骤
- R4 批次按任务锁定的方案版本构造；取消的任务、别的方案的任务不能建批次
- R5 子任务继承父任务的上游依赖
- R6 工位通道数不能超过所属资产容量（排程计数在 tests/domain/test_scheduling.py）
- R7 计划完成时间包含尾部工艺等待
- R8 需要清洗的设备：清洗确认前不给别的批次用
- R9 重排建议整组替换
- 单一「当前步骤」假设：计划开始、保持中工位、硬时限倒计时按依赖图算
"""
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm.attributes import flag_modified

from test_failure_paths import running_batch  # noqa: F401  （复用 fixture）

CAPACITY = "METRIC-discharge_capacity-v1"


# ---------- 工具 ----------


def _session():
    from app.core.db import SessionLocal

    return SessionLocal()


def _new_batch(operator, **extra) -> str:
    created = operator.post("/api/batches", {"plan_id": "EP-205-01", **extra})
    assert created.status_code == 201, created.text
    return created.json()["id"]


def _reshape(db, batch_id: str, make_steps) -> None:
    """改写批次快照里的步骤：用来构造依赖图（并行分支）场景。"""
    from app.models import Batch

    batch = db.get(Batch, batch_id)
    snapshot = dict(batch.recipe_snapshot)
    snapshot["steps"] = make_steps([dict(step) for step in snapshot["steps"]])
    batch.recipe_snapshot = snapshot
    flag_modified(batch, "recipe_snapshot")
    db.commit()


def _dispatch(operator, batch_id: str) -> None:
    scheduled = operator.post(f"/api/batches/{batch_id}/schedule", {})
    assert scheduled.status_code == 200, scheduled.text
    dispatched = operator.post(
        f"/api/batches/{batch_id}/dispatch",
        {"manual_review": True, "signature_id": operator.sign("批准执行", target=batch_id)},
    )
    assert dispatched.status_code == 200, dispatched.text


def _detail(operator, batch_id: str) -> dict:
    return operator.get(f"/api/batches/{batch_id}").json()


def _commands(operator, batch_id: str, *types: str) -> list[dict]:
    return [row for row in _detail(operator, batch_id)["commands"] if not types or row["type"] in types]


def _abort(operator, batch_id: str):
    return operator.post(
        f"/api/batches/{batch_id}/abort",
        {"reason": "安全终止", "signature_id": operator.sign("安全终止", target=batch_id)},
    )


def _resume(operator, batch_id: str):
    return operator.post(
        f"/api/batches/{batch_id}/recover",
        {"strategy": "resume", "verified": True, "signature_id": operator.sign("已核实实际量与设备状态")},
    )


def _verify(operator, command_id: str, conclusion: str):
    return operator.post(
        f"/api/commands/{command_id}/verify",
        {"conclusion": conclusion, "note": "现场查看设备面板",
         "signature_id": operator.sign("已到现场核实设备实态", target=command_id)},
    )


def _without_hard(step: dict) -> dict:
    return {key: value for key, value in step.items() if key != "hard"}


def _parallel_roots(steps):
    """干燥（ST-05）与组装（ST-06）都是起点：下发后两台设备同时动作。"""
    dry, weigh, assemble, test = steps
    return [
        {**dry, "after": []},
        {**weigh, "after": [dry["step_id"]]},
        {**_without_hard(assemble), "after": []},
        {**test, "after": [weigh["step_id"], assemble["step_id"]]},
    ]


def _pausable_roots(steps):
    """干燥（ST-05）与充放电（ST-07）都是起点，两个能力都允许保持。"""
    dry, weigh, assemble, test = steps
    return [
        {**dry, "after": []},
        {**weigh, "after": [dry["step_id"]]},
        {**_without_hard(assemble), "after": [weigh["step_id"]]},
        {**test, "after": []},
    ]


def _wait_beside_dry(minutes: float):
    """静置（不占工位）与干燥并行作为起点：批次的「当前步骤」是静置，设备上跑的是干燥。"""

    def make(steps):
        dry, weigh, assemble, test = steps
        wait = {"step_id": "w0", "name": "静置", "kind": "wait", "dur": minutes, "after": [],
                "wait_for": {"mode": "duration"}}
        return [
            wait,
            {**dry, "after": []},
            {**weigh, "after": [dry["step_id"]]},
            {**_without_hard(assemble), "after": [weigh["step_id"], "w0"]},
            {**test, "after": [assemble["step_id"]]},
        ]

    return make


def _foreign_command(operator, station_id: str, capability: str) -> tuple[str, str]:
    """另一个运行中批次在该工位上已到点、排队中的动作指令。返回 (批次, 指令)。"""
    from app.models import Batch, Command

    batch_id = _new_batch(operator)
    with _session() as db:
        batch = db.get(Batch, batch_id)
        batch.state = "running"
        command = Command(
            org_id=batch.org_id, batch_id=batch_id, station_id=station_id, capability=capability,
            params={}, type="dispatch", state="sent", delivery_state="queued", step_index=0,
        )
        db.add(command)
        db.commit()
        return batch_id, command.id


def _command(db, command_id: str):
    from app.models import Command

    db.expire_all()
    return db.get(Command, command_id)


@pytest.fixture()
def scripted(monkeypatch, reset_runtime):
    """把指定工位换成可编排的异步真实驱动。

    动作指令先回 accepted，`finished` 里有它之后查询才回 done；`fail[(工位, 动作)] = "unreachable"`
    让该工位的这类调用网络超时（结果未知）。
    """
    from app.adapters.base import AdapterContract, AdapterUnreachable, CommandResult
    from app.adapters.registry import REAL_IMPLEMENTATIONS, reset_cache
    from app.core.clock import now
    from app.models import Adapter

    state: dict = {"finished": set(), "calls": [], "fail": {}}
    swapped: dict[str, dict] = {}

    class ScriptedDevice:
        def __init__(self, record):
            self.station_id = record.station_id
            self.contract = AdapterContract(
                kind="real", protocol="test-scripted", supports_query=True, supports_hold=True,
                supports_abort=True,
            )

        def healthcheck(self):
            return {"reachable": True}

        def _result(self, command_id, value):
            return CommandResult(
                command_id=command_id, state=value, device_ts=now(), quality="good",
                origin="real:test-scripted",
            )

        def _maybe_fail(self, action):
            if state["fail"].get((self.station_id, action)) == "unreachable":
                raise AdapterUnreachable(f"{self.station_id} {action} 网络超时（模拟）")

        def submit(self, request):
            state["calls"].append((self.station_id, request.type, request.command_id))
            self._maybe_fail("submit")
            return self._result(request.command_id, "accepted")

        def query(self, command_id):
            return self._result(command_id, "done" if command_id in state["finished"] else "running")

        def hold(self, request):
            state["calls"].append((self.station_id, "hold", request.target_command_id))
            self._maybe_fail("hold")
            return self._result(request.command_id, "done")

        def abort(self, request):
            state["calls"].append((self.station_id, "abort", request.target_command_id))
            self._maybe_fail("abort")
            return self._result(request.command_id, "done")

    monkeypatch.setitem(REAL_IMPLEMENTATIONS, "test_scripted", ScriptedDevice)

    def use(*station_ids: str) -> dict:
        with _session() as db:
            for station_id in station_ids:
                adapter = db.get(Adapter, station_id)
                swapped.setdefault(
                    station_id,
                    {key: getattr(adapter, key) for key in ("kind", "driver", "protocol", "config_version")},
                )
                adapter.kind, adapter.driver, adapter.protocol = "real", "test_scripted", "test-scripted"
                adapter.config_version += 1
            db.commit()
        reset_cache()
        return state

    state["use"] = use
    yield state
    with _session() as db:
        for station_id, original in swapped.items():
            adapter = db.get(Adapter, station_id)
            for key, value in original.items():
                setattr(adapter, key, value)
            adapter.current_command_id = ""
        db.commit()
    reset_cache()


# ---------- R1 终止与保持 ----------


def test_abort_stops_every_acting_device_and_waits_for_each_confirmation(operator, scripted, db, executor):
    """R1：两台设备并行动作，A 确认终止而 B 超时：整批不能显示已终止，B 的占用不能释放。"""
    from app.models import Adapter

    devices = scripted["use"]("ST-05", "ST-06")
    batch_id = _new_batch(operator)
    _reshape(db, batch_id, _parallel_roots)
    _dispatch(operator, batch_id)
    executor()
    acting = {row["station_id"]: row for row in _commands(operator, batch_id, "dispatch")}
    assert {sid: row["state"] for sid, row in acting.items()} == {"ST-05": "running", "ST-06": "running"}

    devices["fail"][("ST-06", "abort")] = "unreachable"
    aborted = _abort(operator, batch_id)
    assert aborted.status_code == 200, aborted.text
    assert aborted.json()["state"] == "aborting"
    stops = {row["station_id"]: row for row in _commands(operator, batch_id, "abort")}
    assert sorted(stops) == ["ST-05", "ST-06"], "每台可能在动作的设备都要收到终止"

    executor()
    detail = _detail(operator, batch_id)
    assert detail["state"] != "aborted", "ST-06 没有确认停止，整批不能显示已终止"
    states = {row["id"]: row["state"] for row in detail["commands"]}
    assert states[acting["ST-05"]["id"]] == "cancelled", "ST-05 确认终止，它自己的动作随之结束"
    assert states[acting["ST-06"]["id"]] == "running", "ST-06 没有停止确认，动作不能被改成已取消"
    assert ("ST-06", "abort", acting["ST-06"]["id"]) in devices["calls"], "终止要指明针对的在途动作"
    db.expire_all()
    assert db.get(Adapter, "ST-06").current_command_id == acting["ST-06"]["id"], "B 的占用不能释放"

    # 其他批次不能占用还没确认停止的 ST-06
    _, other = _foreign_command(operator, "ST-06", "cap.assemble")
    executor()
    assert _command(db, other).state == "sent", "ST-06 可能仍在动作，新任务不能投给它"

    # 现场确认 ST-06 已停机：这台设备的动作结束，整批才终止，ST-06 才空出来
    verified = _verify(operator, stops["ST-06"]["id"], "executed")
    assert verified.status_code == 200, verified.text
    detail = _detail(operator, batch_id)
    assert detail["state"] == "aborted"
    assert {row["id"]: row["state"] for row in detail["commands"]}[acting["ST-06"]["id"]] == "cancelled"
    executor()
    assert _command(db, other).state == "running", "确认停止后工位释放，排队的指令照常投递"


def test_hold_judges_pausability_by_the_step_on_the_device_and_resumes_it(operator, scripted, db, executor):
    """R1：当前步骤是静置、设备上跑的是可保持的干燥：保持要放行，并且保持后能续跑到完成。"""
    devices = scripted["use"]("ST-05")
    batch_id = _new_batch(operator)
    _reshape(db, batch_id, _wait_beside_dry(5))
    _dispatch(operator, batch_id)
    executor()
    detail = _detail(operator, batch_id)
    assert detail["current_step"] == 0, "并行时当前步骤取最靠前的开放步骤：静置"
    dry = next(row for row in detail["commands"] if row["type"] == "dispatch")
    assert dry["state"] == "running"

    held = operator.post(f"/api/batches/{batch_id}/hold", {"reason": "核对真空度"})
    assert held.status_code == 200, held.text
    assert [(row["station_id"], row["state"]) for row in _commands(operator, batch_id, "hold")] == [
        ("ST-05", "sent")
    ]
    early = _resume(operator, batch_id)
    assert early.status_code == 409, "设备保持还没确认，不能续跑"

    executor()
    assert ("ST-05", "hold", dry["id"]) in devices["calls"]
    assert _command(db, dry["id"]).state == "held", "保持回执确认后，被保持的动作记为已保持"

    resumed = _resume(operator, batch_id)
    assert resumed.status_code == 200, resumed.text
    executor()
    resume = next(row for row in _commands(operator, batch_id) if row["type"] == "resume")
    assert resume["state"] == "running", "续跑不能被自己保持着的那个动作挡住"
    assert _command(db, dry["id"]).state == "superseded", "原动作由续跑指令接续，不再单独完成一次"

    devices["finished"].update({dry["id"], resume["id"]})
    executor()
    runs = [row for row in _detail(operator, batch_id)["step_runs"] if row["step_index"] == 1]
    assert [row["state"] for row in runs] == ["completed"]
    from app.models import Checkpoint

    assert db.query(Checkpoint).filter(Checkpoint.batch_id == batch_id, Checkpoint.step_index == 1).count() == 1, \
        "同一个动作只写一次检查点"


def test_hold_is_refused_when_any_acting_device_cannot_pause(operator, scripted, db, executor):
    """R1：当前步骤（干燥）可保持，但并行在跑的注液封口不可中断：保持必须整体拒绝。"""
    scripted["use"]("ST-05", "ST-06")
    batch_id = _new_batch(operator)
    _reshape(db, batch_id, _parallel_roots)
    _dispatch(operator, batch_id)
    executor()
    assert _detail(operator, batch_id)["current_step"] == 0
    assert {row["state"] for row in _commands(operator, batch_id, "dispatch")} == {"running"}

    refused = operator.post(f"/api/batches/{batch_id}/hold", {"reason": "核对读数"})
    assert refused.status_code == 409, refused.text
    assert "注液封口" in refused.json()["detail"]["message"]
    assert not _commands(operator, batch_id, "hold"), "不能只保持其中一台而让不可中断的步骤被打断"


def test_hold_reaches_every_acting_device_and_resume_continues_all(operator, scripted, db, executor):
    """R1：两台设备并行动作且都可保持：保持逐台下发、逐台确认；续跑把两台都接续上。"""
    from app.services.schedule_service import ScheduleService
    from app.core.context import system_context

    devices = scripted["use"]("ST-05", "ST-07")
    batch_id = _new_batch(operator)
    _reshape(db, batch_id, _pausable_roots)
    _dispatch(operator, batch_id)
    executor()
    acting = {row["station_id"]: row["id"] for row in _commands(operator, batch_id, "dispatch")}
    assert sorted(acting) == ["ST-05", "ST-07"]

    held = operator.post(f"/api/batches/{batch_id}/hold", {"reason": "现场排查"})
    assert held.status_code == 200, held.text
    assert sorted(row["station_id"] for row in _commands(operator, batch_id, "hold")) == ["ST-05", "ST-07"]
    executor()
    assert {station: _command(db, cid).state for station, cid in acting.items()} == {
        "ST-05": "held", "ST-07": "held",
    }
    held_stations = ScheduleService(db, system_context("ORG-001")).held_station_ids()
    assert {"ST-05", "ST-07"} <= held_stations, "保持中的每台设备都不能被新排程当成空闲"

    assert _resume(operator, batch_id).status_code == 200
    executor()
    resumes = {row["station_id"]: row for row in _commands(operator, batch_id, "resume")}
    assert sorted(resumes) == ["ST-05", "ST-07"], "被保持的每个动作都要续跑，不能只续跑当前步骤"
    assert {row["state"] for row in resumes.values()} == {"running"}


def test_abort_whose_target_already_finished_is_confirmed_without_calling_the_device(
    operator, scripted, running_batch, db, executor,
):
    """R1：终止发出后、投递前目标动作刚好完成：设备上已没有要停的动作，不再调用设备，批次照常终止。"""
    devices = scripted["use"]("ST-05")
    executor()
    dry = _commands(operator, running_batch, "dispatch")[0]
    assert dry["state"] == "running"
    aborted = _abort(operator, running_batch)
    assert aborted.status_code == 200 and aborted.json()["state"] == "aborting"

    devices["finished"].add(dry["id"])
    executor()
    detail = _detail(operator, running_batch)
    assert detail["state"] == "aborted"
    assert not [call for call in devices["calls"] if call[1] == "abort"], "目标已结束，不再给设备发终止"
    stop = next(row for row in detail["commands"] if row["type"] == "abort")
    assert (stop["state"], stop["delivery_state"]) == ("done", "not_sent")


def test_hold_confirmed_by_site_check_then_resumes(operator, scripted, running_batch, db, executor):
    """R1：保持指令网络超时（结果未知）时不能续跑；现场确认设备已停在保持后，续跑接续被保持的动作。"""
    devices = scripted["use"]("ST-05")
    executor()
    dry = _commands(operator, running_batch, "dispatch")[0]
    devices["fail"][("ST-05", "hold")] = "unreachable"
    assert operator.post(f"/api/batches/{running_batch}/hold", {"reason": "核对读数"}).status_code == 200
    executor()
    hold = _commands(operator, running_batch, "hold")[0]
    assert (hold["state"], hold["delivery_state"]) == ("unknown", "maybe_sent")
    assert _command(db, dry["id"]).state == "running", "保持没有确认，不能把动作记成已保持"
    assert _resume(operator, running_batch).status_code == 409

    devices["fail"].clear()
    assert _verify(operator, hold["id"], "executed").status_code == 200
    assert _command(db, dry["id"]).state == "held"
    resumed = _resume(operator, running_batch)
    assert resumed.status_code == 200, resumed.text
    executor()
    resume = next(row for row in _commands(operator, running_batch) if row["type"] == "resume")
    assert resume["state"] == "running"
    assert _command(db, dry["id"]).state == "superseded"


def test_retry_after_hold_restarts_the_step_in_place_of_the_held_action(
    operator, scripted, running_batch, db, executor,
):
    """R1：保持后选择重试：旧步骤实例作废，重试指令接续设备上被保持的动作，不被它挡住。"""
    scripted["use"]("ST-05")
    executor()
    dry = _commands(operator, running_batch, "dispatch")[0]
    assert operator.post(f"/api/batches/{running_batch}/hold", {"reason": "重新干燥"}).status_code == 200
    executor()
    assert _command(db, dry["id"]).state == "held"

    retried = operator.post(
        f"/api/batches/{running_batch}/recover",
        {"strategy": "retry", "verified": True, "signature_id": operator.sign("已核实实际量与设备状态")},
    )
    assert retried.status_code == 200, retried.text
    executor()
    retry = next(row for row in _commands(operator, running_batch) if row["type"] == "retry")
    assert retry["state"] == "running"
    assert _command(db, dry["id"]).state == "superseded"
    runs = [row["state"] for row in _detail(operator, running_batch)["step_runs"] if row["step_index"] == 0]
    assert runs == ["superseded", "running"], "旧实例作废留痕，新实例在跑"


# ---------- R2 执行器按实际占用投递 ----------


def test_unknown_action_keeps_the_station_for_other_batches(operator, scripted, running_batch, db, executor):
    """R2：A 的动作结果未知（可能已执行）且设备心跳正常：B 不能抢占这台设备。"""
    devices = scripted["use"]("ST-05")
    devices["fail"][("ST-05", "submit")] = "unreachable"
    executor()
    first = _commands(operator, running_batch, "dispatch")[0]
    assert (first["state"], first["delivery_state"]) == ("unknown", "maybe_sent")

    devices["fail"].clear()
    _, other = _foreign_command(operator, "ST-05", "cap.vacuum_dry")
    executor()
    assert _command(db, other).state == "sent", "结果未知的动作保留占用，核查前不投递新动作"

    assert _verify(operator, first["id"], "not_executed").status_code == 200
    executor()
    assert _command(db, other).state == "running", "现场确认未执行后占用释放"


def test_timed_out_action_keeps_the_station(operator, scripted, running_batch, db, executor):
    """R2：动作超过硬上限转结果未知，设备可能卡在动作中：占用不因超时而释放。"""
    from app.models import Command

    scripted["use"]("ST-05")
    executor()
    first = _commands(operator, running_batch, "dispatch")[0]
    assert first["state"] == "running"
    with _session() as session:
        session.get(Command, first["id"]).started_at -= timedelta(days=1)
        session.commit()
    executor()
    timed_out = _command(db, first["id"])
    assert (timed_out.state, timed_out.delivery_state) == ("unknown", "maybe_sent")

    _, other = _foreign_command(operator, "ST-05", "cap.vacuum_dry")
    executor()
    assert _command(db, other).state == "sent", "超时不等于设备已停下，新动作不能投给它"


def test_multi_channel_station_never_runs_more_actions_than_channels(operator, scripted, db, executor):
    """R2：两通道工位上两条动作都在执行（且已超时），第三条到了计划时间也要等。"""
    from app.models import Command, Station

    scripted["use"]("ST-07")
    with _session() as session:
        station = session.get(Station, "ST-07")
        original = station.channels
        station.channels = 2
        session.commit()
    try:
        first = _foreign_command(operator, "ST-07", "cap.test")[1]
        second = _foreign_command(operator, "ST-07", "cap.test")[1]
        executor()
        assert {_command(db, first).state, _command(db, second).state} == {"running"}
        with _session() as session:
            session.get(Command, first).started_at -= timedelta(days=1)
            session.commit()
        third = _foreign_command(operator, "ST-07", "cap.test")[1]
        executor()
        assert _command(db, first).state == "unknown", "超过硬上限转结果未知"
        assert _command(db, third).state == "sent", "两个通道都可能被占着，第三条不投递"
    finally:
        with _session() as session:
            session.get(Station, "ST-07").channels = original
            session.commit()


def test_station_in_maintenance_refuses_new_actions(operator, scripted, running_batch, db, executor):
    """R2：工位所属资产转入维护后，排队的动作在投递前被拦下（未投递、不自动重试）。"""
    from app.models import Asset, Station

    scripted["use"]("ST-05")
    with _session() as session:
        asset = session.get(Asset, session.get(Station, "ST-05").asset_id)
        asset.state = "maintenance"
        session.commit()
    try:
        executor()
        first = _commands(operator, running_batch, "dispatch")[0]
        assert (first["state"], first["delivery_state"]) == ("unknown", "unreachable")
        assert "维护" in first["error"]
    finally:
        with _session() as session:
            session.get(Asset, session.get(Station, "ST-05").asset_id).state = "active"
            session.commit()


def test_calibration_failed_mid_run_refuses_the_next_action(operator, running_batch, db, executor):
    """R2：批次开跑之后资产复校不合格：下一条动作在投递前被拦下，而不是只在开跑时查一次。"""
    from app.core.clock import now
    from app.models import CalibrationRecord, Station

    with _session() as session:
        asset_id = session.get(Station, "ST-05").asset_id
        record = CalibrationRecord(
            org_id="ORG-001", asset_id=asset_id, capability_scope=[], result="fail",
            effective_from=now() - timedelta(minutes=1), expires_at=None, note="复校不合格（测试）",
        )
        session.add(record)
        session.commit()
        record_id = record.id
    try:
        executor()
        first = _commands(operator, running_batch, "dispatch")[0]
        assert (first["state"], first["delivery_state"]) == ("unknown", "unreachable")
        assert "校准" in first["error"] and "未投递" in first["error"]
    finally:
        with _session() as session:
            session.delete(session.get(CalibrationRecord, record_id))
            session.commit()


# ---------- R3 手动重排 ----------


def test_manual_reschedule_leaves_live_dag_steps_alone(operator, scripted, db, executor):
    """R3：当前步骤是静置，第 2 步干燥已在 ST-05 上运行：不能从第 2 步重排，只能重排其后未开出的部分。"""
    from app.core.clock import now

    scripted["use"]("ST-05")
    batch_id = _new_batch(operator)
    _reshape(db, batch_id, _wait_beside_dry(5))
    _dispatch(operator, batch_id)
    executor()
    assert _detail(operator, batch_id)["current_step"] == 0
    # 称重要在干燥结束后 15 min 内开工：尾段从「现在」起排，实际起点由干燥的计划结束决定
    start = now().isoformat()

    refused = operator.post(f"/api/batches/{batch_id}/reschedule", {"from_step": 1, "start_from": start})
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["code"] == "step_in_progress"

    before = [row for row in _detail(operator, batch_id)["allocations"] if row["step_index"] == 1]
    tail = operator.post(f"/api/batches/{batch_id}/reschedule", {"from_step": 2, "start_from": start})
    assert tail.status_code == 200, tail.text
    after = [row for row in _detail(operator, batch_id)["allocations"] if row["step_index"] == 1]
    assert after == before, "已开出的干燥时间窗保持原样"


# ---------- R4 任务与批次的版本契约 ----------


def _approved_plan(researcher, qa, sample_count: int = 4) -> str:
    created = researcher.post("/api/plans", {
        "name": f"版本契约 {uuid.uuid4().hex[:4]}", "recipe_id": "R-205", "plan_type": "single_condition",
        "sample_count": sample_count, "required_metrics": [CAPACITY],
    })
    assert created.status_code == 201, created.text
    plan_id = created.json()["id"]
    _approve(researcher, qa, plan_id)
    return plan_id


def _approve(researcher, qa, plan_id: str) -> None:
    assert researcher.post(f"/api/plans/{plan_id}/lock").status_code == 200
    assert researcher.post(f"/api/plans/{plan_id}/submit").status_code == 200
    plan = researcher.get(f"/api/plans/{plan_id}").json()
    decided = qa.post(f"/api/plans/{plan_id}/decision", {
        "conclusion": "approved",
        "signature_id": qa.sign("批准方案", target=plan_id, object_version=plan["row_version"]),
    })
    assert decided.status_code == 200, decided.text
    assert decided.json()["approval_state"] == "approved"


def _plan_task(researcher, plan_id: str) -> dict:
    created = researcher.post("/api/experiment-tasks", {"plan_id": plan_id})
    assert created.status_code == 201, created.text
    return created.json()


def test_batch_follows_the_plan_version_pinned_by_its_task(researcher, qa, operator, reset_runtime):
    """R4：任务建在 v1；方案修订期间与 v2 批准之后，这个任务的批次都按 v1 执行，不静默变成 v2。"""
    plan_id = _approved_plan(researcher, qa, sample_count=4)
    first, second = _plan_task(researcher, plan_id), _plan_task(researcher, plan_id)
    assert first["plan_version"] == 1

    revised = researcher.post(f"/api/plans/{plan_id}/revisions")
    assert revised.status_code == 201, revised.text
    during = operator.post("/api/batches", {"plan_id": plan_id, "task_id": first["id"]})
    assert during.status_code == 201, during.text
    assert during.json()["plan_version"] == 1 and during.json()["sample_count"] == 4

    plan = researcher.get(f"/api/plans/{plan_id}").json()
    edited = researcher.patch(f"/api/plans/{plan_id}", {"sample_count": 6, "row_version": plan["row_version"]})
    assert edited.status_code == 200, edited.text
    _approve(researcher, qa, plan_id)

    pinned = operator.post("/api/batches", {"plan_id": plan_id, "task_id": second["id"]})
    assert pinned.status_code == 201, pinned.text
    assert pinned.json()["plan_version"] == 1, "任务锁定 v1，批次不能静默改用 v2"
    assert pinned.json()["sample_count"] == 4, "样本数也按任务锁定的版本"

    fresh = _plan_task(researcher, plan_id)
    assert fresh["plan_version"] == 2
    upgraded = operator.post("/api/batches", {"plan_id": plan_id, "task_id": fresh["id"]})
    assert upgraded.status_code == 201, upgraded.text
    assert upgraded.json()["plan_version"] == 2 and upgraded.json()["sample_count"] == 6


def test_cancelled_or_foreign_task_cannot_produce_a_batch(researcher, qa, operator, reset_runtime):
    """R4：别的方案的任务、已取消的任务都不能产生执行批次。"""
    plan_id = _approved_plan(researcher, qa)
    task = _plan_task(researcher, plan_id)

    foreign = operator.post("/api/batches", {"plan_id": "EP-205-01", "task_id": task["id"]})
    assert foreign.status_code == 409, foreign.text
    assert foreign.json()["detail"]["code"] == "task_plan_mismatch"

    cancelled = researcher.post(f"/api/experiment-tasks/{task['id']}/cancel", {"reason": "订单撤回"})
    assert cancelled.status_code == 200, cancelled.text
    refused = operator.post("/api/batches", {"plan_id": plan_id, "task_id": task["id"]})
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["code"] == "task_cancelled"


# ---------- R5 父任务依赖传递到子任务 ----------


def _task(researcher, **extra) -> dict:
    created = researcher.post("/api/experiment-tasks", {"plan_id": "EP-205-01", **extra})
    assert created.status_code == 201, created.text
    return created.json()


def _task_batch(operator, task_id: str) -> str:
    created = operator.post("/api/batches", {"plan_id": "EP-205-01", "task_id": task_id})
    assert created.status_code == 201, created.text
    return created.json()["id"]


def _work(detail: dict) -> list[dict]:
    return [row for row in detail["allocations"] if row["kind"] == "work"]


def test_children_inherit_the_parent_upstream(researcher, operator, reset_runtime):
    """R5：父任务 B 依赖 A，拆成 B1、B2 后，A 没完成前任何 B 的后代都不能先排、先跑。"""
    upstream, parent = _task(researcher), _task(researcher)
    assert researcher.put(
        f"/api/experiment-tasks/{parent['id']}/dependencies", {"depends_on": [upstream["id"]]},
    ).status_code == 200
    split = researcher.post(f"/api/experiment-tasks/{parent['id']}/decompose", {"parts": 2, "sequential": True})
    assert split.status_code == 200, split.text
    first, second = split.json()["children"]

    detail = researcher.get(f"/api/experiment-tasks/{first['id']}").json()
    assert detail["blocked_by"] and upstream["id"] in detail["blocked_by"][0]["label"]

    child_batch = _task_batch(operator, first["id"])
    early = operator.post(f"/api/batches/{child_batch}/schedule", {})
    assert early.status_code == 409 and early.json()["detail"]["code"] == "dependency_unscheduled"

    up_batch = _task_batch(operator, upstream["id"])
    assert operator.post(f"/api/batches/{up_batch}/schedule", {}).status_code == 200
    assert operator.post(f"/api/batches/{child_batch}/schedule", {}).status_code == 200
    up_end = max(row["ends_at"] for row in _work(_detail(operator, up_batch)))
    child_start = min(row["starts_at"] for row in _work(_detail(operator, child_batch)))
    assert child_start >= up_end, "子任务继承父任务的完成—开始依赖"
    preflight = operator.get(f"/api/batches/{child_batch}/preflight").json()
    upstream_check = next(row for row in preflight["checks"] if row["key"] == "upstream")
    assert upstream_check["state"] == "blocked"

    later = researcher.get(f"/api/experiment-tasks/{second['id']}").json()
    assert later["depends_on"] == [first["id"]], "顺序拆分：子任务之间的顺序照旧"
    assert any(upstream["id"] in row["label"] for row in later["blocked_by"]), "顺序拆分也保留父任务的外部依赖"

    cycle = researcher.put(f"/api/experiment-tasks/{upstream['id']}/dependencies", {"depends_on": [first["id"]]})
    assert cycle.status_code == 409 and cycle.json()["detail"]["code"] == "task_dependency_invalid", \
        "A 依赖 B1、B1 继承依赖 A：成环"


def test_parent_cannot_gain_upstream_after_a_child_batch_is_running(researcher, operator, reset_runtime):
    """R5：子任务的批次已下发时，父任务再加上游等于给在跑的批次追加前置，必须拒绝。"""
    parent, upstream = _task(researcher), _task(researcher)
    split = researcher.post(f"/api/experiment-tasks/{parent['id']}/decompose", {"parts": 2})
    child = split.json()["children"][0]
    batch_id = _task_batch(operator, child["id"])
    _dispatch(operator, batch_id)

    refused = researcher.put(f"/api/experiment-tasks/{parent['id']}/dependencies", {"depends_on": [upstream["id"]]})
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["code"] == "batch_in_flight"


# ---------- R6 工位通道与资产容量 ----------


def test_station_channels_cannot_exceed_asset_capacity(admin, reset_runtime):
    """R6：一台资产同一时刻只能承接 capacity 份作业，映射到它的工位通道数不能超过这个数。"""
    from app.models import Asset, Station

    with _session() as db:
        for station in db.query(Station).filter(Station.asset_id != "").all():
            asset = db.get(Asset, station.asset_id)
            assert (station.channels or 1) <= asset.capacity, f"{station.id} 通道数超过资产 {asset.asset_no} 容量"
        cycler = db.get(Station, "ST-07")
        asset = db.get(Asset, cycler.asset_id)
        asset_id, capacity, station_version = asset.id, asset.capacity, cycler.row_version
        asset_version = asset.row_version

    widened = admin.patch("/api/stations/ST-07", {"channels": capacity + 1, "row_version": station_version})
    assert widened.status_code == 409, widened.text
    assert widened.json()["detail"]["code"] == "channels_exceed_asset_capacity"
    shrunk = admin.patch(f"/api/assets/{asset_id}", {"capacity": 1, "row_version": asset_version})
    assert shrunk.status_code == 409, shrunk.text
    assert shrunk.json()["detail"]["code"] == "channels_exceed_asset_capacity"


# ---------- R7 计划完成时间 ----------


def _trailing_wait(steps):
    return [*steps, {"step_id": "w-tail", "name": "化成后静置", "kind": "wait", "dur": 60,
                     "wait_for": {"mode": "duration"}}]


def test_trailing_wait_pushes_the_downstream_start(researcher, operator, db, reset_runtime):
    """R7：上游设备做完还要静置 60 min：下游开工以静置结束为准，不以最后一个设备时间窗为准。"""
    upstream, downstream = _task(researcher), _task(researcher)
    assert researcher.put(
        f"/api/experiment-tasks/{downstream['id']}/dependencies", {"depends_on": [upstream["id"]]},
    ).status_code == 200
    up_batch, down_batch = _task_batch(operator, upstream["id"]), _task_batch(operator, downstream["id"])
    _reshape(db, up_batch, _trailing_wait)
    assert operator.post(f"/api/batches/{up_batch}/schedule", {}).status_code == 200
    assert operator.post(f"/api/batches/{down_batch}/schedule", {}).status_code == 200

    last_work = max(datetime.fromisoformat(row["ends_at"]) for row in _work(_detail(operator, up_batch)))
    first_down = min(datetime.fromisoformat(row["starts_at"]) for row in _work(_detail(operator, down_batch)))
    assert first_down >= last_work + timedelta(minutes=60), "尾部静置也是上游工艺时间"


def test_optimizer_completion_includes_trailing_wait(operator, db, reset_runtime):
    """R7：多批次优化的完成时间与拖期按全部工艺时间算。"""
    batch_id = _new_batch(operator)
    _reshape(db, batch_id, _trailing_wait)
    preview = operator.post("/api/schedule/optimize", {"batch_ids": [batch_id]})
    assert preview.status_code == 200, preview.text
    best = preview.json()["best"]
    last_work = max(row["ends_at"] for row in best["plans"][batch_id] if row["kind"] == "work")
    assert datetime.fromisoformat(best["finish_at"]) >= datetime.fromisoformat(last_work) + timedelta(minutes=59)


# ---------- R8 清洗 ----------


def test_station_needing_cleaning_waits_for_confirmation_even_after_the_batch_is_done(
    operator, scripted, db, executor,
):
    """R8：上一批已完成但清洗未确认，下一批仍拿不到这台设备；确认清洗后才释放。"""
    from app.core.context import system_context
    from app.models import Capability, Station
    from app.services.schedule_service import ScheduleService

    devices = scripted["use"]("ST-05")
    with _session() as session:
        capability = session.get(Capability, "cap.vacuum_dry")
        original = dict(capability.recovery or {})
        capability.recovery = {**original, "cleanAfter": True}
        session.commit()
    try:
        batch_id = _new_batch(operator)
        _reshape(db, batch_id, lambda steps: steps[:1])
        _dispatch(operator, batch_id)
        executor()
        dry = _commands(operator, batch_id, "dispatch")[0]
        devices["finished"].add(dry["id"])
        executor()
        assert _detail(operator, batch_id)["state"] == "done"
        db.expire_all()
        station = db.get(Station, "ST-05")
        assert station.clean is False, "需要清洗的动作做完，设备转为待清洗"

        _, other = _foreign_command(operator, "ST-05", "cap.vacuum_dry")
        executor()
        assert _command(db, other).state == "sent", "清洗未确认，下一批不能用这台设备"
        busy = ScheduleService(db, system_context("ORG-001")).context().busy.get("ST-05", [])
        assert busy, "排程也把待清洗的设备当作占用"

        confirmed = operator.patch(
            "/api/stations/ST-05/readiness", {"clean": True, "status": "idle", "row_version": station.row_version},
        )
        assert confirmed.status_code == 200, confirmed.text
        executor()
        assert _command(db, other).state == "running", "确认清洗后设备释放"
    finally:
        with _session() as session:
            session.get(Capability, "cap.vacuum_dry").recovery = original
            station = session.get(Station, "ST-05")
            station.clean = True
            station.dirty_batch_id = ""
            session.commit()


# ---------- R9 重排建议整组替换 ----------


def test_swapping_two_batches_applies_as_one_replacement(operator, db, reset_runtime):
    """R9：A、B 交换顺序的建议合法：先写 A 的新时间窗时 B 的旧时间窗还在，不能因此判冲突。"""
    from app.core.context import system_context
    from app.models import Batch, ScheduleProposal
    from app.services.reschedule_service import RescheduleService

    first, second = _new_batch(operator), _new_batch(operator)
    assert operator.post(f"/api/batches/{first}/schedule", {}).status_code == 200
    assert operator.post(f"/api/batches/{second}/schedule", {}).status_code == 200
    service = RescheduleService(db, system_context("ORG-001"))
    before = {batch_id: service._before(db.get(Batch, batch_id)) for batch_id in (first, second)}
    proposal = ScheduleProposal(
        org_id="ORG-001", trigger="manual", reason="交换顺序", station_id="", batch_ids=[first, second],
        before=before,
        after={first: {"from_step": 0, "allocations": before[second]},
               second: {"from_step": 0, "allocations": before[first]}},
        impact={}, unplanned=[],
    )
    db.add(proposal)
    db.commit()

    applied = operator.post(f"/api/schedule/proposals/{proposal.id}/apply")
    assert applied.status_code == 200, applied.text
    windows = lambda batch_id: sorted(  # noqa: E731
        (row["step_index"], row["kind"], row["station_id"], row["starts_at"]) for row in _detail(operator, batch_id)["allocations"]
    )
    assert windows(first) == sorted(
        (row["step_index"], row["kind"], row["station_id"], row["starts_at"][:16]) for row in before[second]
    )


def test_proposal_is_stale_when_maintenance_lands_on_its_windows(operator, admin, db, reset_runtime):
    """R9：建议生成后新增维护占满资产：应用时判为过期并提示重新计算。"""
    from app.core.context import system_context
    from app.models import Batch, Station
    from app.services.reschedule_service import RescheduleService

    batch_id = _new_batch(operator)
    assert operator.post(f"/api/batches/{batch_id}/schedule", {}).status_code == 200
    service = RescheduleService(db, system_context("ORG-001"))
    proposal = service.propose(trigger="manual", reason="演练", batch_ids=[batch_id])
    db.commit()
    work = [row for row in proposal.after[batch_id]["allocations"] if row["kind"] == "work"]
    target = work[0]
    asset_id = db.get(Station, target["station_id"]).asset_id
    booked = admin.post("/api/resource-bookings", {
        "asset_id": asset_id, "kind": "maintenance", "reason": "临时检修",
        "starts_at": target["starts_at"], "ends_at": target["ends_at"],
    })
    assert booked.status_code in {200, 201}, booked.text

    applied = operator.post(f"/api/schedule/proposals/{proposal.id}/apply")
    assert applied.status_code == 409, applied.text
    assert applied.json()["detail"]["code"] == "proposal_stale"
    assert db.get(Batch, batch_id) is not None


# ---------- 单一「当前步骤」假设 ----------


def test_parallel_wait_does_not_push_the_planned_start_back(operator, db, reset_runtime):
    """与干燥并行的长静置不在干燥之前：开跑检查的计划开始时间不能因此被算成几小时前。"""
    batch_id = _new_batch(operator)
    _reshape(db, batch_id, _wait_beside_dry(240))
    assert operator.post(f"/api/batches/{batch_id}/schedule", {}).status_code == 200
    preflight = operator.get(f"/api/batches/{batch_id}/preflight?manual_review=true").json()
    schedule_check = next(row for row in preflight["checks"] if row["key"] == "schedule")
    assert schedule_check["state"] == "pass", schedule_check["detail"]


def test_due_windows_follow_the_graph_not_the_first_open_step(operator, db, reset_runtime):
    """硬时限倒计时：静置还开着（当前步骤 0），干燥已完成，带硬时限的称重正等着开工。"""
    from app.core.clock import now
    from app.core.context import system_context
    from app.models import Batch, Checkpoint, Command, StepRun
    from app.services.batch_service import BatchService

    batch_id = _new_batch(operator)
    _reshape(db, batch_id, _wait_beside_dry(240))
    batch = db.get(Batch, batch_id)
    steps = batch.recipe_snapshot["steps"]
    batch.state = "running"
    batch.current_step = 0
    finished = now() - timedelta(minutes=5)
    db.add(StepRun(org_id=batch.org_id, batch_id=batch_id, step_id="w0", step_index=0, kind="wait",
                   attempt=1, state="waiting", step_snapshot=steps[0], started_at=now()))
    db.add(StepRun(org_id=batch.org_id, batch_id=batch_id, step_id=steps[1]["step_id"], step_index=1,
                   kind="device", attempt=1, state="completed", step_snapshot=steps[1], started_at=finished,
                   ended_at=finished))
    dry = Command(org_id=batch.org_id, batch_id=batch_id, station_id="ST-05", capability="cap.vacuum_dry",
                  params={}, type="dispatch", state="done", delivery_state="delivered", step_index=1)
    db.add(dry)
    db.flush()
    db.add(Checkpoint(batch_id=batch_id, command_id=dry.id, step_index=1, state="done", payload={},
                      created_at=finished))
    db.commit()

    rows = [row for row in BatchService(db, system_context("ORG-001")).due_windows() if row["batch_id"] == batch_id]
    assert [row["step_index"] for row in rows] == [2], "称重的硬时限从干燥结束起算"
    assert 8 <= rows[0]["remaining_min"] <= 10
