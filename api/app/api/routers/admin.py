"""治理与运行状态。迁移核对、组织成员、访问拒绝日志。"""
from fastapi import APIRouter

from ...core.schema import EXPECTED_REVISION, current_revision
from ...core.errors import ValidationFailed
from ...schemas import AccountCreateIn, AccountPatchIn, RolePermissionsIn
from ...services.identity_service import IdentityService
from ...services.migration_report_service import MigrationReportService
from ..deps import Ctx, CurrentUser, DbSession, require

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/accounts")
def accounts(db: DbSession, ctx=require("org.admin")):
    return IdentityService(db, ctx).list_accounts()


@router.post("/accounts", status_code=201)
def create_account(
    payload: AccountCreateIn, db: DbSession, user: CurrentUser, ctx=require("org.admin"),
):
    roles = payload.roles or ([payload.role] if payload.role else [])
    if not roles:
        raise ValidationFailed("至少选择一个角色", code="role_required")
    return IdentityService(db, ctx).create_account(
        user, payload.username, payload.display_name, roles[0], payload.default_lab_id, roles=roles,
    )


@router.patch("/accounts/{user_id}")
def update_account(
    user_id: str, payload: AccountPatchIn, db: DbSession, user: CurrentUser,
    ctx=require("org.admin"),
):
    changes = payload.model_dump(exclude_unset=True, exclude={"row_version"})
    return IdentityService(db, ctx).update_account(user, user_id, changes, payload.row_version)


@router.get("/role-permissions")
def role_permissions(db: DbSession, ctx=require("org.admin")):
    """当前组织的角色权限矩阵、权限目录与出厂默认值。"""
    return IdentityService(db, ctx).role_permissions()


@router.put("/role-permissions")
def update_role_permissions(
    payload: RolePermissionsIn, db: DbSession, user: CurrentUser, ctx=require("org.admin"),
):
    """修改角色权限矩阵。需要管理员签名；系统管理员恒有全部权限，不在矩阵里。
    职责分离（本人不能审批本人）不受矩阵影响。"""
    return IdentityService(db, ctx).update_role_permissions(
        user, payload.matrix, payload.row_version, payload.signature_id,
    )


@router.post("/accounts/{user_id}/reset-password")
def reset_account_password(
    user_id: str, db: DbSession, user: CurrentUser, ctx=require("org.admin"),
):
    return IdentityService(db, ctx).reset_account_password(user, user_id)


@router.get("/migration-report")
def migration_report(db: DbSession, ctx=require("org.admin")):
    """迁移核对报告。逐项列出记录数、关联完整性、余额与待人工处理项。"""
    return MigrationReportService(db).reconcile()


@router.get("/schema")
def schema_state(db: DbSession, ctx=require("org.admin")):
    from ...core.db import engine

    revision = current_revision(engine)
    return {
        "current": revision,
        "expected": EXPECTED_REVISION,
        "compatible": revision == EXPECTED_REVISION,
    }


@router.get("/organizations")
def organizations(db: DbSession, ctx=require("org.admin")):
    from ...models import Organization
    from ...repositories.organization import MembershipRepository, OrganizationRepository

    memberships = MembershipRepository(db)
    rows = []
    for org in OrganizationRepository(db).list():
        members = memberships.for_org(org.id)
        rows.append(
            {
                "id": org.id, "code": org.code, "name": org.name, "timezone": org.timezone,
                "state": org.state,
                "members": len([m for m in members if m.state == "active"]),
                "revoked": len([m for m in members if m.state != "active"]),
                "current": org.id == ctx.org_id,
            }
        )
    return rows


@router.get("/members")
def members(db: DbSession, ctx=require("person.edit")):
    """本组织的账号清单，供人员档案绑定账号时选择。

    只列当前组织的有效成员，不是全库账号——人员档案是组织内的对象，
    绑到组织外的账号上会做出一个谁都管不着的执行人。
    同时返回该账号是否已被别的档案占用：一个账号只应对应一份人员档案。
    """
    from ...models import Person, User
    from ...repositories.organization import MembershipRepository

    bound = {
        row.user_id: row for row in db.query(Person).filter(Person.org_id == ctx.org_id).all()
        if row.user_id
    }
    rows = []
    for membership in MembershipRepository(db).for_org(ctx.org_id):
        if membership.state != "active":
            continue
        user = db.get(User, membership.user_id)
        if user is None:
            continue
        person = bound.get(user.id)
        rows.append(
            {
                "user_id": user.id,
                "username": user.username,
                "display_name": user.display_name,
                "role": user.role,
                "active": user.state == "active",
                "bound_person_id": person.id if person else "",
                "bound_person": f"{person.code} {person.name}" if person else "",
            }
        )
    return sorted(rows, key=lambda row: row["username"])


@router.get("/access-log")
def access_log(db: DbSession, ctx=require("org.admin"), limit: int = 100):
    """被拒绝的访问与回传。它不制造成功业务审计，但必须能查。"""
    from ...repositories.governance import AccessLogRepository

    return [
        {
            "id": row.id,
            "time": row.time.isoformat(timespec="seconds"),
            "org_id": row.org_id,
            "subject": row.subject,
            "subject_kind": row.subject_kind,
            "method": row.method,
            "path": row.path,
            "outcome": row.outcome,
            "code": row.code,
            "reason": row.reason,
            "request_id": row.request_id,
        }
        for row in AccessLogRepository(db).recent(min(500, max(1, limit)), org_id=ctx.org_id)
    ]
