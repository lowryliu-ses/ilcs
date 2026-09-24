"""设备方法目录。流程的设备步骤引用这里的已发布方法：流程管做什么，方法管怎么做。"""
from fastapi import APIRouter

from ...schemas import DeviceMethodIn, DeviceMethodPatchIn, Versioned
from ...services.method_service import MethodService
from ..deps import Ctx, CurrentUser, DbSession, require

router = APIRouter(prefix="/device-methods", tags=["method"])


@router.get("")
def list_methods(db: DbSession, ctx: Ctx, state: str | None = None, capability_id: str | None = None):
    return MethodService(db, ctx).list(state, capability_id)


@router.get("/{method_id}")
def get_method(method_id: str, db: DbSession, ctx: Ctx):
    return MethodService(db, ctx).get(method_id)


@router.post("", status_code=201)
def create_method(payload: DeviceMethodIn, db: DbSession, user: CurrentUser, ctx=require("method.edit")):
    return MethodService(db, ctx).create(payload.model_dump(), user)


@router.patch("/{method_id}")
def update_method(
    method_id: str, payload: DeviceMethodPatchIn, db: DbSession, user: CurrentUser, ctx=require("method.edit"),
):
    return MethodService(db, ctx).update(method_id, payload.model_dump(exclude_unset=True), user)


@router.post("/{method_id}/release")
def release_method(method_id: str, payload: Versioned, db: DbSession, user: CurrentUser, ctx=require("method.release")):
    """发布。同编号的旧发布版随之退役；起草人不能发布本人起草的方法。"""
    return MethodService(db, ctx).release(method_id, payload.row_version, user)


@router.post("/{method_id}/revise", status_code=201)
def revise_method(method_id: str, db: DbSession, user: CurrentUser, ctx=require("method.edit")):
    return MethodService(db, ctx).revise(method_id, user)


@router.post("/{method_id}/retire")
def retire_method(method_id: str, payload: Versioned, db: DbSession, user: CurrentUser, ctx=require("method.release")):
    return MethodService(db, ctx).retire(method_id, payload.row_version, user)


@router.delete("/{method_id}")
def delete_method(method_id: str, db: DbSession, user: CurrentUser, ctx=require("method.edit")):
    return MethodService(db, ctx).delete(method_id, user)
