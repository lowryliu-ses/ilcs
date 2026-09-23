from __future__ import annotations

from datetime import timedelta
import re

from sqlalchemy.orm import Session

from ..core.clock import now
from ..core.config import settings
from ..core.context import USER, AccessContext
from ..core.errors import (
    DomainError, NotFound, PermissionDenied, SignatureInvalid, StateConflict, Unauthenticated,
    ValidationFailed,
)
from ..core.security import (
    hash_password, hash_secret, issue_token, new_secret, password_needs_rehash, verify_password,
)
from ..domain.permissions import (
    ADMIN, ASSIGNABLE_ROLES, PERMISSION_CATALOG, ROLE_NAMES, default_matrix, effective_permissions,
    normalize_matrix, roles_label,
)
from ..models import ESignature, Membership, Organization, RolePermissionSet, ServiceIdentity, User, roles_of
from ..repositories.governance import SignatureRepository, UserRepository
from ..repositories.organization import (
    MembershipRepository, OrganizationRepository, ProjectMemberRepository, ProjectRepository,
    ServiceIdentityRepository,
)
from .audit_service import AuditService


def user_may(ctx: AccessContext | None, user: User, permission: str) -> bool:
    """这个账号能不能做这个动作：请求上下文就是本人时按它的有效权限（组织矩阵 + 多角色），
    否则按账号全部角色与出厂矩阵判断。"""
    if ctx is not None and ctx.is_user and ctx.subject_id == user.id:
        return ctx.has(permission)
    return permission in effective_permissions(roles_of(user))


