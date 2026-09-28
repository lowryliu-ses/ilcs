"""`sql_table_v1`（数据库中间表）× 中间库模拟设备：设备侧轮询作业表、回写状态与结果。

SQLite 跑行为用例；另有一条用例在测试用的 PostgreSQL 上走一遍，验证真实方言。
"""
import json
import time

import pytest

from sim_harness import _device, record, request


@pytest.fixture()
def exchange(tmp_path):
    from simulators.sql_device.worker import SqlDeviceWorker

    url = f"sqlite:///{tmp_path / 'exchange.db'}"
    worker = SqlDeviceWorker(url, _device("SIM-SQL-T", task_seconds=0.3, methods=[{"program": "MIX-A"}]),
                             poll_seconds=0.05)
    worker.start()
    try:
        yield worker, url
    finally:
        worker.stop()


def _adapter(url: str, **config):
    from app.adapters.sql_table import SqlTableAdapter

    return SqlTableAdapter(record("数据库中间表", {"url": url, "device_id": "SIM-SQL-T", "request_timeout_sec": 2,
                                                  "heartbeat_stale_sec": 2, **config}))


def _wait(adapter, command_id: str, seconds: float = 5, states=("done", "failed")):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = adapter.query(command_id)
        if result is not None and result.state in states:
            return result
        time.sleep(0.05)
    raise AssertionError(f"作业 {command_id} 没有在时限内到达 {states}")


def _rows(url: str) -> list[dict]:
    from sqlalchemy import create_engine, text

    engine = create_engine(url)
    with engine.begin() as connection:
        rows = [dict(row) for row in connection.execute(text("SELECT * FROM ilcs_jobs")).mappings()]
    engine.dispose()
    return rows


def test_job_row_is_picked_up_executed_and_replayed_by_primary_key(exchange):
    worker, url = exchange
    adapter = _adapter(url)
    health = adapter.healthcheck()
    assert (health["device_id"], health["simulator"], health["accepts_commands"]) == ("SIM-SQL-T", True, True)
    assert adapter.identity()["methods"] == [{"program": "MIX-A"}], "设备表里的程序目录作为设备自报"

    accepted = adapter.submit(request("CMD-S1", program="MIX-A"))
    assert accepted.state == "accepted" and accepted.origin == "real:sql_table_v1"
    adapter.submit(request("CMD-S1", program="MIX-A"))
    rows = _rows(url)
    assert len(rows) == 1 and rows[0]["program"] == "MIX-A", "重投同一指令号只撞主键"
    assert json.loads(rows[0]["context_json"])["step_id"] == "s01"

    done = _wait(adapter, "CMD-S1")
    assert done.state == "done" and done.telemetry, "设备侧回写的遥测随回执带回"
    assert worker.device.executions["CMD-S1"] == 1
    assert adapter.query("CMD-NEVER-SEEN") is None


def test_rejection_hold_abort_and_stalled_device_side(exchange):
    from app.adapters import AdapterUnreachable

    worker, url = exchange
    adapter = _adapter(url)
    worker.device.set_fault("interlock")
    adapter.submit(request("CMD-I"))
    rejected = _wait(adapter, "CMD-I")
    assert rejected.state == "failed" and "Interlocked" in rejected.error
    worker.device.set_fault("none")

    worker.device.task_seconds = 30
    adapter.submit(request("CMD-H"))
    _wait(adapter, "CMD-H", states=("running",))
    assert adapter.hold(request("CMD-HOLD", "hold", target="CMD-H")).state == "done"
    assert worker.device.tasks["CMD-H"].state == "held"
    assert adapter.abort(request("CMD-ABORT", "abort", target="CMD-H")).state == "done"
    assert _wait(adapter, "CMD-H").state == "failed"

    # 设备侧软件停了：作业表还能写，但心跳不更新、控制请求没人处理
    from sqlalchemy import create_engine, text

    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("UPDATE ilcs_device SET sim_fault = 'offline 30'"))
    engine.dispose()
    time.sleep(0.3)
    with pytest.raises(AdapterUnreachable, match="没有处理"):
        _adapter(url, request_timeout_sec=0.3).hold(request("CMD-HOLD2", "hold", target="CMD-H"))
    time.sleep(2.2)
    with pytest.raises(AdapterUnreachable, match="心跳"):
        adapter.healthcheck()


def test_configuration_guards():
    from app.adapters import AdapterError
    from app.core.config import settings

    with pytest.raises(AdapterError, match="口令不能写进 url"):
        _adapter("postgresql+psycopg2://u:secret@127.0.0.1/x")
    with pytest.raises(AdapterError, match="白名单"):
        _adapter("postgresql+psycopg2://u@evil.example/x")
    with pytest.raises(AdapterError, match="不支持"):
        _adapter("oracle://u@127.0.0.1/x")
    with pytest.raises(AdapterError, match="表名"):
        _adapter("sqlite:///x.db", jobs_table="jobs; DROP TABLE x")
    environment = settings.environment
    try:
        settings.environment = "production"
        with pytest.raises(AdapterError, match="SQLite"):
            _adapter("sqlite:///x.db")
    finally:
        settings.environment = environment


def test_postgresql_exchange_with_password_from_credential_file(tmp_path, monkeypatch):
    """在测试用的 PostgreSQL 上跑一遍：口令从凭据文件读，连接串里不带口令。"""
    from sqlalchemy.engine import make_url

    from app.adapters.sql_table import SqlTableAdapter
    from app.core.config import settings
    from simulators.sql_device.worker import SqlDeviceWorker
    from tests.conftest import TEST_DATABASE_URL

    url = make_url(TEST_DATABASE_URL)
    monkeypatch.setattr(settings, "adapter_credential_root", str(tmp_path))
    (tmp_path / "exchange.password").write_text(url.password or "")
    worker = SqlDeviceWorker(url.render_as_string(hide_password=False), _device("SIM-SQL-PG", task_seconds=0.2),
                             jobs="ilcs_test_jobs", devices="ilcs_test_device", poll_seconds=0.05)
    worker.start()
    try:
        adapter = SqlTableAdapter(record("数据库中间表", {
            "url": url._replace(password=None).render_as_string(hide_password=False), "device_id": "SIM-SQL-PG",
            "jobs_table": "ilcs_test_jobs", "device_table": "ilcs_test_device", "request_timeout_sec": 2,
        }, f"file://{tmp_path / 'exchange.password'}"))
        assert adapter.healthcheck()["device_id"] == "SIM-SQL-PG"
        adapter.submit(request("CMD-PG-1"))
        assert _wait(adapter, "CMD-PG-1").state == "done"
    finally:
        worker.stop()
        from sqlalchemy import create_engine, text

        engine = create_engine(url)
        with engine.begin() as connection:
            connection.execute(text("DROP TABLE IF EXISTS ilcs_test_jobs"))
            connection.execute(text("DROP TABLE IF EXISTS ilcs_test_device"))
        engine.dispose()
