"""SOP 与受控版本。已发布内容不可原位编辑；修订和恢复历史内容都产生新版本。"""
from __future__ import annotations

from sqlalchemy.orm import Session

from ..core.clock import now
from ..core.context import AccessContext
from ..core.errors import NotFound, PermissionDenied, StateConflict, ValidationFailed
from ..domain import diffs, sop_steps
from ..domain.access import same_person
from ..models import Sop, SopAck, SopVersion, User
from ..repositories.files import FileRepository
from ..repositories.governance import UserRepository
from ..repositories.people import PersonRepository, QualificationRepository
from ..repositories.recipes import RecipeRepository
from ..repositories.sops import SopAckRepository, SopRepository, SopVersionRepository
from .audit_service import AuditService
from .identity_service import IdentityService, admin_self_approval

STATES = ("draft", "review", "published", "retired")
STATE_LABEL = {"draft": "草稿", "review": "评审中", "published": "已发布", "retired": "已退役"}


class SopService:
    def __init__(self, db: Session, ctx: AccessContext):
        self.db = db
        self.ctx = ctx
        self.sops = SopRepository(db, ctx)
        self.versions = SopVersionRepository(db, ctx)
        self.acks = SopAckRepository(db)
        self.files = FileRepository(db, ctx)
        self.people = PersonRepository(db, ctx)
        self.qualifications = QualificationRepository(db, ctx)
        self.recipes = RecipeRepository(db, ctx)
        self.users = UserRepository(db)
        self.audit = AuditService(db, ctx)
        self.identity = IdentityService(db, ctx)

    # ---------- 读 ----------

    def version_out(self, version: SopVersion, detail: bool = False) -> dict:
        sop = self.sops.get(version.sop_id)
        attachment = self.files.get(version.file_id) if version.file_id else None
        author = self.users.get(version.author_id) if version.author_id else None
        approver = self.users.get(version.approver_id) if version.approver_id else None
        payload = {
            "id": version.id,
            "sop_id": version.sop_id,
            "code": sop.code if sop else "",
            "title": sop.title if sop else "",
            "version": version.version,
            "state": version.state,
            "state_label": STATE_LABEL.get(version.state, version.state),
            "file_id": version.file_id,
            "filename": attachment.filename if attachment else "",
            "file_checksum": version.file_checksum,
            "capability_scope": version.capability_scope or [],
            "sample_types": version.sample_types or [],
            "requires_training_ack": version.requires_training_ack,
            "effective_from": version.effective_from.isoformat(timespec="minutes") if version.effective_from else None,
            "author_id": version.author_id,
            "author_name": author.display_name if author else "",
            "approver_id": version.approver_id,
            "approver_name": approver.display_name if approver else "",
            "published_at": version.published_at.isoformat(timespec="seconds") if version.published_at else None,
            "retired_at": version.retired_at.isoformat(timespec="seconds") if version.retired_at else None,
            "reject_reason": version.reject_reason,
            "row_version": version.row_version,
            "editable": version.state == "draft",
            "steps": version.steps or [],
            "restored_from": version.restored_from,
            "ack_count": len(self.acks.for_version(version.id)),
        }
        if detail:
            payload["acks"] = [
                {
                    "person_id": row.person_id,
                    "person_name": (
                        self.people.get(row.person_id).name if self.people.get(row.person_id) else ""
                    ),
                    "acked_at": row.acked_at.isoformat(timespec="seconds"),
                }
                for row in self.acks.for_version(version.id)
            ]
            payload["using_recipes"] = [
                {"id": row.id, "name": row.name, "version": row.version, "state": row.state}
                for row in self.recipes.using_sop_version(version.id)
            ]
            payload["audit"] = [
                {
                    "time": e.time.isoformat(timespec="seconds"), "user": e.user, "action": e.action,
                    "before": e.before, "after": e.after, "detail": e.detail,
                }
                for e in self.audit.for_target(version.id)
            ]
        return payload

    def page(self, offset: int, limit: int, state: str | None = None):
        rows, total = self.versions.page(offset, limit, state)
        return [self.version_out(row) for row in rows], total

    def detail(self, version_id: str) -> dict:
        version = self.versions.get(version_id)
        if not version:
            raise NotFound("SOP 版本不存在")
        return self.version_out(version, detail=True)

    def effective_for_capability(self, capability_id: str) -> list[dict]:
        """当前生效的版本。`capability_id` 为空表示不按能力过滤。

        这里的空值语义要小心：早先的实现把空字符串直接丢进 `in row.capability_scope`，
        于是「不指定能力」变成了「必须适用于空能力」，所有限定了适用范围的 SOP 都被滤掉，
        方法编辑器的下拉框永远是空的。不指定能力就是不筛。
        """
        moment = now()
        rows = [
            row for row in self.versions.published()
            if (
                not capability_id
                or not row.capability_scope
                or capability_id in row.capability_scope
            )
            and row.effective_from is not None
            and row.effective_from <= moment
        ]
        return [self.version_out(row) for row in rows]

    def snapshot_for(self, version_id: str) -> dict:
        """批次固化用的快照：版本号 + 附件摘要。"""
        version = self.versions.get(version_id)
        if version is None:
            return {}
        sop = self.sops.get(version.sop_id)
        return {
            "sop_version_id": version.id,
            "code": sop.code if sop else "",
            "title": sop.title if sop else "",
            "version": version.version,
            "file_id": version.file_id,
            "file_checksum": version.file_checksum,
            "effective_from": version.effective_from.isoformat(timespec="minutes") if version.effective_from else None,
            "frozen_at": now().isoformat(timespec="seconds"),
        }

    # ---------- 写 ----------

    def create_version(self, payload: dict, user: User) -> dict:
        code = (payload.get("code") or "").strip()
        if not code:
            raise ValidationFailed("SOP 编号必填")
        sop = self.sops.by_code(code)
        if sop is None:
            sop = Sop(org_id=self.ctx.org_id, code=code, title=payload["title"])
            self.sops.add(sop)
        existing = self.versions.for_sop(sop.id)
        version_label = (payload.get("version") or "").strip() or f"v{len(existing) + 1}"
        if any(row.version == version_label for row in existing):
            raise StateConflict(f"{code} 的版本 {version_label} 已存在")
        file_id = payload.get("file_id") or ""
        checksum = ""
        attachment = None
        if file_id:
            attachment = self.files.available(file_id)
            if attachment is None:
                raise NotFound("附件不存在或状态不可用")
            checksum = attachment.checksum
        version = SopVersion(
            org_id=self.ctx.org_id, sop_id=sop.id, version=version_label, state="draft",
            file_id=file_id, file_checksum=checksum,
            capability_scope=payload.get("capability_scope") or [],
            sample_types=payload.get("sample_types") or [],
            requires_training_ack=payload.get("requires_training_ack", False),
            effective_from=payload.get("effective_from"), author_id=user.id,
        )
        self.versions.add(version)
        if attachment is not None:
            attachment.ref_type = "sop_version"
            attachment.ref_id = version.id
        self.audit.record(
            user, "新建 SOP 版本", version.id, before="—", after="草稿",
            detail=f"{code} {version_label}；{payload['title']}",
            object_version=version.row_version,
        )
        self.db.commit()
        return self.version_out(version)

    def update_version(self, version_id: str, changes: dict, expected: int | None, user: User) -> dict:
        version = self.versions.get(version_id)
        if not version:
            raise NotFound("SOP 版本不存在")
        if version.state != "draft":
            raise StateConflict(
                f"{STATE_LABEL.get(version.state, version.state)}的版本不可原位编辑，请建立新版本",
                code="sop_not_editable",
            )
        self.versions.check_version(version, expected, "SOP 版本")
        attachment = None
        if changes.get("file_id"):
            attachment = self.files.available(changes["file_id"])
            if attachment is None:
                raise NotFound("附件不存在或状态不可用")
            version.file_checksum = attachment.checksum
        before = {key: getattr(version, key) for key in changes}
        for key, value in changes.items():
            setattr(version, key, value)
        if attachment is not None:
            attachment.ref_type = "sop_version"
            attachment.ref_id = version.id
        self.versions.bump(version)
        self.audit.record(
            user, "编辑 SOP 草稿", version.id, object_version=version.row_version,
            detail="；".join(f"{k}: {before[k]} → {v}" for k, v in changes.items()),
        )
        self.db.commit()
        return self.version_out(version)

    # ---------- 数字 SOP ----------

    def update_steps(self, version_id: str, steps: list[dict], expected: int | None, user: User) -> dict:
        """结构化步骤：人照着做什么。只有草稿能改；步骤里的设备能力与参数按能力字典校验。"""
        from ..repositories.resources import CapabilityRepository

        version = self._require(version_id)
        if version.state != "draft":
            raise StateConflict("只有草稿能改结构化步骤；已发布的请建新版本", code="sop_not_editable")
        self.versions.check_version(version, expected, "SOP 版本")
        problems = sop_steps.issues(steps, CapabilityRepository(self.db, self.ctx).specs())
        if problems:
            raise ValidationFailed("；".join(problems[:5]), code="sop_steps_invalid")
        before = len(version.steps or [])
        version.steps = steps
        self.versions.bump(version)
        self.audit.record(user, "编辑 SOP 结构化步骤", version.id, before=f"{before} 步", after=f"{len(steps)} 步",
                          object_version=version.row_version)
        self.db.commit()
        return self.version_out(version)

    def generate_recipe(self, version_id: str, payload: dict, user: User) -> dict:
        """一键生成方法草稿。已发布的 SOP 版本同时挂到方法上；草稿 SOP 生成的方法不挂（方法只能引用已发布 SOP）。"""
        from .recipe_service import RecipeService

        version = self._require(version_id)
        if not version.steps:
            raise StateConflict("这个 SOP 版本还没有结构化步骤", code="sop_steps_missing")
        sop = self.sops.get(version.sop_id)
        steps = sop_steps.to_recipe_steps(version.steps)
        name = (payload.get("name") or "").strip() or f"{sop.title if sop else 'SOP'} 方法草稿"
        linked = version.id if version.state == "published" else ""
        recipes = RecipeService(self.db, self.ctx)
        recipe = recipes.create_from_steps(
            name, int(payload.get("plate") or 8), steps, user, sop_version_id=linked,
            note=f"由 SOP {sop.code if sop else ''} {version.version} 生成",
        )
        self.audit.record(
            user, "由 SOP 生成方法草稿", version.id, after=recipe.id,
            detail=f"{len(steps)} 步；" + ("已挂接本 SOP 版本" if linked else "SOP 未发布，方法未挂接 SOP"),
        )
        self.db.commit()
        return {"recipe_id": recipe.id, "name": recipe.name, "steps": len(steps), "sop_linked": bool(linked)}

    # ---------- 版本对比与恢复 ----------

    DIFF_LABELS = {
        "filename": "附件", "file_checksum": "附件摘要", "capability_scope": "适用能力", "sample_types": "适用样本类型",
        "requires_training_ack": "要求培训确认", "steps": "结构化步骤",
    }

    def _compare_view(self, version: SopVersion) -> dict:
        attachment = self.files.get(version.file_id) if version.file_id else None
        return {
            "filename": attachment.filename if attachment else "", "file_checksum": version.file_checksum,
            "capability_scope": version.capability_scope or [], "sample_types": version.sample_types or [],
            "requires_training_ack": version.requires_training_ack, "steps": version.steps or [],
        }

    def diff(self, from_id: str, to_id: str) -> dict:
        source, target = self._require(from_id), self._require(to_id)
        if source.sop_id != target.sop_id:
            raise ValidationFailed("只能对比同一个 SOP 的两个版本")
        return {
            "from": source.version, "to": target.version,
            "changes": diffs.diff(self._compare_view(source), self._compare_view(target), self.DIFF_LABELS),
        }

    def restore(self, version_id: str, label: str, user: User) -> dict:
        """从历史版本恢复：产生新的草稿版本，内容（附件、适用范围、结构化步骤）取自历史版本。

        历史版本本身不动；新版本照常评审、发布，发布前旧的已发布版本仍然有效。
        """
        source = self._require(version_id)
        sop = self.sops.get(source.sop_id)
        existing = self.versions.for_sop(source.sop_id)
        if any(row.state in {"draft", "review"} for row in existing):
            raise StateConflict("这个 SOP 已有草稿或评审中的版本，先处理完再恢复", code="sop_draft_exists")
        version_label = (label or "").strip() or f"v{len(existing) + 1}"
        if any(row.version == version_label for row in existing):
            raise StateConflict(f"版本 {version_label} 已存在")
        version = SopVersion(
            org_id=self.ctx.org_id, sop_id=source.sop_id, version=version_label, state="draft",
            file_id=source.file_id, file_checksum=source.file_checksum,
            capability_scope=list(source.capability_scope or []), sample_types=list(source.sample_types or []),
            requires_training_ack=source.requires_training_ack, steps=list(source.steps or []),
            author_id=user.id, restored_from=source.id,
        )
        self.versions.add(version)
        self.audit.record(
            user, "恢复 SOP 历史版本", version.id, before="—", after="草稿",
            detail=f"{sop.code if sop else ''} {version_label} 的内容取自 {source.version}；历史版本不变",
            object_version=version.row_version,
        )
        self.db.commit()
        return self.version_out(version)

    def _require(self, version_id: str) -> SopVersion:
        version = self.versions.get(version_id)
        if not version:
            raise NotFound("SOP 版本不存在")
        return version

    def submit(self, version_id: str, user: User) -> dict:
        version = self.versions.get(version_id)
        if not version:
            raise NotFound("SOP 版本不存在")
        if version.state != "draft":
            raise StateConflict("只有草稿可以提交评审")
        if not version.file_id:
            raise StateConflict(
                "提交评审前必须上传 SOP 附件",
                {"blocked": [{"key": "file", "label": "缺少附件"}]},
            )
        version.state = "review"
        self.versions.bump(version)
        self.audit.record(
            user, "提交 SOP 评审", version.id, before="草稿", after="评审中",
            object_version=version.row_version,
        )
        self.db.commit()
        return self.version_out(version)

    def decide(self, version_id: str, payload: dict, user: User) -> dict:
        """批准或驳回。作者不能批准自己写的版本。"""
        version = self.versions.get(version_id)
        if not version:
            raise NotFound("SOP 版本不存在")
        if version.state != "review":
            raise StateConflict("只有评审中的版本可以批准或驳回")
        conclusion = payload.get("conclusion")
        if conclusion not in {"approved", "rejected"}:
            raise ValidationFailed("结论只能是 approved 或 rejected")
        if same_person(version.author_id, user.id) and not admin_self_approval(
            self.db, self.ctx, user, version.id, "批准本人编写的 SOP 版本",
        ):
            raise PermissionDenied(
                "不能批准本人编写的 SOP 版本（职责分离）", code="self_approval_denied"
            )
        reason = (payload.get("reason") or "").strip()
        if conclusion == "rejected":
            if not reason:
                raise ValidationFailed("驳回必须写明理由")
            version.state = "draft"
            version.reject_reason = reason
            self.versions.bump(version)
            self.audit.record(
                user, "驳回 SOP 版本", version.id, before="评审中", after="草稿", detail=reason,
                object_version=version.row_version,
            )
            self.db.commit()
            return self.version_out(version)

        signature = self.identity.consume_signature(
            payload.get("signature_id"), user, "批准并发布 SOP",
            object_ref=version.id, object_version=version.row_version,
        )
        effective_from = payload.get("effective_from") or version.effective_from or now()
        version.state = "published"
        version.approver_id = user.id
        version.effective_from = effective_from
        version.published_at = now()
        self.versions.bump(version)
        # 新版本生效不自动改在途运行，只显示影响
        impacted = [
            {"id": row.id, "name": row.name, "version": row.version, "state": row.state}
            for row in self.recipes.using_sop_version(version.id)
        ]
        self.audit.record(
            user, "发布 SOP 版本", version.id, sign=True, meaning=signature.meaning,
            signature_id=signature.id, before="评审中", after="已发布",
            object_version=version.row_version,
            detail=(
                f"生效 {effective_from:%Y-%m-%d %H:%M}；"
                f"影响 {len(impacted)} 个方法，在途运行不自动改版"
            ),
        )
        self.db.commit()
        return {**self.version_out(version), "impacted_recipes": impacted}

    def retire(self, version_id: str, reason: str, user: User) -> dict:
        version = self.versions.get(version_id)
        if not version:
            raise NotFound("SOP 版本不存在")
        if version.state != "published":
            raise StateConflict("只有已发布的版本可以退役")
        if not reason.strip():
            raise ValidationFailed("退役必须写明理由")
        version.state = "retired"
        version.retired_at = now()
        self.versions.bump(version)
        impacted = self.recipes.using_sop_version(version.id)
        self.audit.record(
            user, "退役 SOP 版本", version.id, before="已发布", after="已退役",
            object_version=version.row_version,
            detail=(
                f"{reason}；{len(impacted)} 个方法仍引用它，历史引用与附件保持可查；"
                f"新任务需改用有效版本"
            ),
        )
        self.db.commit()
        return {
            **self.version_out(version),
            "impacted_recipes": [
                {"id": row.id, "name": row.name, "version": row.version} for row in impacted
            ],
        }

    def acknowledge(self, version_id: str, user: User) -> dict:
        version = self.versions.get(version_id)
        if not version:
            raise NotFound("SOP 版本不存在")
        if version.state != "published":
            raise StateConflict("只能确认已发布的版本")
        person = self.people.by_user(user.id)
        if person is None:
            raise StateConflict(
                "当前账号没有关联人员档案，无法记录阅读确认", code="person_required"
            )
        existing = self.acks.find(version.id, person.id)
        if existing is not None:
            return {"acked": True, "replayed": True, "acked_at": existing.acked_at.isoformat()}
        self.acks.add(SopAck(sop_version_id=version.id, person_id=person.id, user_id=user.id))
        self.audit.record(
            user, "SOP 阅读确认", version.id,
            detail=f"{person.name} 确认 {version.version}",
        )
        self.db.commit()
        return {"acked": True, "replayed": False}

    # ---------- 供其他服务调用 ----------

    def ack_blockers(self, version_id: str, user_id: str) -> list[str]:
        """SOP 要求培训确认时的阻塞理由。"""
        version = self.versions.get(version_id)
        if version is None or not version.requires_training_ack:
            return []
        person = self.people.by_user(user_id)
        if person is None:
            return ["执行人没有关联人员档案，无法确认 SOP 培训记录"]
        if self.acks.find(version.id, person.id) is not None:
            return []
        # 业务认可的等效资质也算
        equivalent = [
            row for row in self.qualifications.live_for_person(person.id, now())
            if row.scope_kind == "sop" and row.scope_ref == version.id
        ]
        if equivalent:
            return []
        sop = self.sops.get(version.sop_id)
        return [
            f"{person.name} 缺少 {sop.code if sop else ''} {version.version} 的阅读确认或等效资质"
        ]
