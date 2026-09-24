"""出厂角色补齐（自动化工程师、实验室经理、审计员）、审计读权限、审计表数据库级只追加。"""
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests.conftest import Session


@pytest.fixture()
def engineer(client):
    return Session(client, "engineer")


@pytest.fixture()
def manager(client):
    return Session(client, "manager")


@pytest.fixture()
def auditor(client):
    return Session(client, "auditor")


def test_auditor_reads_everything_and_changes_nothing(auditor, researcher):
    assert auditor.get("/api/audit").status_code == 200
    assert auditor.get("/api/audit?paged=true&action=新建实验方案").status_code == 200
    assert auditor.post("/api/plans", {"name": "x", "recipe_id": "R-205"}).status_code == 403
    assert auditor.post("/api/device-methods", {"name": "x", "capability_id": "cap.mix"}).status_code == 403
    assert researcher.get("/api/audit").status_code == 403, "研究员不浏览全量审计日志"
    assert researcher.get("/api/audit?target=R-205").status_code == 200, "对象自己的历史照常可看"


def test_engineer_maintains_devices_and_methods_but_not_plans(engineer):
    me = engineer.get("/api/auth/me").json()
    assert "station.edit" in me["perms"] and "method.edit" in me["perms"] and "integration.manage" in me["perms"]
    assert "plan.approve" not in me["perms"] and "recipe.approve" not in me["perms"]
    created = engineer.post("/api/device-methods", {"name": "工程师起草", "capability_id": "cap.mix"})
    assert created.status_code == 201, created.text


def test_lab_manager_can_approve_plans_and_assign_work(manager):
    perms = set(manager.get("/api/auth/me").json()["perms"])
    assert {"plan.approve", "task.assign", "batch.schedule", "person.edit", "audit.read"} <= perms
    assert "station.edit" not in perms and "org.admin" not in perms


def test_audit_rows_cannot_be_updated_or_deleted_even_with_sql(db):
    from app.models import AuditEvent

    row = db.query(AuditEvent).order_by(AuditEvent.id.desc()).first()
    assert row is not None
    with pytest.raises(DBAPIError) as updated:
        db.execute(text("UPDATE audit_events SET detail = '改' WHERE id = :id"), {"id": row.id})
        db.flush()
    db.rollback()
    assert "只追加" in str(updated.value)
    with pytest.raises(DBAPIError):
        db.execute(text("DELETE FROM audit_events WHERE id = :id"), {"id": row.id})
    db.rollback()
    assert db.get(AuditEvent, row.id) is not None
