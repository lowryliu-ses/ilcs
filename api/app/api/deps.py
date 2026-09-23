"""API 层依赖。把 HTTP 关注点（认证头、幂等键、请求版本）翻译成服务层参数。

两条认证路径，权限互不重叠：
- 人类用户走 Bearer JWT，组织由成员关系确定，请求体里的 organization_id 一律不作依据。
- 集成服务走 `X-Service-Source` + `X-Service-Secret`，只能提交授权任务的结果、
  授权设备的心跳与回执，拿不到人类用户的管理权限。
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Annotated, Any

from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..core.context import SERVICE, USER, AccessContext
from ..core.db import ManagedSession, get_db
from ..core.errors import (
    IdempotencyConflict, PermissionDenied, Unauthenticated, ValidationFailed,
)
from ..core.security import decode_token, verify_secret
from ..domain.permissions import action_name, can, permissions_of
from ..models import User
from ..repositories.governance import AccessLogRepository, IdempotencyRepository
from ..repositories.organization import MembershipRepository, ServiceIdentityRepository
from ..services.identity_service import IdentityService

bearer = HTTPBearer(auto_error=False)

DbSession = Annotated[Session, Depends(get_db)]


def request_id(request: Request) -> str:
    existing = request.headers.get("X-Request-Id")
    return existing or uuid.uuid4().hex[:16]


def _deny(db: Session, request: Request, org_id: str, subject: str, kind: str, code: str, reason: str):
    """拒绝也要留痕，但不制造成功业务审计。"""
    AccessLogRepository(db).record(
        org_id=org_id, subject=subject, subject_kind=kind, method=request.method,
        path=request.url.path, code=code, reason=reason, request_id=request_id(request),
    )
    db.commit()


def current_user(
    db: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)] = None,
) -> User:
    if not credentials:
        raise Unauthenticated("缺少访问令牌")
    payload = decode_token(credentials.credentials)
    return IdentityService(db).user_from_token(payload)


CurrentUser = Annotated[User, Depends(current_user)]


def user_context(
    request: Request,
    db: DbSession,
    user: CurrentUser,
    organization: Annotated[str | None, Header(alias="X-Organization-Id")] = None,
) -> AccessContext:
    """用户上下文。

    组织来自令牌里的选择，缺失时回落到唯一有效成员关系——旧令牌没有组织声明，
    这是兼容升级的路径，而不是「谁传谁算」。请求头只能在自己的成员关系里切换。
    """
    if user.state != "active":
        _deny(db, request, "", user.id, USER, "account_disabled", "账号已停用")
        raise PermissionDenied("账号已停用", code="account_disabled")
    if user.must_change_password and request.url.path != "/api/auth/me":
        _deny(
            db, request, "", user.id, USER, "password_change_required",
            "首次登录或口令重置后必须先修改口令",
        )
        raise PermissionDenied(
            "首次登录或口令重置后必须先修改口令",
            code="password_change_required",
        )
    identity = IdentityService(db)
    try:
        return identity.context_for(user, requested_org=organization).with_request_id(
            request_id(request)
        )
    except PermissionDenied as exc:
        _deny(db, request, organization or "", user.id, USER, exc.code, exc.message)
        raise


Ctx = Annotated[AccessContext, Depends(user_context)]


def require(permission: str):
    """动作权限守卫。返回访问上下文，服务层不需要再自己取 user。"""

    def guard(request: Request, db: DbSession, ctx: Ctx) -> AccessContext:
        if not ctx.has(permission):
            _deny(
                db, request, ctx.org_id, ctx.subject_id, ctx.subject_kind, "permission_denied",
                f"角色 {ctx.role} 无权限 {permission}",
            )
            raise PermissionDenied(
                f"当前角色不能{action_name(permission)}（缺少权限 {permission}）",
                {"required_permission": permission},
            )
        return ctx

    return Depends(guard)


def service_context(
    request: Request,
    db: DbSession,
    source: Annotated[str | None, Header(alias="X-Service-Source")] = None,
    secret: Annotated[str | None, Header(alias="X-Service-Secret")] = None,
) -> AccessContext:
    """集成服务上下文。来源由认证确定，仪器序列号只能作业务数据校验，不能当身份。"""
    # 已知来源即使密钥错误，也可以安全地把「拒绝事件」归入它所属组织；
    # 响应仍只说凭据无效，不向调用方泄漏这个来源是否存在。
    identity = ServiceIdentityRepository(db).by_source(source) if source else None
    if not source or not secret:
        _deny(
            db, request, identity.org_id if identity else "", source or "-", SERVICE,
            "unauthenticated", "缺少服务凭据",
        )
        raise Unauthenticated("缺少服务凭据（X-Service-Source / X-Service-Secret）")
    if not identity or not verify_secret(secret, identity.secret_hash):
        # 凭据原文不进日志
        _deny(
            db, request, identity.org_id if identity else "", source, SERVICE,
            "unauthenticated", "服务凭据无效",
        )
        raise Unauthenticated("服务凭据无效")
    if identity.state != "active":
        _deny(db, request, identity.org_id, source, SERVICE, "identity_disabled", "服务凭据已停用")
        raise PermissionDenied("服务凭据已停用", code="identity_disabled")
    ServiceIdentityRepository(db).touch(identity)
    db.commit()
    return AccessContext(
        org_id=identity.org_id,
        subject_id=identity.id,
        subject_kind=SERVICE,
        subject_label=identity.name or identity.source,
        role="device",
        perms=("result.ingest", "station.heartbeat", "command.ack"),
        scopes=identity.scopes or {},
        request_id=request_id(request),
    )


ServiceCtx = Annotated[AccessContext, Depends(service_context)]


def digest_of(payload: Any) -> str:
    """请求内容摘要。同键不同内容要能判出来，所以 key 排序后再摘要。"""
    normalized = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(normalized.encode()).hexdigest()


class Idempotent:
    """写接口的幂等包装。

    作用域 = 组织 + 调用主体 + 动作 + 键值。相同键且内容一致回放首次响应；
    内容不同返回 409，不重复变更。关键写接口缺键直接拒绝——网络重试复用原键，
    不生成新键，否则重试就是第二次业务动作。
    """

    def __init__(self, request: Request, db: Session, key: str | None):
        self.repository = IdempotencyRepository(db)
        self.db = db
        self.key = (key or "").strip()
        self.method = request.method
        self.path = request.url.path
        self.action = f"{request.method} {request.scope.get('route').path if request.scope.get('route') else request.url.path}"
        self.ctx: AccessContext | None = None
        self.digest = ""
        self._advisory_lock_id: int | None = None
        self._advisory_connection = None

    def bind(self, ctx: AccessContext, payload: Any = None) -> "Idempotent":
        self.ctx = ctx
        self.digest = digest_of(payload) if payload is not None else ""
        return self

    def required(self) -> "Idempotent":
        if not self.key:
            raise ValidationFailed(
                "该写操作必须携带 Idempotency-Key；网络重试请复用原键，不要生成新键",
                {"header": "Idempotency-Key"},
                code="idempotency_key_required",
            )
        return self

    def replay(self):
        if not self.key or self.ctx is None:
            return None
        self._acquire_advisory_lock()
        row = self.repository.find(self.ctx.org_id, self.ctx.subject_id, self.action, self.key)
        if row is None:
            if isinstance(self.db, ManagedSession):
                self.db.begin_idempotent()
            return None
        if self.digest and row.request_digest and row.request_digest != self.digest:
            raise IdempotencyConflict(
                "同一 Idempotency-Key 提交了不同内容，已拒绝；换新键或改回原内容",
                {"key": self.key},
            )
        return row.body

    def remember(self, body):
        """记住首次结果。

        领域服务内部的 commit 已由 ManagedSession 收敛为 flush；业务数据、成功审计、
        签名消费与本记录在这里一次提交。PostgreSQL 的会话级 advisory lock 仍从
        replay 前持有到响应结束，负责串行化来自不同进程的同键请求。
        """
        if self.key and self.ctx is not None:
            self.repository.remember(
                org_id=self.ctx.org_id, subject=self.ctx.subject_id, action=self.action,
                key=self.key, method=self.method, path=self.path, digest=self.digest, body=body,
            )
            if isinstance(self.db, ManagedSession):
                self.db.commit_idempotent()
            else:
                self.db.commit()
        return body

    def abort(self) -> None:
        """路由异常或漏调 remember 时回滚整项业务，不能留下半条成功数据。"""
        if isinstance(self.db, ManagedSession):
            self.db.abort_idempotent()

    def _acquire_advisory_lock(self) -> None:
        """串行化同一幂等作用域，覆盖领域服务内部的 commit。"""
        if self._advisory_lock_id is not None:
            return
        bind = self.db.get_bind()
        if self.ctx is None:
            return
        scope = "\x1f".join(
            (self.ctx.org_id, self.ctx.subject_id, self.action, self.key)
        ).encode()
        lock_id = int.from_bytes(hashlib.sha256(scope).digest()[:8], "big", signed=True)
        # 专用连接必须一直保持 checkout。若借用 ORM Session 的连接，领域服务一旦
        # commit，SQLAlchemy 会把连接归还池中；会话级锁可能被别的请求重入，且最终
        # 在另一条连接上 unlock，既失去互斥也会泄漏锁。
        connection = bind.connect()
        try:
            connection.execute(
                text("SELECT pg_advisory_lock(:lock_id)"), {"lock_id": lock_id}
            )
        except Exception:
            connection.close()
            raise
        self._advisory_connection = connection
        self._advisory_lock_id = lock_id

    def release(self) -> None:
        """释放 PostgreSQL 会话锁；异常路径也必须执行，避免锁留在连接池连接上。"""
        if self._advisory_lock_id is None:
            return
        lock_id = self._advisory_lock_id
        connection = self._advisory_connection
        self._advisory_lock_id = None
        self._advisory_connection = None
        try:
            if connection is not None:
                connection.execute(
                    text("SELECT pg_advisory_unlock(:lock_id)"), {"lock_id": lock_id}
                )
                connection.commit()
        finally:
            if connection is not None:
                connection.close()


def idempotency(
    request: Request,
    db: DbSession,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> Any:
    guard = Idempotent(request, db, idempotency_key)
    try:
        yield guard
    finally:
        guard.abort()
        guard.release()


IdempotencyGuard = Annotated[Idempotent, Depends(idempotency)]


class Page:
    """列表分页。默认每页 20、最大 100，筛选状态由前端保留。"""

    def __init__(self, page: int = 1, page_size: int = 20):
        self.page = max(1, page)
        self.page_size = min(100, max(1, page_size))

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size

    def wrap(self, items: list, total: int) -> dict:
        return {"items": items, "total": total, "page": self.page, "page_size": self.page_size}


def pagination(page: int = 1, page_size: int = 20) -> Page:
    return Page(page, page_size)


Paging = Annotated[Page, Depends(pagination)]
