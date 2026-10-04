"""并发执行器与变更推送。

- 通知与业务写入同事务：提交才发、回滚不发；心跳这类高频无意义更新不推送。
- 并发执行器走完整条批次，与串行回路结论一致。
- 一台工位的回路卡住，只拖住它自己：其他工位照常投递，执行器心跳照常写。
- 失锁即熔断：看门狗发现持锁连接断了就取消排队的工位任务、立即退出；工位线程领取前核对执行权，失锁的旧执行器
  不再领走指令。
"""
import json
import select
import threading
import time

import pytest

from test_failure_paths import running_batch  # noqa: F401
from tests.conftest import ORG


@pytest.fixture()
def listener():
    """一条独立的 LISTEN 连接，收集提交后投递的通知。"""
    from app.core.db import engine
    from app.core.events import EVENTS_CHANNEL, QUEUE_CHANNEL

    raw = engine.raw_connection()
    dbapi = raw.dbapi_connection
    dbapi.autocommit = True
    with dbapi.cursor() as cursor:
        cursor.execute(f"LISTEN {EVENTS_CHANNEL}")
        cursor.execute(f"LISTEN {QUEUE_CHANNEL}")

    def drain(wait: float = 0.3) -> list[tuple[str, dict]]:
        deadline = time.monotonic() + wait
        found = []
        while time.monotonic() < deadline:
            readable, _, _ = select.select([dbapi], [], [], max(0.0, deadline - time.monotonic()))
            if readable:
                dbapi.poll()
                while dbapi.notifies:
                    note = dbapi.notifies.pop(0)
                    found.append((note.channel, json.loads(note.payload) if note.payload else {}))
        return found

    drain(0.1)
    yield drain
    raw.invalidate()


def test_change_notification_is_sent_on_commit_only(running_batch, db, listener):
    from app.models import Batch

    batch = db.get(Batch, running_batch)
    batch.priority = (batch.priority or 0) + 1
    db.rollback()
    assert not [n for n in listener() if n[1].get("topic") == "batches"], "回滚的改动不能推送"

    batch = db.get(Batch, running_batch)
    batch.priority = (batch.priority or 0) + 1
    db.commit()
    notes = [payload for channel, payload in listener() if payload.get("topic") == "batches"]
    assert notes and running_batch in notes[0]["ids"] and notes[0]["org"] == ORG


def test_heartbeat_only_updates_are_not_pushed(reset_runtime, db, listener):
    from app.core.clock import now
    from app.models import Adapter

    adapter = db.get(Adapter, "ST-05")
    adapter.last_heartbeat = now()
    db.commit()
    assert not [n for n in listener() if n[1].get("topic") == "stations"]

    adapter.site_interlock = True
    db.commit()
    topics = {payload.get("topic") for _, payload in listener()}
    assert {"stations", "gate"} <= topics
    adapter.site_interlock = False
    db.commit()


def test_new_command_wakes_the_executor(operator, reset_runtime, listener):
    from app.core.events import QUEUE_CHANNEL

    created = operator.post("/api/batches", {"plan_id": "EP-205-01"})
    batch_id = created.json()["id"]
    assert operator.post(f"/api/batches/{batch_id}/schedule", {}).status_code == 200
    listener()
    dispatched = operator.post(
        f"/api/batches/{batch_id}/dispatch",
        {"manual_review": True, "signature_id": operator.sign("批准执行", target=batch_id)},
    )
    assert dispatched.status_code == 200, dispatched.text
    channels = {channel for channel, _ in listener()}
    assert QUEUE_CHANNEL in channels, "新指令入队要唤醒执行器，不等满轮询周期"


def test_concurrent_executor_runs_a_batch_to_completion(operator, running_batch):
    from app.core.db import SessionLocal
    from app.services.executor_runtime import ConcurrentExecutor

    runtime = ConcurrentExecutor(SessionLocal, workers=4, station_wait_sec=5.0)
    try:
        for _ in range(20):
            runtime.cycle()
            if operator.get(f"/api/batches/{running_batch}").json()["state"] == "done":
                break
        assert operator.get(f"/api/batches/{running_batch}").json()["state"] == "done"
    finally:
        runtime.drain(timeout=10)
        runtime.shutdown()

    from app.models import ExecutorHeartbeat
    from app.services.monitoring_service import EXECUTOR_ID

    with SessionLocal() as db:
        detail = db.get(ExecutorHeartbeat, EXECUTOR_ID).detail
    assert detail["mode"] == "concurrent" and detail["workers"] == 4


