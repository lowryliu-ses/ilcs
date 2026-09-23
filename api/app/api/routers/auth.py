from fastapi import APIRouter

from ...schemas import (
    LoginIn, PasswordChangeIn, ServiceIdentityIn, ServiceIdentityPatchIn,
    ServiceIdentityStateIn, SignatureIn,
)
from ...services.identity_service import IdentityService
from ..deps import Ctx, CurrentUser, DbSession, require

router = APIRouter(tags=["identity"])


@router.post("/auth/login")
def login(payload: LoginIn, db: DbSession):
    """登录。只能选自己所属的组织；没有有效成员关系一律拒绝。"""
    return IdentityService(db).login(payload.username, payload.password, payload.organization_id)


@router.get("/auth/me")
def me(db: DbSession, user: CurrentUser, ctx: Ctx):
    return IdentityService(db, ctx).profile(user, ctx)


@router.get("/auth/organizations")
def organizations(db: DbSession, user: CurrentUser):
    return IdentityService(db).organizations_of(user)


@router.post("/auth/change-password")
def change_password(payload: PasswordChangeIn, db: DbSession, user: CurrentUser):
    """首次登录也可调用：这里只依赖已验证令牌，不经过必须改密的业务上下文。"""
    identity = IdentityService(db)
    ctx = identity.context_for(user)
    return IdentityService(db, ctx).change_password(
        user, payload.current_password, payload.new_password,
    )


@router.post("/signatures")
def create_signature(payload: SignatureIn, db: DbSession, user: CurrentUser, ctx: Ctx):
    """签发一次性签名票据，绑定动作 + 对象 ID + 对象版本。"""
    return IdentityService(db, ctx).create_signature(
        user, payload.password, payload.meaning, payload.action, payload.target, payload.note,
        payload.object_version,
    )


@router.get("/service-identities")
def list_service_identities(db: DbSession, ctx=require("service.manage")):
    return IdentityService(db, ctx).list_service_identities()


@router.post("/service-identities", status_code=201)
def create_service_identity(
    payload: ServiceIdentityIn, db: DbSession, user: CurrentUser, ctx=require("service.manage")
):
    """签发集成凭据。原文只在这一次响应里出现，库内只存摘要。"""
    return IdentityService(db, ctx).create_service_identity(
        user, payload.source, payload.name, payload.scopes
    )


@router.post("/service-identities/{identity_id}/rotate")
def rotate_service_identity(
    identity_id: str, db: DbSession, user: CurrentUser, ctx=require("service.manage")
):
    return IdentityService(db, ctx).rotate_service_identity(user, identity_id)


@router.patch("/service-identities/{identity_id}")
def update_service_identity(
    identity_id: str, payload: ServiceIdentityPatchIn, db: DbSession, user: CurrentUser,
    ctx=require("service.manage"),
):
    changes = payload.model_dump(exclude_unset=True, exclude={"row_version"})
    return IdentityService(db, ctx).update_service_identity(
        user, identity_id, changes, payload.row_version,
    )


@router.post("/service-identities/{identity_id}/state")
def set_service_identity_state(
    identity_id: str, payload: ServiceIdentityStateIn, db: DbSession, user: CurrentUser,
    ctx=require("service.manage"),
):
    return IdentityService(db, ctx).set_service_identity_state(user, identity_id, payload.state)
