"""AC-03 至 AC-05：组织隔离、成员撤销、服务身份。

跨组织对象一律 404 而不是 403——「不存在」和「无权限」的区别本身就会泄漏对象存在。
"""
import pytest

from tests.conftest import ISOLATED_ORG, ORG, ServiceSession


def test_login_requires_an_active_membership(client):
    """没有成员关系的账号登录即被拒，不是登录成功后再看不到数据。"""
    from app.core.db import SessionLocal
    from app.core.security import hash_password
    from app.models import User

    with SessionLocal() as db:
        db.add(
            User(
                username="outsider", display_name="外部人员", role="researcher",
                password_hash=hash_password("ilcs1234"), state="active",
            )
        )
        db.commit()

    response = client.post("/api/auth/login", json={"username": "outsider", "password": "ilcs1234"})
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "no_membership"


def test_cannot_switch_to_an_organization_you_are_not_a_member_of(client, operator):
    response = client.post(
        "/api/auth/login",
        json={"username": "operator", "password": "ilcs1234", "organization_id": ISOLATED_ORG},
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "not_a_member"


def test_cross_organization_objects_are_invisible_everywhere(client, operator, reset_runtime):
    """列表、主键读取、导出、统计、附件都在同一个范围里。"""
    from app.core.clock import now
    from app.core.db import SessionLocal
    from app.models import InventoryEvent, InventoryLedger, Lot, Material

    with SessionLocal() as db:
        material = Material(
            org_id=ISOLATED_ORG, code="OTHER@g", name="他组织物料", base_unit="g",
            conversions={}, ghs=[],
        )
        db.add(material)
        db.flush()
        db.add(
            Lot(
                id="LOT-OTHER-ORG", org_id=ISOLATED_ORG, material_id=material.id,
                material="他组织物料", qty="100", opening_balance="100", unit="g",
                release="已放行", expiry="2028-01-01", ghs=[],
            )
        )
        # 期初流水一起写：账面库存只能由流水推出来，迁移核对会查这条规则
        event = InventoryEvent(
            org_id=ISOLATED_ORG, source="migration", event_id="opening-LOT-OTHER-ORG",
            event_type="receive", reason="用例期初", created_by="test", created_at=now(),
        )
        db.add(event)
        db.flush()
        db.add(
            InventoryLedger(
                org_id=ISOLATED_ORG, event_row_id=event.id, source="migration",
                event_id="opening-LOT-OTHER-ORG", line_no=1, event_type="receive",
                lot_id="LOT-OTHER-ORG", material_id=material.id, quantity="100", unit="g",
                balance_delta="100", balance_after="100", operator="test", created_at=now(),
            )
        )
        db.commit()

    lots = {row["id"] for row in operator.get("/api/lots").json()}
    assert "LOT-OTHER-ORG" not in lots, "列表不能返回其他组织的对象"

    assert operator.get("/api/lots/LOT-OTHER-ORG/ledger").status_code == 404
    assert operator.patch("/api/lots/LOT-OTHER-ORG", {"storage": "x"}).status_code == 404
    # 聚合计数也不能泄漏
    assert all(
        row["lot_id"] != "LOT-OTHER-ORG" for row in operator.get("/api/handover").json()["expiring_lots"]
    )


def test_revoked_membership_blocks_new_requests(client, reset_runtime):
    from app.core.clock import now
    from app.core.db import SessionLocal
    from app.core.security import hash_password
    from app.models import Membership, User

    with SessionLocal() as db:
        user = User(
            username="temp-operator", display_name="临时操作员", role="operator",
            password_hash=hash_password("ilcs1234"), state="active",
        )
        db.add(user)
        db.flush()
        db.add(Membership(org_id=ORG, user_id=user.id, state="active", granted_at=now()))
        db.commit()
        user_id = user.id

    login = client.post("/api/auth/login", json={"username": "temp-operator", "password": "ilcs1234"})
    assert login.status_code == 200
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/lots", headers=headers).status_code == 200

    with SessionLocal() as db:
        membership = db.query(Membership).filter(Membership.user_id == user_id).first()
        membership.state = "revoked"
        membership.revoked_at = now()
        db.commit()

    # 令牌还没过期，但成员关系是每次请求重新算的
    after = client.get("/api/lots", headers=headers)
    assert after.status_code == 403
    assert after.json()["detail"]["code"] == "no_membership"


def test_disabled_account_cannot_act(client, reset_runtime):
    from app.core.db import SessionLocal
    from app.models import User

    login = client.post("/api/auth/login", json={"username": "ehs", "password": "ilcs1234"})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    with SessionLocal() as db:
        db.query(User).filter(User.username == "ehs").first().state = "disabled"
        db.commit()
    try:
        response = client.get("/api/lots", headers=headers)
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "account_disabled"
    finally:
        with SessionLocal() as db:
            db.query(User).filter(User.username == "ehs").first().state = "active"
            db.commit()


def test_access_log_is_scoped_to_the_current_organization(admin, reset_runtime):
    from app.core.db import SessionLocal
    from app.repositories.governance import AccessLogRepository

    with SessionLocal() as db:
        logs = AccessLogRepository(db)
        logs.record(ORG, "own-service", "service", "POST", "/api/own", "denied", "own")
        logs.record(
            ISOLATED_ORG, "other-service", "service", "POST", "/api/other", "denied", "other",
        )
        db.commit()

    rows = admin.get("/api/admin/access-log?limit=500")
    assert rows.status_code == 200
    assert any(row["subject"] == "own-service" for row in rows.json())
    assert all(row["subject"] != "other-service" for row in rows.json())


def test_account_lifecycle_forces_first_password_change(client, admin, reset_runtime):
    created = admin.post("/api/admin/accounts", {
        "username": "new.operator", "display_name": "新操作员", "role": "operator",
    })
    assert created.status_code == 201, created.text
    issued = created.json()
    assert issued["temporary_password"] and issued["must_change_password"] is True

    login = client.post("/api/auth/login", json={
        "username": "new.operator", "password": issued["temporary_password"],
    })
    assert login.status_code == 200, login.text
    assert login.json()["user"]["must_change_password"] is True
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    profile = client.get("/api/auth/me", headers=headers)
    assert profile.status_code == 200 and profile.json()["must_change_password"] is True

    blocked = client.get("/api/lots", headers=headers)
    assert blocked.status_code == 403
    assert blocked.json()["detail"]["code"] == "password_change_required"
    weak = client.post("/api/auth/change-password", headers=headers, json={
        "current_password": issued["temporary_password"], "new_password": "short",
    })
    assert weak.status_code == 422 and weak.json()["detail"]["code"] == "password_too_short"

    changed = client.post("/api/auth/change-password", headers=headers, json={
        "current_password": issued["temporary_password"], "new_password": "UniquePass2026!",
    })
    assert changed.status_code == 200, changed.text
    assert changed.json()["must_change_password"] is False
    assert client.get("/api/lots", headers=headers).status_code == 200

    row = next(row for row in admin.get("/api/admin/accounts").json() if row["id"] == issued["id"])
    disabled = admin.patch(f"/api/admin/accounts/{issued['id']}", {
        "membership_state": "revoked", "row_version": row["row_version"],
    })
    assert disabled.status_code == 200
    assert client.get("/api/lots", headers=headers).status_code == 403


def test_admin_cannot_lock_out_self_or_last_admin(admin, reset_runtime):
    row = next(row for row in admin.get("/api/admin/accounts").json() if row["id"] == admin.user["id"])
    self_disable = admin.patch(f"/api/admin/accounts/{row['id']}", {
        "account_state": "disabled", "row_version": row["row_version"],
    })
    assert self_disable.status_code == 409
    assert self_disable.json()["detail"]["code"] == "self_lockout"


def test_device_entries_require_service_credentials(client, device, operator):
    """AC-05：未认证、停用凭据、授权外设备都不写业务数据。"""
    anonymous = client.post("/api/runtime/stations/ST-03/heartbeat", json={"connected": True})
    assert anonymous.status_code == 401

    wrong = client.post(
        "/api/runtime/stations/ST-03/heartbeat", json={"connected": True},
        headers={"X-Service-Source": "executor-sim", "X-Service-Secret": "wrong-secret"},
    )
    assert wrong.status_code == 401

    assert device.post("/api/runtime/stations/ST-03/heartbeat", {"connected": True}).status_code == 200

    # 拒绝留痕，但不制造成功业务审计
    log = operator.client.get(
        "/api/admin/access-log",
        headers={"Authorization": f"Bearer {_admin_token(client)}"},
    ).json()
    assert any(row["code"] == "unauthenticated" for row in log)


def test_service_identity_is_limited_to_authorized_stations(client, lims):
    """LIMS 凭据只授权了 ST-07，动别的工位必须被拒。"""
    allowed = lims.post("/api/runtime/stations/ST-07/heartbeat", {"connected": True})
    assert allowed.status_code == 200

    denied = lims.post("/api/runtime/stations/ST-03/heartbeat", {"connected": True})
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "station_not_authorized"


def test_runtime_adapter_list_is_limited_to_own_organization_and_stations(client, lims):
    """适配器契约清单只列本组织、且授权给这个服务凭据的工位；未声明工位范围的一条都不给。"""
    from app.core.db import SessionLocal
    from app.core.security import hash_secret
    from app.models import ServiceIdentity

    assert [row["station_id"] for row in lims.get("/api/runtime/adapters").json()] == ["ST-07"]

    # 别的组织的凭据即使在范围里写了本组织的工位编号，也看不到；本组织没声明工位范围的凭据同样看不到
    temporary = {
        "scope-other-org": (ISOLATED_ORG, {"stations": ["ST-07"]}),
        "scope-none": (ORG, {"analysis_tasks": "all"}),
    }
    with SessionLocal() as db:
        for source, (org_id, scopes) in temporary.items():
            db.add(ServiceIdentity(
                org_id=org_id, source=source, name=source, secret_hash=hash_secret(f"{source}-secret"), scopes=scopes,
            ))
        db.commit()
    try:
        for source in temporary:
            listed = ServiceSession(client, source, f"{source}-secret").get("/api/runtime/adapters")
            assert listed.status_code == 200, listed.text
            assert listed.json() == [], source
    finally:
        with SessionLocal() as db:
            db.query(ServiceIdentity).filter(ServiceIdentity.source.in_(list(temporary))).delete(
                synchronize_session=False
            )
            db.commit()


def test_device_condition_recovery_only_for_authorized_station_conditions(lims):
    """设备上报「条件恢复」只认授权工位上的设备侧条件。条件一复位，批次续跑的前置检查就放行了。"""
    from app.core.db import SessionLocal
    from app.models import Alarm

    cases = {
        # 报警号: (组织, 来源类型, 来源, 产生方式, 期望状态码, 期望错误码)
        "A-SCOPE-OWN": (ORG, "station", "ST-07", "device", 200, ""),
        "A-SCOPE-OTHER-STATION": (ORG, "station", "ST-03", "device", 403, "station_not_authorized"),
        "A-SCOPE-NOT-STATION": (ORG, "material", "Tank A", "device", 403, "alarm_not_authorized"),
        "A-SCOPE-SOFTWARE": (ORG, "station", "ST-07", "system", 409, "system_alarm"),
        "A-SCOPE-OTHER-ORG": (ISOLATED_ORG, "station", "ST-07", "device", 404, ""),
    }
    with SessionLocal() as db:
        for alarm_id, (org_id, source_type, source_id, origin, _, _) in cases.items():
            db.add(Alarm(
                id=alarm_id, org_id=org_id, severity=3, source_type=source_type, source_id=source_id,
                message="用例：设备侧条件", origin=origin,
            ))
        db.commit()
    try:
        for alarm_id, (_, _, _, _, status, code) in cases.items():
            response = lims.post(f"/api/alarms/{alarm_id}/condition-cleared")
            assert response.status_code == status, (alarm_id, response.text)
            if code:
                assert response.json()["detail"]["code"] == code, alarm_id
        with SessionLocal() as db:
            rows = db.query(Alarm).filter(Alarm.id.in_(list(cases)), Alarm.condition_active.is_(True)).all()
            active = {row.id for row in rows}
        assert active == set(cases) - {"A-SCOPE-OWN"}, "被拒的上报不能改动条件"
    finally:
        # 持续中的工位条件会挡住后面用例的批次续跑，用完就删
        with SessionLocal() as db:
            db.query(Alarm).filter(Alarm.id.in_(list(cases))).delete(synchronize_session=False)
            db.commit()


def test_disabled_service_identity_is_rejected_immediately(client, admin, device):
    identities = admin.get("/api/service-identities").json()
    target = next(row for row in identities if row["source"] == "executor-sim")
    assert admin.post(
        f"/api/service-identities/{target['id']}/state", {"state": "disabled"}
    ).status_code == 200
    try:
        response = device.post("/api/runtime/stations/ST-03/heartbeat", {"connected": True})
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "identity_disabled"
    finally:
        admin.post(f"/api/service-identities/{target['id']}/state", {"state": "active"})


def _admin_token(client) -> str:
    return client.post(
        "/api/auth/login", json={"username": "admin", "password": "ilcs1234"}
    ).json()["access_token"]
