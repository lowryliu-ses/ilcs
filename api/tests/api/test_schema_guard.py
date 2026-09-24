"""AC-02、AC-40：应用启动只校验库版本，不自行改结构。"""
import json
import os
from pathlib import Path
import subprocess
import uuid

import pytest


@pytest.fixture()
def scratch_database():
    """同一 PostgreSQL 实例上的一次性空库，用完删除。验证「空库」行为不能借用测试主库。"""
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    from conftest import TEST_DATABASE_URL

    name = f"test_scratch_{uuid.uuid4().hex[:10]}"
    admin = create_engine(TEST_DATABASE_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield make_url(TEST_DATABASE_URL).set(database=name).render_as_string(hide_password=False)
    finally:
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def test_expected_revision_matches_the_latest_migration():
    """代码期待的版本必须是迁移目录里的最后一个，否则升级后启动会判为「超前」。"""
    from app.core.schema import EXPECTED_REVISION, known_revisions

    revisions = known_revisions()
    assert revisions, "迁移目录不能为空"
    assert EXPECTED_REVISION == revisions[-1]


def test_checked_in_openapi_contract_matches_the_application():
    """接口增删改必须同步交付契约，不能让集成方读到上一版路由。"""
    from app.main import app

    stored = json.loads((Path(__file__).parents[3] / "contracts" / "openapi.json").read_text())
    assert stored == app.openapi(), (
        "contracts/openapi.json 已漂移；运行 scripts/export-openapi.py 后提交生成结果"
    )


def test_non_postgresql_database_url_is_rejected():
    """只支持 PostgreSQL：SQLite 连接串在配置加载时就被拒绝，不会带着另一套锁语义跑起来。"""
    from pydantic import ValidationError

    from app.core.config import Settings

    with pytest.raises(ValidationError, match="PostgreSQL"):
        Settings(database_url="sqlite:///ilcs.db")


def test_fresh_database_migrations_do_not_create_business_data(scratch_database):
    """结构迁移不能在全新正式库里伪造组织、账号、指标或演示业务记录。"""
    from sqlalchemy import create_engine, text

    api_dir = Path(__file__).parents[2]
    result = subprocess.run(
        [str(api_dir / ".venv" / "bin" / "alembic"), "upgrade", "head"],
        cwd=api_dir,
        capture_output=True,
        text=True,
        env={**os.environ, "ILCS_DATABASE_URL": scratch_database},
    )
    assert result.returncode == 0, result.stderr

    tables = (
        "organizations", "labs", "users", "memberships", "service_identities",
        "metric_definitions", "stations", "recipes", "plans", "lots",
    )
    engine = create_engine(scratch_database)
    try:
        with engine.connect() as connection:
            counts = {
                table: connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()
                for table in tables
            }
            revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
    finally:
        engine.dispose()
    assert counts == {table: 0 for table in tables}
    from app.core.schema import EXPECTED_REVISION

    assert revision == EXPECTED_REVISION


def test_production_configuration_rejects_placeholders():
    from app.core.config import Settings

    unsafe = Settings(
        environment="production",
        secret_key="__CHANGE_ME__",
        password_pepper="__CHANGE_ME_PASSWORD_PEPPER__",
        database_url="postgresql+psycopg2://ilcs:secret@db:5432/ilcs",
        cors_origins="*",
    )
    issues = unsafe.production_issues()
    # 令牌密钥、口令 pepper、CORS 通配、设备网关白名单
    assert len(issues) == 4, issues
    safe = Settings(
        environment="production",
        secret_key="token-" + "a" * 40,
        password_pepper="password-" + "b" * 40,
        database_url="postgresql+psycopg2://ilcs:secret@db:5432/ilcs",
        cors_origins="https://ilcs.example.internal",
        adapter_allowed_hosts="instrument-gateway.lab.internal,10.20.1.31",
    )
    assert safe.production_issues() == []

    unsafe_cleanup = Settings(
        environment="production",
        secret_key="token-" + "a" * 40,
        password_pepper="password-" + "b" * 40,
        database_url="postgresql+psycopg2://ilcs:secret@db:5432/ilcs",
        cors_origins="https://ilcs.example.internal",
        adapter_allowed_hosts="instrument-gateway.lab.internal",
        file_orphan_retention_hours=0,
        file_cleanup_interval_sec=0,
        file_cleanup_batch_size=0,
    )
    cleanup_issues = unsafe_cleanup.production_issues()
    assert any("FILE_ORPHAN_RETENTION_HOURS" in issue for issue in cleanup_issues)
    assert any("FILE_CLEANUP_INTERVAL_SEC" in issue for issue in cleanup_issues)
    assert any("FILE_CLEANUP_BATCH_SIZE" in issue for issue in cleanup_issues)


def test_startup_does_not_create_tables_or_seed(scratch_database):
    """空库启动不建表、不播种，只报告版本不兼容。"""
    import importlib
    import os
    import sys

    from fastapi.testclient import TestClient

    original = os.environ.get("ILCS_DATABASE_URL")
    os.environ["ILCS_DATABASE_URL"] = scratch_database
    for name in [n for n in list(sys.modules) if n.startswith("app.")]:
        del sys.modules[name]
    try:
        import app.main as main

        importlib.reload(main)
        with TestClient(main.app) as client:
            health = client.get("/api/health").json()
            assert health["status"] == "schema_mismatch"
            assert "没有迁移版本记录" in health["detail"]
            # 业务接口一律 503：不兼容的副本不能接流量
            blocked = client.post(
                "/api/auth/login", json={"username": "operator", "password": "ilcs1234"}
            )
            assert blocked.status_code == 503
            assert blocked.json()["detail"]["code"] == "schema_mismatch"

        from sqlalchemy import create_engine, inspect

        engine = create_engine(scratch_database)
        assert inspect(engine).get_table_names() == [], "启动不得建表"
        engine.dispose()
    finally:
        if original is None:
            os.environ.pop("ILCS_DATABASE_URL", None)
        else:
            os.environ["ILCS_DATABASE_URL"] = original
        for name in [n for n in list(sys.modules) if n.startswith("app.")]:
            del sys.modules[name]


def test_migration_reconciliation_report_passes_on_the_seeded_database():
    """AC-01：迁移核对逐项给出结论。"""
    import importlib
    import sys

    for name in [n for n in list(sys.modules) if n.startswith("app.")]:
        del sys.modules[name]
    from app.core.db import SessionLocal
    from app.services.migration_report_service import MigrationReportService

    with SessionLocal() as db:
        report = MigrationReportService(db).reconcile()
    keys = {row["key"] for row in report["lines"]}
    assert keys == {
        "schema", "counts", "org_scope", "sample_link", "balance", "legacy_review",
        "plan_approval", "execution_master_data", "pending",
    }
    rows = {row["key"]: row for row in report["lines"]}
    # 种子库上这些核对项必须全部通过：记录数、组织归属、样本关联、库存对平、
    # 历史结果未被补造审核、锁定方案未被当成已审批。
    invariant = [row for key, row in rows.items() if key != "execution_master_data" and not row["ok"]]
    assert not invariant, invariant

    # 执行前置主数据这一项是「会不会阻塞下发」的预警，按库里当下的行算。整套测试跑下来，
    # 前面的用例会建出 ST-99 这类没有资产档案的工位和没有人员档案的账号，所以这里不断言
    # 它必然通过，只断言它报的是那些行——种子建的工位一个都不该出现在缺口里。
    from app.seed.data import STATIONS

    seeded = {row["id"] for row in STATIONS}
    gap = rows["execution_master_data"]["detail"]
    assert not [station for station in seeded if station in gap], gap
