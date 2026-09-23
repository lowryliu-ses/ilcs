"""并发执行器与变更推送。

- 通知与业务写入同事务：提交才发、回滚不发；心跳这类高频无意义更新不推送。
- 并发执行器走完整条批次，与串行回路结论一致。
- 一台工位的回路卡住，只拖住它自己：其他工位照常投递，执行器心跳照常写。
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