class IdentityService:
    def __init__(self, db: Session, ctx: AccessContext | None = None):
        self.db = db
        self.ctx = ctx
        self.users = UserRepository(db)
        self.signatures = SignatureRepository(db)
        self.organizations = OrganizationRepository(db)
        self.memberships = MembershipRepository(db)
        self.project_members = ProjectMemberRepository(db)
        self.services = ServiceIdentityRepository(db)
        self.audit = AuditService(db, ctx)

    # ---------- 登录与上下文 ----------

    def login(self, username: str, password: str, organization_id: str | None = None) -> dict:
        user = self.users.by_username(username)
        if not user or not verify_password(password, user.password_hash):
            raise DomainError("用户名或口令错误", code="bad_credentials")
        if user.state != "active":
            raise PermissionDenied("账号已停用", code="account_disabled")
        memberships = self.memberships.active_for_user(user.id)
        if not memberships:
            raise PermissionDenied(
                "该账号没有任何有效的组织成员关系，请联系管理员", code="no_membership"
            )
        chosen = self._choose(memberships, organization_id)
        ctx = self.context_for(user, requested_org=chosen.org_id)
        if password_needs_rehash(user.password_hash):
            # 兼容升级旧库里的无盐摘要。只有口令已成功验证才迁移，不需要知道明文以外
            # 的历史参数，也不把摘要或口令写入审计。
            user.password_hash = hash_password(password)
            self.db.commit()
        return {
            "access_token": issue_token(user.id, user.role, chosen.org_id),
            "token_type": "bearer",
            "user": self.profile(user, ctx),
            "organizations": self.organizations_of(user),
        }

    def _choose(self, memberships: list[Membership], requested: str | None) -> Membership:
        """登录后只能选自己所属的组织；请求里的组织不在成员关系里就拒绝。"""
        if requested:
            found = next((m for m in memberships if m.org_id == requested), None)
            if not found:
                raise PermissionDenied(
                    f"当前账号不是组织 {requested} 的有效成员", code="not_a_member"
                )
            return found
        return memberships[0]

    def organizations_of(self, user: User) -> list[dict]:
        rows = []
        for membership in self.memberships.active_for_user(user.id):
            org = self.db.get(Organization, membership.org_id)
            if not org or org.state != "active":
                continue
            rows.append(
                {"id": org.id, "code": org.code, "name": org.name, "timezone": org.timezone}
            )
        return rows

    def context_for(self, user: User, requested_org: str | None = None) -> AccessContext:
        memberships = self.memberships.active_for_user(user.id)
        if not memberships:
            raise PermissionDenied("成员关系已撤销，无法访问任何组织数据", code="no_membership")
        membership = self._choose(memberships, requested_org)
        org = self.db.get(Organization, membership.org_id)
        if not org or org.state != "active":
            raise PermissionDenied("组织已停用", code="org_suspended")
        projects = ProjectRepository(self.db)
        restricted = projects.query().filter_by(restricted=True).count() > 0
        return AccessContext(
            org_id=membership.org_id,
            subject_id=user.id,
            subject_kind=USER,
            subject_label=user.display_name,
            role=user.role,
            perms=tuple(effective_permissions(roles_of(user), self.matrix_for(membership.org_id))),
            project_ids=frozenset(self.project_members.project_ids_for(user.id)),
            restricted_projects=restricted,
        )

    def profile(self, user: User, ctx: AccessContext | None = None) -> dict:
        context = ctx or self.ctx
        org = self.db.get(Organization, context.org_id) if context else None
        return {
            "id": user.id,
            "username": user.username,
            "display_name": user.display_name,
            "role": user.role,
            "roles": roles_of(user),
            "role_name": roles_label(roles_of(user)),
            "perms": list(context.perms) if context else [],
            "organization_id": context.org_id if context else "",
            "organization_name": org.name if org else "",
            "organization_timezone": org.timezone if org else "Asia/Shanghai",
            "restricted_projects": bool(context.restricted_projects) if context else False,
            "project_ids": sorted(context.project_ids) if context else [],
            "must_change_password": bool(user.must_change_password),
            "password_changed_at": (
                user.password_changed_at.isoformat(timespec="seconds")
                if user.password_changed_at else None
            ),
        }

    def user_from_token(self, payload: dict) -> User:
        user = self.users.get(payload.get("sub"))
        if not user:
            raise Unauthenticated("用户不存在")
        return user

    @staticmethod
    def _validate_new_password(password: str) -> None:
        if len(password) < 12:
            raise ValidationFailed("新口令至少 12 位", code="password_too_short")
        if password in {"ilcs1234", "password", "123456789012", "admin12345678"}:
            raise ValidationFailed("新口令过于常见，请使用唯一口令", code="password_too_common")
        classes = sum(
            any(check(char) for char in password)
            for check in (str.islower, str.isupper, str.isdigit)
        )
        if classes < 2:
            raise ValidationFailed(
                "新口令至少包含大写字母、小写字母、数字中的两类",
                code="password_too_weak",
            )

    def change_password(self, user: User, current_password: str, new_password: str) -> dict:
        if not verify_password(current_password, user.password_hash):
            raise Unauthenticated("当前口令错误")
        if current_password == new_password:
            raise ValidationFailed("新口令不能与当前口令相同", code="password_unchanged")
        self._validate_new_password(new_password)
        user.password_hash = hash_password(new_password)
        user.must_change_password = False
        user.password_changed_at = now()
        self.users.bump(user)
        self.audit.record(
            user, "修改本人登录口令", user.id, detail="口令摘要已更新；明文未记录",
            object_version=user.row_version,
        )
        self.db.commit()
        return self.profile(user, self.ctx)

    # ---------- 人员账号与成员关系 ----------

    def list_accounts(self) -> list[dict]:
        rows = []
        for membership in self.memberships.for_org(self.ctx.org_id):
            user = self.db.get(User, membership.user_id)
            if user is None:
                continue
            rows.append({
                "id": user.id,
                "username": user.username,
                "display_name": user.display_name,
                "role": user.role,
                "roles": roles_of(user),
                "role_name": roles_label(roles_of(user)),
                "account_state": user.state,
                "membership_state": membership.state,
                "default_lab_id": membership.default_lab_id,
                "must_change_password": bool(user.must_change_password),
                "password_changed_at": (
                    user.password_changed_at.isoformat(timespec="seconds")
                    if user.password_changed_at else None
                ),
                "row_version": user.row_version,
            })
        return sorted(rows, key=lambda row: row["username"])

    def create_account(
        self, actor: User, username: str, display_name: str, role: str, default_lab_id: str,
        roles: list[str] | None = None,
    ) -> dict:
        roles = self._clean_roles(roles or [role])
        username = username.strip().lower()
        if not re.fullmatch(r"[a-z][a-z0-9._-]{2,63}", username):
            raise ValidationFailed(
                "账号须为 3–64 位，以小写字母开头，可包含数字、点、下划线或连字符",
                code="username_invalid",
            )
        if not display_name.strip():
            raise ValidationFailed("姓名不能为空", code="display_name_required")
        if self.users.by_username(username):
            raise StateConflict(f"账号 {username} 已存在", code="username_exists")
        temporary_password = new_secret()
        user = User(
            username=username,
            display_name=display_name.strip(),
            role=roles[0],
            roles=roles,
            password_hash=hash_password(temporary_password),
            state="active",
            must_change_password=True,
        )
        self.users.add(user)
        self.memberships.add(Membership(
            org_id=self.ctx.org_id,
            user_id=user.id,
            state="active",
            default_lab_id=default_lab_id.strip(),
            granted_at=now(),
        ))
        self.audit.record(
            actor, "创建账号", user.id, before="—", after=roles_label(roles),
            detail=f"{username}；临时口令只显示一次；首次登录必须修改",
            object_version=user.row_version,
        )
        self.db.commit()
        return {
            "id": user.id,
            "username": username,
            "temporary_password": temporary_password,
            "must_change_password": True,
        }

    def _scoped_account(self, user_id: str) -> tuple[User, Membership]:
        membership = self.memberships.find(self.ctx.org_id, user_id)
        user = self.db.get(User, user_id)
        if not membership or not user:
            raise NotFound("账号不存在或不在当前组织")
        return user, membership

    def _ensure_admin_remains(
        self, target: User, membership: Membership, changes: dict,
    ) -> None:
        removes_admin = ADMIN in roles_of(target) and (
            ADMIN not in changes.get("roles", roles_of(target))
            or changes.get("account_state", target.state) != "active"
            or changes.get("membership_state", membership.state) != "active"
        )
        if not removes_admin:
            return
        active_admins = 0
        for item in self.memberships.for_org(self.ctx.org_id):
            candidate = self.db.get(User, item.user_id)
            if item.state == "active" and candidate and candidate.state == "active" and ADMIN in roles_of(candidate):
                active_admins += 1
        if active_admins <= 1:
            raise StateConflict(
                "不能停用、撤销或降级当前组织最后一个有效管理员",
                code="last_admin_required",
            )

    def update_account(
        self, actor: User, user_id: str, changes: dict, expected_version: int | None,
    ) -> dict:
        target, membership = self._scoped_account(user_id)
        self.users.check_version(target, expected_version, "账号")
        # 旧客户端只传 role：按「只有这一个角色」处理
        if changes.get("roles") is None and changes.get("role") is not None:
            changes["roles"] = [changes["role"]]
        changes.pop("role", None)
        if "roles" in changes:
            changes["roles"] = self._clean_roles(changes["roles"])
        if actor.id == target.id and (
            changes.get("account_state") == "disabled"
            or changes.get("membership_state") == "revoked"
            or ("roles" in changes and set(changes["roles"]) != set(roles_of(target)))
        ):
            raise StateConflict("不能停用、撤销或修改自己当前会话的角色", code="self_lockout")
        self._ensure_admin_remains(target, membership, changes)
        before = {
            "display_name": target.display_name,
            "roles": roles_of(target),
            "account_state": target.state,
            "membership_state": membership.state,
        }
        if "display_name" in changes:
            if not changes["display_name"].strip():
                raise ValidationFailed("姓名不能为空", code="display_name_required")
            target.display_name = changes["display_name"].strip()
        if "roles" in changes:
            target.role, target.roles = changes["roles"][0], changes["roles"]
        if "account_state" in changes:
            target.state = changes["account_state"]
        if "membership_state" in changes:
            membership.state = changes["membership_state"]
            membership.revoked_at = now() if membership.state == "revoked" else None
        self.users.bump(target)
        self.audit.record(
            actor, "修改账号", target.id,
            detail=f"{before} → {changes}", object_version=target.row_version,
        )
        self.db.commit()
        return next(row for row in self.list_accounts() if row["id"] == target.id)

    @staticmethod
    def _clean_roles(roles: list[str]) -> list[str]:
        cleaned = list(dict.fromkeys(role for role in roles if role))
        if not cleaned:
            raise ValidationFailed("至少选择一个角色", code="role_required")
        unknown = [role for role in cleaned if role not in ASSIGNABLE_ROLES]
        if unknown:
            raise ValidationFailed(f"未知角色 {', '.join(unknown)}", code="role_unknown")
        return cleaned

    # ---------- 角色权限矩阵 ----------

    def matrix_for(self, org_id: str) -> dict[str, list[str]]:
        """组织当前生效的矩阵。没改过的组织、以及矩阵里没有的角色，沿用出厂默认值。"""
        row = self.db.get(RolePermissionSet, org_id)
        return {**default_matrix(), **((row.matrix or {}) if row else {})}

    def role_permissions(self) -> dict:
        row = self.db.get(RolePermissionSet, self.ctx.org_id)
        updater = self.db.get(User, row.updated_by) if row and row.updated_by else None
        return {
            "catalog": [
                {"group": group, "permissions": [{"key": key, "label": label} for key, label in rows]}
                for group, rows in PERMISSION_CATALOG
            ],
            "roles": [
                {"key": role, "name": ROLE_NAMES[role], "locked": role == ADMIN} for role in ASSIGNABLE_ROLES
            ],
            "matrix": self.matrix_for(self.ctx.org_id),
            "defaults": default_matrix(),
            "customized": row is not None,
            "row_version": row.row_version if row else 0,
            "updated_by": updater.display_name if updater else "",
            "updated_at": row.updated_at.isoformat(timespec="seconds") if row else None,
        }

    def update_role_permissions(
        self, actor: User, raw: dict, expected_version: int, signature_id: str,
    ) -> dict:
        """改矩阵：管理员签名、乐观锁、逐角色记录增减。系统管理员角色不可改。"""
        row = self.db.get(RolePermissionSet, self.ctx.org_id)
        current_version = row.row_version if row else 0
        if int(expected_version) != current_version:
            raise StateConflict("权限矩阵已被他人修改，请刷新后重试", code="version_conflict")
        matrix, issues = normalize_matrix(raw)
        if issues:
            raise ValidationFailed("；".join(issues), {"issues": issues}, code="role_permissions_invalid")
        signature = self.consume_signature(
            signature_id, actor, "修改角色权限", object_ref=f"role-permissions:{self.ctx.org_id}",
            object_version=current_version, strict=True,
        )
        before = self.matrix_for(self.ctx.org_id)
        changes = []
        for role, perms in matrix.items():
            added = sorted(set(perms) - set(before.get(role, [])))
            removed = sorted(set(before.get(role, [])) - set(perms))
            if added or removed:
                changes.append(
                    f"{ROLE_NAMES[role]}：" + "；".join(filter(None, [
                        f"新增 {', '.join(added)}" if added else "", f"移除 {', '.join(removed)}" if removed else "",
                    ]))
                )
        if not changes:
            raise ValidationFailed("矩阵没有变化", code="role_permissions_unchanged")
        if row is None:
            row = RolePermissionSet(org_id=self.ctx.org_id, row_version=0)
            self.db.add(row)
        row.matrix = matrix
        row.row_version = current_version + 1
        row.updated_by = actor.id
        row.updated_at = now()
        self.audit.record(
            actor, "修改角色权限", f"role-permissions:{self.ctx.org_id}", sign=True, meaning=signature.meaning,
            signature_id=signature.id, object_version=row.row_version, detail="\n".join(changes)[:4000],
        )
        self.db.commit()
        return self.role_permissions()

    def reset_account_password(self, actor: User, user_id: str) -> dict:
        target, _ = self._scoped_account(user_id)
        temporary_password = new_secret()
        target.password_hash = hash_password(temporary_password)
        target.must_change_password = True
        target.password_changed_at = None
        self.users.bump(target)
        self.audit.record(
            actor, "重置账号口令", target.id,
            detail="临时口令只显示一次；首次登录必须修改；明文未记录",
            object_version=target.row_version,
        )
        self.db.commit()
        return {
            "id": target.id,
            "username": target.username,
            "temporary_password": temporary_password,
            "must_change_password": True,
        }

    # ---------- 电子签名 ----------

    def create_signature(
        self, user: User, password: str, meaning: str, action: str, target: str, note: str,
        object_version: int = 0,
    ) -> dict:
        """签名要求在已认证会话上再验一次口令，返回一次性 ticket。

        票据绑定动作 + 对象 ID + 对象版本：为一个对象签发的票据用在另一个对象上会被拒。
        """
        if not verify_password(password, user.password_hash):
            raise DomainError("签名口令错误", code="bad_signature_password")
        if not meaning.strip():
            raise DomainError("必须选择签名含义", code="signature_meaning_required")
        signature = ESignature(
            user_id=user.id, meaning=meaning.strip(), note=note, action=action,
            object_ref=target or "", object_version=object_version,
        )
        self.signatures.add(signature)
        self.audit.record(
            user, "电子签名", target or action, sign=True, meaning=meaning, detail=note,
            signature_id=signature.id, object_version=object_version,
        )
        self.db.commit()
        return {
            "signature_id": signature.id,
            "meaning": signature.meaning,
            "object_ref": signature.object_ref,
            "object_version": signature.object_version,
            "ttl_sec": settings.sign_ttl_sec,
        }

    def consume_signature(
        self, signature_id: str | None, user: User, action: str,
        object_ref: str = "", object_version: int | None = None, strict: bool = False,
    ) -> ESignature:
        """消费一次性签名。

        `strict=True` 时票据必须明确针对 `object_ref`（以及给出的版本）签发，未写对象的票据
        不再当作通配——用于审批、发布这类「签的就是这个对象」的动作。
        """
        if not signature_id:
            raise SignatureInvalid("此操作需要电子签名")
        signature = self.signatures.get(signature_id)
        if not signature or signature.user_id != user.id:
            raise SignatureInvalid("签名无效")
        if signature.consumed_at:
            raise SignatureInvalid("签名已使用，请重新签署")
        if now() - signature.created_at > timedelta(seconds=settings.sign_ttl_sec):
            raise SignatureInvalid("签名已过期，请重新签署")
        if strict and object_ref and signature.object_ref != object_ref:
            raise SignatureInvalid(
                f"签名必须针对 {object_ref} 签发，当前票据针对 {signature.object_ref or '未指定对象'}"
            )
        if strict and object_version is not None and int(signature.object_version or 0) != int(object_version):
            raise SignatureInvalid(
                f"签名必须针对版本 {object_version} 签发，请刷新后重新签署"
            )
        if object_ref and signature.object_ref and signature.object_ref != object_ref:
            raise SignatureInvalid(
                f"签名是为 {signature.object_ref} 签发的，不能用于 {object_ref}"
            )
        if (
            object_version is not None
            and signature.object_version
            and int(signature.object_version) != int(object_version)
        ):
            raise SignatureInvalid(
                f"签名针对版本 {signature.object_version}，当前对象已是版本 {object_version}，请重新签署"
            )
        signature.consumed_at = now()
        signature.action = action
        return signature

    # ---------- 服务身份 ----------

    @staticmethod
    def _service_scopes(raw: dict | None) -> dict:
        """校验并规范化服务授权范围，拒绝拼错字段造成的假授权。"""
        scopes = raw or {}
        allowed_keys = {"stations", "analysis_tasks", "instrument_serials", "plan_proposals"}
        unknown = sorted(set(scopes) - allowed_keys)
        if unknown:
            raise ValidationFailed(
                f"未知的服务授权字段：{'、'.join(unknown)}",
                {"allowed": sorted(allowed_keys)},
                code="service_scope_invalid",
            )
        normalized: dict = {}
        for key in ("stations", "instrument_serials"):
            value = scopes.get(key)
            if value is None:
                continue
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                raise ValidationFailed(f"{key} 必须是字符串数组", code="service_scope_invalid")
            normalized[key] = sorted({item.strip() for item in value if item.strip()})
        proposals = scopes.get("plan_proposals")
        if proposals is not None:
            # 外部优化器：可向哪些方案提交下一轮提案。all 表示本组织全部已批准方案
            if proposals == "all":
                normalized["plan_proposals"] = "all"
            elif isinstance(proposals, list) and all(isinstance(item, str) for item in proposals):
                normalized["plan_proposals"] = sorted({item.strip() for item in proposals if item.strip()})
            else:
                raise ValidationFailed(
                    "plan_proposals 只能是 all 或方案编号数组", code="service_scope_invalid",
                )
        tasks = scopes.get("analysis_tasks")
        if tasks is not None:
            if tasks == "all":
                normalized["analysis_tasks"] = "all"
            elif isinstance(tasks, list) and all(isinstance(item, str) for item in tasks):
                normalized["analysis_tasks"] = sorted(
                    {item.strip() for item in tasks if item.strip()}
                )
            else:
                raise ValidationFailed(
                    "analysis_tasks 只能是 all 或检测任务编号数组",
                    code="service_scope_invalid",
                )
        return normalized

    def list_service_identities(self) -> list[dict]:
        return [
            {
                "id": row.id, "source": row.source, "name": row.name, "state": row.state,
                "scopes": row.scopes or {},
                "created_at": row.created_at.isoformat(timespec="seconds"),
                "rotated_at": row.rotated_at.isoformat(timespec="seconds") if row.rotated_at else None,
                "last_used_at": row.last_used_at.isoformat(timespec="seconds") if row.last_used_at else None,
                "row_version": row.row_version,
            }
            for row in self.services.for_org(self.ctx.org_id)
        ]

    def create_service_identity(self, user: User, source: str, name: str, scopes: dict) -> dict:
        source = source.strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,63}", source):
            raise ValidationFailed(
                "来源标识须为 3–64 位小写字母、数字、点、下划线或连字符",
                code="service_source_invalid",
            )
        if self.services.by_source(source):
            raise StateConflict(f"来源标识 {source} 已被占用")
        normalized_scopes = self._service_scopes(scopes)
        secret = new_secret()
        identity = ServiceIdentity(
            org_id=self.ctx.org_id, source=source, name=name, secret_hash=hash_secret(secret),
            scopes=normalized_scopes,
        )
        self.services.add(identity)
        self.audit.record(
            user, "签发集成凭据", identity.id, before="—", after="active",
            detail=f"来源 {source}；范围 {normalized_scopes}；凭据原文只显示一次，库内仅存摘要",
        )
        self.db.commit()
        # 原文只在这一次响应里出现
        return {
            "id": identity.id, "source": source, "secret": secret,
            "scopes": identity.scopes, "row_version": identity.row_version,
        }

    def update_service_identity(
        self, user: User, identity_id: str, changes: dict, expected_version: int | None,
    ) -> dict:
        identity = self.services.get(identity_id)
        if not identity or identity.org_id != self.ctx.org_id:
            raise NotFound("集成凭据不存在")
        self.services.check_version(identity, expected_version, "集成凭据")
        if not changes:
            raise StateConflict("没有需要保存的变更")
        if "scopes" in changes:
            changes["scopes"] = self._service_scopes(changes["scopes"])
        before = {key: getattr(identity, key) for key in changes}
        for key, value in changes.items():
            setattr(identity, key, value)
        self.services.bump(identity)
        self.audit.record(
            user, "修改集成凭据范围", identity.id,
            detail="；".join(f"{key}: {before[key]} → {value}" for key, value in changes.items()),
            object_version=identity.row_version,
        )
        self.db.commit()
        return next(row for row in self.list_service_identities() if row["id"] == identity.id)

    def rotate_service_identity(self, user: User, identity_id: str) -> dict:
        identity = self.services.get(identity_id)
        if not identity or identity.org_id != self.ctx.org_id:
            raise NotFound("集成凭据不存在")
        secret = new_secret()
        identity.secret_hash = hash_secret(secret)
        identity.rotated_at = now()
        self.services.bump(identity)
        self.audit.record(user, "轮换集成凭据", identity.id, detail=f"来源 {identity.source}")
        self.db.commit()
        return {
            "id": identity.id, "source": identity.source, "secret": secret,
            "row_version": identity.row_version,
        }

    def set_service_identity_state(self, user: User, identity_id: str, state: str) -> dict:
        identity = self.services.get(identity_id)
        if not identity or identity.org_id != self.ctx.org_id:
            raise NotFound("集成凭据不存在")
        if state not in {"active", "disabled"}:
            raise StateConflict("状态只能是 active 或 disabled")
        before = identity.state
        identity.state = state
        self.services.bump(identity)
        self.audit.record(
            user, "变更集成凭据状态", identity.id, before=before, after=state,
            detail="停用立即影响新请求",
        )
        self.db.commit()
        return {"id": identity.id, "state": identity.state, "row_version": identity.row_version}

    @staticmethod
    def initial_password_hash(raw: str) -> str:
        return hash_password(raw)