def test_stuck_station_does_not_block_other_stations_or_heartbeat(monkeypatch, db):
    from app.core.db import SessionLocal
    from app.models import ExecutorHeartbeat
    from app.services import execution_service
    from app.services.executor_runtime import ConcurrentExecutor
    from app.services.monitoring_service import EXECUTOR_ID

    release = threading.Event()
    served: list[str] = []

    def fake_pass(self, station_id, **_kwargs):
        if station_id == "ST-SLOW":
            release.wait(10)
        served.append(station_id)
        return {"executed": 1}

    monkeypatch.setattr(execution_service.ExecutorLoop, "station_pass", fake_pass)
    monkeypatch.setattr(
        execution_service.ExecutorLoop, "stations_needing_work", lambda self: {"ST-SLOW", "ST-FAST"}
    )
    runtime = ConcurrentExecutor(SessionLocal, workers=4, station_wait_sec=0.3)
    try:
        started = time.monotonic()
        first = runtime.cycle()
        assert time.monotonic() - started < 3, "卡住的工位不能拖住整轮"
        assert "ST-FAST" in served and first["stations_busy"] == 1
        seen_before = db.get(ExecutorHeartbeat, EXECUTOR_ID).last_seen

        second = runtime.cycle()
        assert second["stations_dispatched"] == 1, "仍在跑的工位不重复派活"
        db.expire_all()
        assert db.get(ExecutorHeartbeat, EXECUTOR_ID).last_seen >= seen_before
    finally:
        release.set()
        runtime.drain(timeout=10)
        runtime.shutdown()
    assert served.count("ST-SLOW") == 1


def test_stream_requires_a_token(client):
    assert client.get("/api/stream").status_code == 401


def test_stream_pushes_changes_for_own_organization(client, operator, running_batch, monkeypatch):
    from app.core import stream_hub
    from app.core.config import settings

    monkeypatch.setattr(settings, "stream_max_sec", 2.0)

    def later():
        time.sleep(0.6)
        stream_hub.hub.publish_local({"topic": "batches", "org": ORG, "ids": [running_batch]})
        stream_hub.hub.publish_local({"topic": "batches", "org": "ORG-OTHER", "ids": ["B-LEAK"]})

    threading.Thread(target=later, daemon=True).start()
    response = client.get("/api/stream", headers=operator.headers)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-accel-buffering"] == "no"
    body = response.text
    assert "event: hello" in body and "event: bye" in body
    assert running_batch in body
    assert "B-LEAK" not in body, "别的组织的变更不能推给本组织"


