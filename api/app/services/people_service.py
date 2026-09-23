"""人员档案与资质。

分配时按预计执行时间校验，实际开始与恢复时再校验一次。这两次都在服务端做，
不靠界面上的禁用按钮。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from ..core.clock import now
from ..core.config import settings
from ..core.context import AccessContext
from ..core.errors import NotFound, StateConflict, ValidationFailed
from ..domain.qualification import (
    PersonSpec, QualificationSpec, blockers_for, requirements_for_steps, running_change_policy,
)
from ..models import Person, Qualification, User
from ..repositories.files import FileRepository
from ..repositories.governance import UserRepository
from ..repositories.people import PersonRepository, QualificationRepository
from ..repositories.resources import CapabilityRepository
from .audit_service import AuditService

SCOPE_KINDS = {"capability", "sop", "safety"}
EMPLOYMENT_STATES = {"on_duty", "leave", "left"}
EMPLOYMENT_LABEL = {"on_duty": "在岗", "leave": "休假", "left": "离岗"}


class PeopleService:
    def __init__(self, db: Session, ctx: AccessContext):
        self.db = db
        self.ctx = ctx
        self.people = PersonRepository(db, ctx)
        self.qualifications = QualificationRepository(db, ctx)
        self.users = UserRepository(db)
        self.capabilities = CapabilityRepository(db)
        self.files = FileRepository(db, ctx)
        self.audit = AuditService(db, ctx)

    # ---------- 读 ----------

    def out(self, person: Person, with_qualifications: bool = False) -> dict:
        account = self.users.get(person.user_id) if person.user_id else None
        rows = self.qualifications.for_person(person.id)
        moment = now()
        live = [row for row in rows if self._spec(row).valid_at(moment)]
        soon = now() + timedelta(days=settings.qualification_warn_days)
        payload = {
            "id": person.id,
            "code": person.code,
            "name": person.name,
            "lab_id": person.lab_id,
            "title": person.title,
            "contact": person.contact,
            "employment_state": person.employment_state,
            "employment_label": EMPLOYMENT_LABEL.get(person.employment_state, person.employment_state),
            "user_id": person.user_id,
            "username": account.username if account else "",
            "account_active": bool(account and account.state == "active"),
            "note": person.note,
            "row_version": person.row_version,
            "qualification_count": len(rows),
            "valid_qualification_count": len(live),
            "expiring_count": len(
                [
                    row for row in live
                    if row.expires_at is not None and row.expires_at <= soon
                ]
            ),
            # 只允许绑定有效账号的人员执行系统任务
            "employable": bool(
                person.employment_state == "on_duty" and account and account.state == "active"
            ),
        }
        if with_qualifications:
            payload["qualifications"] = [self.qualification_out(row) for row in rows]
        return payload

    def qualification_out(self, row: Qualification) -> dict:
        moment = now()
        spec = self._spec(row)
        status = "valid"
        if row.revoked_at is not None:
            status = "revoked"
        elif row.expires_at is not None and row.expires_at < moment:
            status = "expired"
        elif row.effective_from > moment:
            status = "pending"
        elif (
            row.expires_at is not None
            and row.expires_at <= moment + timedelta(days=settings.qualification_warn_days)
        ):
            status = "expiring"
        return {
            "id": row.id,
            "person_id": row.person_id,
            "scope_kind": row.scope_kind,
            "scope_ref": row.scope_ref,
            "label": row.label or self._label_for(row.scope_kind, row.scope_ref),
            "evidence_file_id": row.evidence_file_id,
            "granted_by": row.granted_by,
            "effective_from": row.effective_from.isoformat(timespec="seconds"),
            "expires_at": row.expires_at.isoformat(timespec="seconds") if row.expires_at else None,
            "revoked_at": row.revoked_at.isoformat(timespec="seconds") if row.revoked_at else None,
            "revoke_reason": row.revoke_reason,
            "status": status,
            "valid_now": spec.valid_at(moment),
        }

    def _label_for(self, scope_kind: str, scope_ref: str) -> str:
        if scope_kind == "capability":
            return f"设备操作「{self.capabilities.names().get(scope_ref, scope_ref)}」"
        if scope_kind == "sop":
            return f"SOP {scope_ref}"
        return f"安全操作 {scope_ref}"

    @staticmethod
    def _spec(row: Qualification) -> QualificationSpec:
        return QualificationSpec(
            scope_kind=row.scope_kind, scope_ref=row.scope_ref, label=row.label,
            effective_from=row.effective_from, expires_at=row.expires_at,
            revoked_at=row.revoked_at,
        )

    def page(self, offset: int, limit: int, keyword: str = "", state: str | None = None):
        rows, total = self.people.page(offset, limit, keyword, state)
        return [self.out(row) for row in rows], total

    def detail(self, person_id: str) -> dict:
        person = self.people.get(person_id)
        if not person:
            raise NotFound("人员档案不存在")
        payload = self.out(person, with_qualifications=True)
        payload["audit"] = [
            {
                "time": event.time.isoformat(timespec="seconds"), "user": event.user,
                "action": event.action, "before": event.before, "after": event.after,
                "detail": event.detail,
            }
            for event in self.audit.for_target(person.id)
        ]
        return payload

    def expiring(self) -> list[dict]:
        before = now() + timedelta(days=settings.qualification_warn_days)
        rows = []
        for qualification in self.qualifications.expiring(before):
            person = self.people.get(qualification.person_id)
            if not person:
                continue
            rows.append(
                {
                    **self.qualification_out(qualification),
                    "person_name": person.name,
                    "person_code": person.code,
                }
            )
        return rows

    # ---------- 写 ----------

    def create(self, payload: dict, user: User) -> dict:
        code = (payload.get("code") or "").strip()
        if not code:
            raise ValidationFailed("人员编号必填")
        if self.people.by_code(code):
            raise StateConflict(f"人员编号 {code} 已存在")
        account_id = payload.get("user_id") or ""
        if account_id and not self.users.get(account_id):
            raise NotFound("关联账号不存在")
        person = Person(
            code=code, name=payload["name"], lab_id=payload.get("lab_id", ""),
            title=payload.get("title", ""), contact=payload.get("contact", ""),
            employment_state=payload.get("employment_state", "on_duty"),
            user_id=account_id, note=payload.get("note", ""),
        )
        self.people.add(person)
        self.audit.record(
            user, "建立人员档案", person.id, before="—", after=person.name,
            detail=f"{code}；{'已绑定账号' if account_id else '未绑定账号，不能执行系统任务'}",
            object_version=person.row_version,
        )
        self.db.commit()
        return self.out(person)

    def update(self, person_id: str, changes: dict, expected_version: int | None, user: User) -> dict:
        person = self.people.get(person_id)
        if not person:
            raise NotFound("人员档案不存在")
        self.people.check_version(person, expected_version, "人员档案")
        if "employment_state" in changes and changes["employment_state"] not in EMPLOYMENT_STATES:
            raise ValidationFailed("在岗状态取值不合法")
        if changes.get("user_id") and not self.users.get(changes["user_id"]):
            raise NotFound("关联账号不存在")
        before = {key: getattr(person, key) for key in changes}
        for key, value in changes.items():
            setattr(person, key, value)
        person.updated_at = now()
        self.people.bump(person)
        self.audit.record(
            user, "编辑人员档案", person.id, object_version=person.row_version,
            detail="；".join(f"{k}: {before[k]} → {v}" for k, v in changes.items()),
        )
        self.db.commit()
        return self.out(person)

    def grant(self, person_id: str, payload: dict, user: User) -> dict:
        person = self.people.get(person_id)
        if not person:
            raise NotFound("人员档案不存在")
        scope_kind = payload["scope_kind"]
        if scope_kind not in SCOPE_KINDS:
            raise ValidationFailed(f"资质类型只能是 {'、'.join(sorted(SCOPE_KINDS))}")
        if scope_kind == "capability" and not self.capabilities.get(payload["scope_ref"]):
            raise NotFound(f"能力 {payload['scope_ref']} 未登记")
        effective_from = payload.get("effective_from") or now()
        expires_at = payload.get("expires_at")
        if expires_at and expires_at <= effective_from:
            raise ValidationFailed("到期时间必须晚于生效时间")
        evidence_file_id = (payload.get("evidence_file_id") or "").strip()
        evidence = self.files.available(evidence_file_id) if evidence_file_id else None
        if evidence_file_id and evidence is None:
            raise NotFound("资质证明文件不存在、不可用或不在当前组织范围内")
        qualification = Qualification(
            person_id=person.id, scope_kind=scope_kind, scope_ref=payload["scope_ref"],
            label=payload.get("label", "") or self._label_for(scope_kind, payload["scope_ref"]),
            evidence_file_id=evidence_file_id, granted_by=user.id,
            effective_from=effective_from, expires_at=expires_at,
        )
        self.qualifications.add(qualification)
        if evidence is not None:
            evidence.ref_type = "qualification"
            evidence.ref_id = qualification.id
        self.audit.record(
            user, "登记资质", person.id, before="—", after=qualification.label,
            detail=(
                f"{qualification.label}；生效 {effective_from:%Y-%m-%d}；"
                f"到期 {expires_at:%Y-%m-%d}" if expires_at else
                f"{qualification.label}；生效 {effective_from:%Y-%m-%d}；未设到期"
            ),
        )
        self.db.commit()
        return self.qualification_out(qualification)

    def revoke(self, qualification_id: str, reason: str, user: User) -> dict:
        qualification = self.qualifications.get(qualification_id)
        if not qualification:
            raise NotFound("资质记录不存在")
        if qualification.revoked_at is not None:
            raise StateConflict("资质已撤销")
        if not reason.strip():
            raise ValidationFailed("撤销资质必须填写理由")
        qualification.revoked_at = now()
        qualification.revoke_reason = reason
        person = self.people.get(qualification.person_id)
        self.audit.record(
            user, "撤销资质", qualification.person_id, before="有效", after="已撤销",
            detail=f"{qualification.label}；{reason}；影响该人员后续受控操作",
        )
        # 运行中的设备不自动急停，由告警 + 阻止下一受控操作处理
        policy = running_change_policy(has_running_device_step=True)
        self.db.commit()
        return {
            **self.qualification_out(qualification),
            "person_name": person.name if person else "",
            "running_policy": policy,
        }

    # ---------- 资质校验（供其他服务调用） ----------

    def spec_for_user(self, user_id: str) -> PersonSpec | None:
        person = self.people.by_user(user_id)
        if not person:
            return None
        account = self.users.get(person.user_id) if person.user_id else None
        return PersonSpec(
            person_id=person.id,
            name=person.name,
            employment_state=person.employment_state,
            account_active=bool(account and account.state == "active"),
            qualifications=tuple(
                self._spec(row) for row in self.qualifications.for_person(person.id)
            ),
        )

    def blockers_for_steps(
        self, user_id: str, steps: list[dict], moment: datetime | None = None,
        until: datetime | None = None,
    ) -> list[str]:
        """资质判据。给了 `until` 就按整段执行时间判：开始时有效、结束前到期同样不行。

        资质有效期是一个连续区间，所以检查区间两端就覆盖了中间任意时刻。
        """
        requirements = requirements_for_steps(steps, self.capabilities.names())
        if not requirements:
            return []
        spec = self.spec_for_user(user_id)
        start = moment or now()
        reasons = blockers_for(spec, requirements, start)
        if until is not None and until > start:
            reasons += [
                f"{reason}（按计划结束时间 {until.isoformat(timespec='minutes')} 判定）"
                for reason in blockers_for(spec, requirements, until)
                if reason not in reasons
            ]
        return reasons

    def require_for_steps(
        self, user_id: str, steps: list[dict], moment: datetime | None = None, action: str = "执行",
        until: datetime | None = None,
    ) -> None:
        reasons = self.blockers_for_steps(user_id, steps, moment, until)
        if reasons:
            raise StateConflict(
                f"资质校验未通过，不能{action}",
                {"blocked": [{"key": "qualification", "label": reason} for reason in reasons]},
                code="qualification_blocked",
            )

    def requires_qualification(self, steps: list[dict]) -> bool:
        return bool(requirements_for_steps(steps, self.capabilities.names()))
