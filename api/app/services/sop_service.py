"""SOP 与受控版本。已发布内容不可原位编辑；修订和恢复历史内容都产生新版本。"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from ..core.clock import as_utc, now
from ..core.context import AccessContext
from ..core.errors import NotFound, PermissionDenied, StateConflict, ValidationFailed
from ..domain import diffs, sop_steps
from ..domain.access import same_person
from ..models import Sop, SopAck, SopVersion, User
from ..repositories.files import FileRepository
from ..repositories.batches import TERMINAL_STATES, BatchRepository, SampleRepository
from ..repositories.governance import UserRepository
from ..repositories.people import PersonRepository, QualificationRepository
from ..repositories.recipes import RecipeRepository
from ..repositories.samples import PhysicalSampleRepository
from ..repositories.sops import SopAckRepository, SopRepository, SopVersionRepository, is_effective
from .audit_service import AuditService
from .identity_service import IdentityService, admin_self_approval, user_may

STATES = ("draft", "review", "published", "retired")
STATE_LABEL = {"draft": "草稿", "review": "评审中", "published": "已发布", "retired": "已退役"}
# 已发布版本按时间再细分：生效中 / 待生效 / 已被取代 / 已失效。state 不变，只是展示与判定
# 批准时生效时间可以比现在早这么多（审批耗时、时钟误差），再早就算回溯生效
BACKDATE_TOLERANCE = timedelta(minutes=5)

STATUS_LABEL = {
    "draft": "草稿", "review": "评审中", "retired": "已退役",
    "effective": "生效中", "pending": "待生效", "superseded": "已被取代", "expired": "已失效",
}


def status_of(version: SopVersion, at: datetime) -> str:
    if version.state != "published":
        return version.state
    if version.effective_from is None or version.effective_from > at:
        return "pending"
    if version.effective_to is not None and version.effective_to <= at:
        return "superseded" if version.superseded_by else "expired"
    return "effective"


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
        self.batches = BatchRepository(db, ctx)
        self.users = UserRepository(db)
        self.audit = AuditService(db, ctx)
        self.identity = IdentityService(db, ctx)

    # ---------- 读 ----------

    def version_out(self, version: SopVersion, detail: bool = False) -> dict:
        sop = self.sops.get(version.sop_id)
        attachment = self.files.get(version.file_id) if version.file_id else None
        author = self.users.get(version.author_id) if version.author_id else None
        approver = self.users.get(version.approver_id) if version.approver_id else None
        owner = self.users.get(sop.owner_id) if sop and sop.owner_id else None
        successor = self.versions.get(version.superseded_by) if version.superseded_by else None
        moment = now()
        status = status_of(version, moment)
        payload = {
            "id": version.id,
            "sop_id": version.sop_id,
            "code": sop.code if sop else "",
            "title": sop.title if sop else "",
            "version": version.version,
            "state": version.state,
            "state_label": STATE_LABEL.get(version.state, version.state),
            "status": status,
            "status_label": STATUS_LABEL.get(status, status),
            "effective": status == "effective",
            "category": sop.category if sop else "",
            "owner_id": sop.owner_id if sop else "",
            "owner_name": owner.display_name if owner else "",
            "file_id": version.file_id,
            "filename": attachment.filename if attachment else "",
            "file_checksum": version.file_checksum,
            "capability_scope": version.capability_scope or [],
            "sample_types": version.sample_types or [],
            "requires_training_ack": version.requires_training_ack,
            "effective_from": version.effective_from.isoformat(timespec="minutes") if version.effective_from else None,
            "effective_to": version.effective_to.isoformat(timespec="minutes") if version.effective_to else None,
            "review_due": version.review_due.isoformat() if version.review_due else None,
            "review_overdue": bool(
                version.review_due and version.state == "published" and version.review_due < moment.date()
            ),
            "superseded_by": version.superseded_by,
            "superseded_by_version": successor.version if successor else "",
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
            payload["active_batches"] = self._active_batches({version.id})
            payload["audit"] = [
                {
                    "time": e.time.isoformat(timespec="seconds"), "user": e.user, "action": e.action,
                    "before": e.before, "after": e.after, "detail": e.detail,
                }
                for e in self.audit.for_target(version.id)
            ]
        return payload

    def page(self, offset: int, limit: int, state: str | None = None, category: str | None = None):
        rows, total = self.versions.page(offset, limit, state, category)
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
            and is_effective(row, moment)
        ]
        return [self.version_out(row) for row in rows]

    def snapshot_for(self, version_id: str, *, linked_version_id: str = "") -> dict:
        """批次固化用的快照：版本号、附件摘要、适用范围与结构化步骤。

        执行人在批次页照着它干活，所以步骤与说明要跟着冻结——SOP 之后再修订，
        这个批次看到的仍是开批时的那一版。`linked_version_id` 是流程当时关联的版本，
        与实际采用的版本不同时（流程关联的旧版已被取代）一并记下，追溯时看得出为什么换了版。
        """
        version = self.versions.get(version_id)
        if version is None:
            return {}
        sop = self.sops.get(version.sop_id)
        attachment = self.files.get(version.file_id) if version.file_id else None
        snapshot = {
            "sop_version_id": version.id,
            "sop_id": version.sop_id,
            "code": sop.code if sop else "",
            "title": sop.title if sop else "",
            "version": version.version,
            "file_id": version.file_id,
            "filename": attachment.filename if attachment else "",
            "file_checksum": version.file_checksum,
            "capability_scope": list(version.capability_scope or []),
            "sample_types": list(version.sample_types or []),
            "requires_training_ack": version.requires_training_ack,
            "steps": list(version.steps or []),
            "effective_from": version.effective_from.isoformat(timespec="minutes") if version.effective_from else None,
            "frozen_at": now().isoformat(timespec="seconds"),
        }
        if linked_version_id and linked_version_id != version.id:
            linked = self.versions.get(linked_version_id)
            snapshot["linked_version_id"] = linked_version_id
            snapshot["linked_version"] = linked.version if linked else ""
            snapshot["linked_steps"] = sop_steps.compact(linked.steps if linked else [])
        return snapshot

    def mapping_snapshot(self, linked_version_id: str) -> dict:
        """新批次会采用的版本（流程关联版本所属 SOP 的当前生效版本），按批次快照的形状给出，供解析映射。"""
        linked = self.versions.get(linked_version_id)
        if linked is None:
            return {}
        moment = now()
        current = linked if is_effective(linked, moment) else self.versions.effective(linked.sop_id, moment)
        if current is None:
            return {}
        snapshot = {"steps": list(current.steps or []), "version": current.version}
        if current.id != linked.id:
            snapshot["linked_version_id"] = linked.id
            snapshot["linked_steps"] = sop_steps.compact(linked.steps)
        return snapshot

    def resolve_for_new_batch(self, linked_version_id: str) -> SopVersion:
        """新批次按哪一版 SOP 执行：流程关联版本所属 SOP 的当前生效版本。

        DEV-13.2「新任务使用生效且可用版本」。流程关联的版本本身生效就用它；它已被同编号新版本
        取代，就用新版本（新版本要求阅读确认的，执行人得重新确认）；这个 SOP 已没有任何生效版本
        （全部退役、失效或尚未生效），不建批次。
        """
        linked = self.versions.get(linked_version_id)
        if linked is None:
            raise StateConflict("流程关联的 SOP 版本不存在", code="sop_not_effective")
        moment = now()
        if is_effective(linked, moment):
            return linked
        current = self.versions.effective(linked.sop_id, moment)
        if current is None:
            sop = self.sops.get(linked.sop_id)
            label = f"{sop.code if sop else ''} {linked.version}"
            status = STATUS_LABEL.get(status_of(linked, moment), linked.state)
            raise StateConflict(
                f"流程关联的 SOP {label}（{status}）没有可用的生效版本，不能建新批次；"
                f"请发布新版本或在流程里改关联",
                {"blocked": [{"key": "sop", "label": f"SOP {label} {status}，同编号无生效版本"}]},
                code="sop_not_effective",
            )
        return current

    def run_checks(self, snapshot: dict, sample_types: list[str], steps: list[dict] | None = None) -> dict:
        """开跑检查里的 SOP 一项：固化版本现在的状态、复审日期、样本类型与设备能力是否在适用范围内。

        固化版本之后被取代、退役或失效：在途批次不自动改版（DEV-13.3），只提醒，由负责人决定继续还是
        新建批次。样本类型与设备能力不符是硬条件：SOP 写明了只适用于哪些样本、哪些能力，就不能拿别的
        样本、别的设备动作照着做。`steps` 是这一版 SOP 管的批次步骤（快照里展开后的）。
        """
        version = self.versions.get(snapshot.get("sop_version_id", ""))
        label = f"SOP {snapshot.get('code', '')} {snapshot.get('version', '')}"
        if snapshot.get("linked_version"):
            label += f"（流程关联 {snapshot['linked_version']}，开批时已被取代，按此版执行）"
        warnings: list[str] = []
        blockers: list[str] = []
        moment = now()
        if version is None:
            warnings.append("固化的 SOP 版本已找不到")
        else:
            status = status_of(version, moment)
            if status != "effective":
                successor = ""
                if version.superseded_by:
                    newer = self.versions.get(version.superseded_by)
                    successor = f"，新版本 {newer.version}" if newer else ""
                warnings.append(
                    f"固化版本现已{STATUS_LABEL.get(status, status)}{successor}；在途批次不自动改版，"
                    f"由负责人决定继续，或终止后按新版本新建批次"
                )
            if version.review_due and version.review_due < moment.date():
                warnings.append(f"已过复审日期 {version.review_due.isoformat()}")
        allowed = [str(item) for item in snapshot.get("sample_types") or []]
        if allowed:
            wrong = sorted({kind for kind in sample_types if kind and kind not in allowed})
            if wrong:
                blockers.append(f"样本类型 {'、'.join(wrong)} 不在 SOP 适用范围（{'、'.join(allowed)}）内")
            missing = len([kind for kind in sample_types if not kind])
            if missing:
                # 没登记类型不等于类型相符：说清楚没核对，而不是报「在适用范围内」
                warnings.append(
                    f"{missing} 个样本没有登记样本类型，无法核对是否在 SOP 适用范围（{'、'.join(allowed)}）内"
                )
        if steps is not None:
            broken = sop_steps.mapping_issues(steps, snapshot)
            if broken:
                warnings.append(
                    f"节点 {'、'.join(broken[:5])} 对应的 SOP 步骤在 {snapshot.get('version', '')} 里对不上"
                    f"（映射失效），执行时按 SOP 原文核对"
                )
            outside = sop_steps.scope_outside(steps, snapshot.get("capability_scope"))
            if outside:
                blockers.append(
                    f"设备能力 {'、'.join(outside)} 不在 SOP {snapshot.get('code', '')} {snapshot.get('version', '')} "
                    f"的适用范围（{'、'.join(snapshot.get('capability_scope') or [])}）内"
                )
        return {"label": label, "warnings": warnings, "blockers": blockers}

    # ---------- 批次采用的全部 SOP ----------

    @staticmethod
    def batch_snapshots(batch) -> list[tuple[str, dict, str | None]]:
        """批次实际采用的 SOP：主流程的一份，加上子流程各自的（建批次时解析并固化在子流程归属上）。

        返回 (标签前缀, 固化快照, 子流程节点)；主流程的子流程节点为空。
        """
        rows: list[tuple[str, dict, str | None]] = []
        main = batch.sop_snapshot or {}
        if main.get("sop_version_id"):
            rows.append(("", main, None))
        for group in (batch.recipe_snapshot or {}).get("subflows") or []:
            snapshot = group.get("sop") or {}
            if snapshot.get("sop_version_id"):
                rows.append((f"子流程「{group.get('name') or group.get('step_id')}」", snapshot, str(group["step_id"])))
        return rows

    def batch_checks(self, batch, sample_types: list[str]) -> dict | None:
        """开跑检查的 SOP 一项：主流程与各子流程的 SOP 一起判，每一版只管它自己的步骤。"""
        from ..domain.steps import normalize

        rows = self.batch_snapshots(batch)
        if not rows:
            return None
        steps = normalize((batch.recipe_snapshot or {}).get("steps") or [])
        sop_groups = {group for _, _, group in rows if group}
        labels, warnings, blockers = [], [], []
        for prefix, snapshot, group in rows:
            result = self.run_checks(snapshot, sample_types, sop_steps.governed_steps(steps, group, sop_groups))
            labels.append(f"{prefix}{result['label']}")
            warnings.extend(f"{prefix}{text}" for text in result["warnings"])
            blockers.extend(f"{prefix}{text}" for text in result["blockers"])
        return {"label": "；".join(labels), "warnings": warnings, "blockers": blockers}

    def batch_ack_blockers(self, batch, user_id: str) -> list[str] | None:
        """批次采用的 SOP 里要求阅读确认的版本，这个人还缺哪些确认。没有任何一版要求确认时返回 None。

        按批次固化的版本判，不按「当前生效版本」：在途批次不自动改版，执行人确认的必须是批次照着做的那一版。
        """
        required = False
        blockers: list[str] = []
        for prefix, snapshot, _ in self.batch_snapshots(batch):
            version = self.versions.get(snapshot.get("sop_version_id", ""))
            if version is None or not version.requires_training_ack:
                continue
            required = True
            blockers.extend(f"{prefix}{text}" for text in self.ack_blockers(version.id, user_id))
        return blockers if required else None

    def acknowledge_for_batch(self, batch, user: User) -> dict:
        """确认批次采用的 SOP 版本：在途批次按旧版固化、旧版之后被取代时，执行人仍能确认它照着做的那一版。

        通用的阅读确认只接受当前生效的版本；这里的对象限定为这个批次固化的版本（主流程与子流程），
        审计写明批次号。只能确认已发布过的版本（生效中、已被取代、已失效或已退役），草稿不行。
        """
        if batch.state in {"done", "aborted"}:
            raise StateConflict("批次已结束，不需要再确认 SOP")
        person = self.people.by_user(user.id)
        if person is None:
            raise StateConflict("当前账号没有关联人员档案，无法记录阅读确认", code="person_required")
        acked: list[str] = []
        for _, snapshot, _ in self.batch_snapshots(batch):
            version = self.versions.get(snapshot.get("sop_version_id", ""))
            if version is None or not version.requires_training_ack:
                continue
            if version.state not in {"published", "retired"}:
                raise StateConflict(f"{snapshot.get('code', '')} {version.version} 不是已发布的版本")
            if self.acks.find(version.id, person.id) is not None:
                continue
            self.acks.add(SopAck(sop_version_id=version.id, person_id=person.id, user_id=user.id))
            sop = self.sops.get(version.sop_id)
            label = f"{sop.code if sop else ''} {version.version}"
            self.audit.record(
                user, "SOP 阅读确认", version.id,
                detail=f"{person.name} 确认 {label}（批次 {batch.id} 按此版执行，状态：{STATUS_LABEL.get(status_of(version, now()), version.state)}）",
            )
            acked.append(label)
        self.db.commit()
        return {"acked": acked, "blockers": self.batch_ack_blockers(batch, user.id) or []}

    def batch_sample_types(self, batch_id: str) -> list[str]:
        samples = SampleRepository(self.db, self.ctx).for_batch(batch_id)
        physical = PhysicalSampleRepository(self.db, self.ctx)
        kinds: list[str] = []
        for row in samples:
            entity = physical.get(row.physical_sample_id) if row.physical_sample_id else None
            # 没有物理样本或没登记类型的记空串：开跑检查据此提醒「无法核对」，不当成类型相符
            kinds.append(entity.sample_type if entity is not None and entity.sample_type else "")
        return kinds

    def _active_batches(self, version_ids: set[str]) -> list[dict]:
        """在途（未完成、未终止）且固化了这些 SOP 版本的批次。"""
        if not version_ids:
            return []
        return [
            {"id": row.id, "state": row.state, "sop_version": (row.sop_snapshot or {}).get("version", "")}
            for row in self.batches.active()
            if (row.sop_snapshot or {}).get("sop_version_id") in version_ids
        ]

    def impact_of(self, version: SopVersion) -> dict:
        """同编号其他版本的引用面：还关联旧版本的流程、按旧版本在途的批次。"""
        labels = {row.id: row.version for row in self.versions.for_sop(version.sop_id) if row.id != version.id}
        others = set(labels)
        recipes = [
            {"id": row.id, "name": row.name, "version": row.version, "state": row.state,
             "sop_version": labels[vid]}
            for vid in sorted(others) for row in self.recipes.using_sop_version(vid)
        ]
        return {"impacted_recipes": recipes, "impacted_batches": self._active_batches(others)}

    def owners(self) -> list[dict]:
        """本组织里能编写或批准 SOP 的有效成员：负责人从这里选。"""
        from ..repositories.organization import MembershipRepository

        rows = []
        for membership in MembershipRepository(self.db).for_org(self.ctx.org_id):
            if membership.state != "active":
                continue
            user = self.users.get(membership.user_id)
            if user is None or not (user_may(None, user, "sop.edit") or user_may(None, user, "sop.approve")):
                continue
            rows.append({"id": user.id, "display_name": user.display_name})
        return sorted(rows, key=lambda row: row["display_name"])

    def _apply_document_meta(self, sop: Sop, payload: dict) -> list[str]:
        """分类与负责人属于文件本身。负责人必须是本组织能编写或批准 SOP 的成员。"""
        changed: list[str] = []
        if payload.get("category") is not None and payload["category"].strip() != sop.category:
            changed.append(f"分类: {sop.category or '—'} → {payload['category'].strip()}")
            sop.category = payload["category"].strip()
        owner_id = payload.get("owner_id")
        if owner_id is not None and owner_id != sop.owner_id:
            if owner_id and owner_id not in {row["id"] for row in self.owners()}:
                raise ValidationFailed("负责人必须是本组织能编写或批准 SOP 的有效成员", code="sop_owner_invalid")
            changed.append(f"负责人: {sop.owner_id or '—'} → {owner_id or '—'}")
            sop.owner_id = owner_id
        return changed

    @staticmethod
    def _check_window(effective_from: datetime | None, effective_to: datetime | None) -> None:
        if effective_from and effective_to and effective_to <= effective_from:
            raise ValidationFailed("失效时间必须晚于生效时间", code="sop_window_invalid")

    # ---------- 写 ----------

    def create_version(self, payload: dict, user: User) -> dict:
        code = (payload.get("code") or "").strip()
        if not code:
            raise ValidationFailed("SOP 编号必填")
        sop = self.sops.by_code(code)
        if sop is None:
            sop = Sop(org_id=self.ctx.org_id, code=code, title=payload["title"])
            self.sops.add(sop)
        self._apply_document_meta(sop, payload)
        for key in ("effective_from", "effective_to"):
            payload[key] = as_utc(payload.get(key))
        self._check_window(payload.get("effective_from"), payload.get("effective_to"))
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
            effective_from=payload.get("effective_from"), effective_to=payload.get("effective_to"),
            review_due=payload.get("review_due"), author_id=user.id,
        )
        if payload.get("copy_steps"):
            # 修订从当前生效版本出发：步骤连同稳定标识一起带过来，引用旧版的流程节点在新版里仍对得上
            current = self.versions.effective(sop.id, now()) if existing else None
            if current is not None:
                version.steps = sop_steps.with_keys(list(current.steps or []))
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
        sop = self.sops.get(version.sop_id)
        document = {key: changes.pop(key) for key in ("category", "owner_id") if key in changes}
        for key in ("effective_from", "effective_to"):
            if key in changes:
                changes[key] = as_utc(changes[key])
        notes = self._apply_document_meta(sop, document) if sop and document else []
        self._check_window(
            changes.get("effective_from", version.effective_from), changes.get("effective_to", version.effective_to),
        )
        before = {key: getattr(version, key) for key in changes}
        for key, value in changes.items():
            setattr(version, key, value)
        if attachment is not None:
            attachment.ref_type = "sop_version"
            attachment.ref_id = version.id
        self.versions.bump(version)
        self.audit.record(
            user, "编辑 SOP 草稿", version.id, object_version=version.row_version,
            detail="；".join([*notes, *(f"{k}: {before[k]} → {v}" for k, v in changes.items())]),
        )
        self.db.commit()
        return self.version_out(version)

    def update_document(self, sop_id: str, changes: dict, user: User) -> dict:
        """改受控文件的分类与负责人。它们不是版本内容，已发布版本也能改；改动进审计。"""
        sop = self.sops.get(sop_id)
        if sop is None:
            raise NotFound("SOP 不存在")
        notes = self._apply_document_meta(sop, changes)
        if notes:
            self.audit.record(user, "修改 SOP 文件信息", sop.id, detail="；".join(notes))
        self.db.commit()
        latest = sorted(self.versions.for_sop(sop.id), key=lambda row: row.created_at)[-1:]
        return self.version_out(latest[0]) if latest else {"sop_id": sop.id}

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
        # 每一步带稳定标识：流程节点按它引用，新版本在前面插入步骤时旧节点仍对得上原来那一步
        version.steps = sop_steps.with_keys(steps)
        self.versions.bump(version)
        self.audit.record(user, "编辑 SOP 结构化步骤", version.id, before=f"{before} 步", after=f"{len(steps)} 步",
                          object_version=version.row_version)
        self.db.commit()
        return self.version_out(version)

    def generate_recipe(self, version_id: str, payload: dict, user: User) -> dict:
        """一键生成流程草稿。已发布的 SOP 版本同时挂到流程上；草稿 SOP 生成的流程不挂（流程只能引用已发布 SOP）。"""
        from .recipe_service import RecipeService

        version = self._require(version_id)
        if not version.steps:
            raise StateConflict("这个 SOP 版本还没有结构化步骤", code="sop_steps_missing")
        sop = self.sops.get(version.sop_id)
        steps = sop_steps.to_recipe_steps(version.steps)
        name = (payload.get("name") or "").strip() or f"{sop.title if sop else 'SOP'} 流程草稿"
        linked = version.id if version.state == "published" else ""
        recipes = RecipeService(self.db, self.ctx)
        recipe = recipes.create_from_steps(
            name, int(payload.get("plate") or 8), steps, user, sop_version_id=linked,
            note=f"由 SOP {sop.code if sop else ''} {version.version} 生成",
        )
        self.audit.record(
            user, "由 SOP 生成流程草稿", version.id, after=recipe.id,
            detail=f"{len(steps)} 步；" + ("已挂接本 SOP 版本" if linked else "SOP 未发布，流程未挂接 SOP"),
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
        scope = set(version.capability_scope or [])
        outside = sorted({
            str(step.get("capability")) for step in version.steps or []
            if step.get("kind") == "device" and scope and step.get("capability") not in scope
        })
        if outside:
            raise StateConflict(
                f"结构化步骤用到的能力 {'、'.join(outside)} 不在本版本的适用能力内",
                {"blocked": [{"key": "scope", "label": f"步骤能力 {cap} 超出适用范围"} for cap in outside]},
                code="sop_scope_mismatch",
            )
        self._check_window(version.effective_from, version.effective_to)
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
        effective_from = as_utc(payload.get("effective_from")) or version.effective_from or now()
        self._check_window(effective_from, version.effective_to)
        self._check_effective_from(version, effective_from, reason)
        version.state = "published"
        version.approver_id = user.id
        version.effective_from = effective_from
        version.published_at = now()
        self.versions.bump(version)
        # 同编号的其他已发布版本被它取代：失效时间取新版本的生效时间，到点后新批次只认新版本。
        # 生效时间在将来的，旧版本继续生效到那一刻，不留空窗
        superseded = []
        for other in self.versions.for_sop(version.sop_id):
            if other.id == version.id or other.state != "published":
                continue
            if other.effective_to is not None and other.effective_to <= effective_from:
                continue
            if other.effective_from is not None and other.effective_from > effective_from:
                # 已排好将来生效的版本：本版本只生效到它开始，由它取代本版本
                version.effective_to = min(version.effective_to or other.effective_from, other.effective_from)
                version.superseded_by = other.id
                version.superseded_at = now()
                continue
            other.effective_to = effective_from
            other.superseded_by = version.id
            other.superseded_at = now()
            self.versions.bump(other)
            superseded.append(other.version)
            self.audit.record(
                user, "SOP 版本被取代", other.id, before="生效中", after="已被取代",
                object_version=other.row_version,
                detail=f"被 {version.version} 取代，失效时间 {effective_from:%Y-%m-%d %H:%M}",
            )
        # 新版本生效不自动改在途运行，只显示影响：还关联旧版本的流程（新批次将改按新版本执行）、
        # 按旧版本在途的批次（由负责人决定继续还是新建）
        impact = self.impact_of(version)
        self.audit.record(
            user, "发布 SOP 版本", version.id, sign=True, meaning=signature.meaning,
            signature_id=signature.id, before="评审中", after="已发布",
            object_version=version.row_version,
            detail=(
                f"生效 {effective_from:%Y-%m-%d %H:%M}；"
                + (f"回溯生效，理由：{reason}；" if effective_from < now() - BACKDATE_TOLERANCE else "")
                + (f"取代 {'、'.join(superseded)}；" if superseded else "")
                + f"{len(impact['impacted_recipes'])} 个流程关联旧版本，新批次改按本版本执行；"
                f"{len(impact['impacted_batches'])} 个在途批次按旧版本执行，不自动改版"
            ),
        )
        self.db.commit()
        return {**self.version_out(version), **impact, "superseded_versions": superseded}

    def _check_effective_from(self, version: SopVersion, effective_from: datetime, reason: str) -> None:
        """批准时的生效时间。

        早于当前生效版本的起始：新版本一发布就被旧版本「取代」，死在发布那一刻，拒绝。早于现在：
        回溯生效会把在途批次固化的版本改成「已被取代」、让它不能再被确认，要写明理由（记进审计）。
        """
        current = self.versions.effective(version.sop_id, now())
        if (
            current is not None and current.id != version.id and current.effective_from is not None
            and effective_from < current.effective_from
        ):
            raise ValidationFailed(
                f"生效时间 {effective_from:%Y-%m-%d %H:%M} 早于当前生效的 {current.version}"
                f"（{current.effective_from:%Y-%m-%d %H:%M} 起）：新版本一发布就会被旧版本取代",
                code="sop_effective_before_current",
            )
        if effective_from < now() - BACKDATE_TOLERANCE and not reason:
            raise ValidationFailed(
                "生效时间早于现在：回溯生效会让已按旧版本开的批次变成「已被取代」，请在理由里写明为什么回溯",
                code="sop_backdate_reason_required",
            )

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
        batches = self._active_batches({version.id})
        replacement = self.versions.effective(version.sop_id, now())
        self.audit.record(
            user, "退役 SOP 版本", version.id, before="已发布", after="已退役",
            object_version=version.row_version,
            detail=(
                f"{reason}；{len(impacted)} 个流程仍引用它，"
                + (f"新批次改按 {replacement.version} 执行；" if replacement else "同编号没有生效版本，这些流程不能再建批次；")
                + f"{len(batches)} 个在途批次按它执行，不自动改版；历史引用与附件保持可查"
            ),
        )
        self.db.commit()
        return {
            **self.version_out(version),
            "impacted_recipes": [
                {"id": row.id, "name": row.name, "version": row.version, "state": row.state} for row in impacted
            ],
            "impacted_batches": batches,
            "replacement_version": replacement.version if replacement else "",
        }

    def acknowledge(self, version_id: str, user: User) -> dict:
        version = self.versions.get(version_id)
        if not version:
            raise NotFound("SOP 版本不存在")
        if version.state != "published":
            raise StateConflict("只能确认已发布的版本")
        if status_of(version, now()) in {"superseded", "expired"}:
            raise StateConflict("这个版本已失效，请确认当前生效的版本", code="sop_not_effective")
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