def test_only_one_executor_process_dispatches_at_a_time():
    """执行器按会话级 advisory lock 主备：第二个进程拿不到锁就待命，不会与第一个同时对账、投递。

    指令领取、对账、轮询都按「只有我在处理这些指令」写的；多副本部署靠这把锁保证同一时刻只有一个在工作，
    主副本退出或断线后待命的副本接管。
    """
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[3] / "executor" / "main.py"
    spec = importlib.util.spec_from_file_location("ilcs_executor_main_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    first = module.acquire_singleton()
    assert first, "第一个执行器拿到锁"
    try:
        module._Stop.requested = True
        assert module.acquire_singleton() is False, "锁被占着时第二个执行器待命，不工作"
    finally:
        module._Stop.requested = False
        # 真断开会话（进程退出就是这样）：close() 只是把连接还回连接池，会话级锁还在池里的那条连接上
        first.invalidate()
    takeover = module.acquire_singleton()
    assert takeover, "主副本退出后待命的副本接管"
    takeover.invalidate()


def _load_executor_main():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[3] / "executor" / "main.py"
    spec = importlib.util.spec_from_file_location("ilcs_executor_main_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _hold_executor_lock():
    """另开一条连接拿执行器锁（像一个执行器那样），返回（连接, 后端进程号）。"""
    from sqlalchemy import text

    from app.core.db import ADVISORY_NAMESPACE, engine
    from app.services.leadership import LOCK_KEY

    holder = engine.connect()
    acquired = holder.execute(text("SELECT pg_try_advisory_lock(:namespace, hashtext(:key))"),
                              {"namespace": ADVISORY_NAMESPACE, "key": LOCK_KEY}).scalar()
    assert acquired, "执行器锁没被别人占着"
    pid = holder.execute(text("SELECT pg_backend_pid()")).scalar()
    holder.commit()
    return holder, pid


def _kill_backend(db, pid):
    """库端断开持锁连接（运维 kill、连接被代理掐掉）：锁随会话一起释放。"""
    from sqlalchemy import text

    db.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
    db.commit()


def test_execution_right_follows_the_lock_connection(db):
    """执行权按持锁连接的后端进程号认：锁在就放行；持锁连接被库端断开、锁随之释放，就不再放行。"""
    from app.core.db import ADVISORY_NAMESPACE
    from app.services.leadership import Leadership

    holder, pid = _hold_executor_lock()
    leadership = Leadership(pid, ADVISORY_NAMESPACE)
    try:
        assert leadership.holds(db)
        assert not Leadership(pid + 100000, ADVISORY_NAMESPACE).holds(db), "别的进程号不算持锁"
        _kill_backend(db, pid)
        deadline = time.monotonic() + 3
        while leadership.holds(db) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not leadership.holds(db)
    finally:
        holder.invalidate()


def test_lease_watch_exits_the_executor_when_the_lock_connection_dies(db):
    """看门狗每个周期在持锁连接上核对一次：连接被断开就熔断，执行器取消排队的工位任务、立即以 3 退出。"""
    from app.core.db import ADVISORY_NAMESPACE
    from app.services.leadership import Leadership, LeaseWatch

    module = _load_executor_main()
    exits: list[int] = []
    fenced: list[bool] = []

    class Runtime:
        def fence(self):
            fenced.append(True)

    holder, pid = _hold_executor_lock()
    leadership = Leadership(pid, ADVISORY_NAMESPACE)
    watch = LeaseWatch(holder, leadership, interval=0.05,
                       on_lost=lambda reason: module.abandon(Runtime(), reason, exit_=exits.append)).start()
    try:
        time.sleep(0.3)
        assert not exits and not leadership.lost.is_set(), "锁还在：看门狗不动"
        _kill_backend(db, pid)
        deadline = time.monotonic() + 3
        while not exits and time.monotonic() < deadline:
            time.sleep(0.05)
        assert exits == [3] and fenced == [True]
        assert leadership.lost.is_set() and "持锁连接" in leadership.reason
    finally:
        watch.stop()
        holder.invalidate()


def test_executor_that_lost_the_lock_does_not_claim_commands(running_batch, db, executor):
    """失锁的旧执行器（看门狗还没发现）在领取前核对执行权：指令留在队列里、没有台账，接管的副本照常投递。"""
    from app.core.db import ADVISORY_NAMESPACE, SessionLocal
    from app.models import AdapterExecution, Command
    from app.services import leadership
    from app.services.execution_service import ExecutorLoop

    leadership.install(leadership.Leadership(0, ADVISORY_NAMESPACE))  # 进程号 0：没有哪个会话持着锁
    try:
        with SessionLocal() as session:
            with pytest.raises(leadership.LeadershipLost):
                ExecutorLoop(session).tick()
            session.rollback()
        assert leadership.fenced()
        command = db.query(Command).filter(Command.batch_id == running_batch, Command.type == "dispatch").one()
        assert (command.state, command.delivery_state) == ("sent", "queued")
        assert db.get(AdapterExecution, command.id) is None
    finally:
        leadership.install(None)
    executor()
    db.expire_all()
    assert db.get(Command, command.id).delivery_state != "queued", "接管的执行器照常投递"


def test_fenced_runtime_cancels_station_jobs_that_have_not_started(monkeypatch):
    """熔断时还在排队的工位任务取消、不再开始（shutdown(wait=False) 不会取消它们）；熔断之后不再开始新的一轮。"""
    from app.core.db import SessionLocal
    from app.services import execution_service
    from app.services.executor_runtime import ConcurrentExecutor
    from app.services.leadership import LeadershipLost

    release = threading.Event()
    served: list[str] = []

    def fake_pass(self, station_id, **_kwargs):
        if station_id == "ST-A":
            release.wait(10)
        served.append(station_id)
        return {"executed": 1}

    monkeypatch.setattr(execution_service.ExecutorLoop, "station_pass", fake_pass)
    monkeypatch.setattr(execution_service.ExecutorLoop, "stations_needing_work", lambda self: {"ST-A", "ST-B"})
    runtime = ConcurrentExecutor(SessionLocal, workers=1, station_wait_sec=0.2)
    try:
        runtime.cycle()
        runtime.fence()
        release.set()
        runtime.drain(timeout=10)
        assert served == ["ST-A"], "只有已经在跑的那个做完；排队的 ST-B 被取消"
        with pytest.raises(LeadershipLost):
            runtime.cycle()
    finally:
        release.set()
        runtime.shutdown()
