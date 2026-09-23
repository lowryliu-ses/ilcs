"""账号多角色与组织可编辑的角色权限矩阵：管理员恒有全部权限；职责分离不受矩阵影响。"""
import pytest

TARGET = "role-permissions:ORG-001"


def _put(admin, matrix, version):
    return admin.put("/api/admin/role-permissions", {
        "matrix": matrix, "row_version": version,
        "signature_id": admin.sign("修改角色权限", target=TARGET, object_version=version),
    })


@pytest.fixture()
def matrix_restored(admin, db):
    """用例改过矩阵就删掉那一行，组织回到出厂默认值。"""
    yield
    from app.models import RolePermissionSet

    db.expire_all()
    row = db.get(RolePermissionSet, "ORG-001")
    if row is not None:
        db.delete(row)
        db.commit()


@pytest.fixture()
def roles_restored(db):
    from app.models import User

    original = {user.username: (user.role, list(user.roles or [])) for user in db.query(User).all()}
    yield
    db.expire_all()
    for user in db.query(User).all():
        if user.username in original:
            user.role, user.roles = original[user.username]
    db.commit()


def _account(admin, username):
    return next(row for row in admin.get("/api/admin/accounts").json() if row["username"] == username)


def test_admin_has_every_permission(admin):
    from app.domain.permissions import PERMISSIONS

    assert set(admin.get("/api/auth/me").json()["perms"]) == set(PERMISSIONS)


def test_catalog_covers_every_permission(admin):
    from app.domain.permissions import PERMISSIONS

    body = admin.get("/api/admin/role-permissions").json()
    listed = {row["key"] for group in body["catalog"] for row in group["permissions"]}
    assert listed == set(PERMISSIONS)
    assert {"key": "admin", "name": "系统管理员", "locked": True} in body["roles"]
    assert body["customized"] is False and body["row_version"] == 0
    assert "admin" not in body["matrix"], "系统管理员不在矩阵里"


def test_admin_edits_matrix_and_it_takes_effect(admin, researcher, matrix_restored):
    body = admin.get("/api/admin/role-permissions").json()
    matrix = {role: [p for p in perms if not (role == "researcher" and p == "plan.edit")]
              for role, perms in body["matrix"].items()}
    saved = _put(admin, matrix, body["row_version"])
    assert saved.status_code == 200, saved.text
    assert saved.json()["row_version"] == 1 and saved.json()["customized"] is True

    assert "plan.edit" not in researcher.get("/api/auth/me").json()["perms"]
    denied = researcher.post("/api/plans", {"name": "x", "recipe_id": "R-201"})
    assert denied.status_code == 403, "矩阵改动下一次请求就生效，不用重新登录"

    audit = admin.get(f"/api/audit?target={TARGET}").json()
    assert any(row["action"] == "修改角色权限" and "移除 plan.edit" in row["detail"] for row in audit)

    stale = _put(admin, body["matrix"], 0)
    assert stale.status_code == 409, "拿旧版本覆盖别人刚改的矩阵被拒"


def test_matrix_rejects_admin_row_reserved_and_unknown_permissions(admin, matrix_restored):
    body = admin.get("/api/admin/role-permissions").json()
    for matrix, expected in (
        ({**body["matrix"], "admin": []}, "系统管理员恒有全部权限"),
        ({**body["matrix"], "qa": [*body["matrix"]["qa"], "org.admin"]}, "只能由系统管理员持有"),
        ({**body["matrix"], "operator": ["batch.fly"]}, "未知权限"),
        ({**body["matrix"], "guest": []}, "未知角色"),
    ):
        response = _put(admin, matrix, 0)
        assert response.status_code == 422, response.text
        assert expected in response.text

    unsigned = admin.put("/api/admin/role-permissions", {"matrix": body["matrix"], "row_version": 0, "signature_id": ""})
    assert unsigned.status_code == 400, "改权限必须签名"


def test_only_admin_manages_permissions(qa):
    assert qa.get("/api/admin/role-permissions").status_code == 403


def test_multiple_roles_union_permissions(admin, researcher, roles_restored):
    account = _account(admin, "researcher")
    updated = admin.patch(f"/api/admin/accounts/{account['id']}", {
        "roles": ["researcher", "operator"], "row_version": account["row_version"],
    })
    assert updated.status_code == 200, updated.text
    assert updated.json()["roles"] == ["researcher", "operator"]
    assert updated.json()["role_name"] == "研究员、操作员"
    perms = set(researcher.get("/api/auth/me").json()["perms"])
    assert {"plan.edit", "batch.create", "batch.control"} <= perms


def test_separation_of_duties_survives_multiple_roles(admin, researcher, roles_restored):
    """同一个人挂了研究员 + QA，也不能批准自己写的方法。"""
    account = _account(admin, "researcher")
    admin.patch(f"/api/admin/accounts/{account['id']}", {
        "roles": ["researcher", "qa"], "row_version": account["row_version"],
    })
    draft = researcher.post("/api/recipes", {"name": "职责分离验证", "plate": 24, "copy_from": "R-201"})
    assert draft.status_code == 201, draft.text
    recipe_id = draft.json()["id"]
    assert researcher.post(f"/api/recipes/{recipe_id}/submit").status_code == 200
    denied = researcher.post(f"/api/recipes/{recipe_id}/transition", {
        "target_state": "approved", "signature_id": researcher.sign_recipe("批准方法", recipe_id),
    })
    assert denied.status_code == 403, denied.text


def test_account_roles_are_validated_and_self_lockout_blocked(admin):
    me = _account(admin, "admin")
    empty = admin.patch(f"/api/admin/accounts/{me['id']}", {"roles": [], "row_version": me["row_version"]})
    assert empty.status_code == 422
    demote = admin.patch(f"/api/admin/accounts/{me['id']}", {"roles": ["qa"], "row_version": me["row_version"]})
    assert demote.status_code == 409, "不能改自己当前会话的角色，也不能降级最后一个管理员"
