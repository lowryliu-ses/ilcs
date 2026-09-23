"""实验方案。三种类型走不同的校验分支；审批状态与矩阵锁定分别维护。

matrix 保留原矩阵校验；single_condition 不要求两个因子水平，用显式样本数或样本清单；
commissioned_test 用已登记样本和发布方法，不强制生成新样本或定义耗材。
「矩阵已锁定」是结构冻结，不代表已审批。
"""
from __future__ import annotations

import copy

from sqlalchemy.orm import Session

from ..core.clock import today_iso
from ..core.context import AccessContext
from ..core.db import dec
from ..core.errors import NotFound, PermissionDenied, StateConflict, ValidationFailed
from ..domain import matrix
from ..domain.access import same_person
from ..domain.lifecycle import plan_delete_blockers
from ..models import Plan, PlanVersion, User
from ..repositories.batches import BatchRepository
from ..repositories.governance import UserRepository
from ..repositories.materials import LotRepository
from ..repositories.metrics import MetricRepository
from ..repositories.recipes import ExperimentTaskRepository, PlanRepository, PlanVersionRepository, RecipeRepository
from ..repositories.samples import PhysicalSampleRepository
from .audit_service import AuditService
from .identity_service import IdentityService
from .inventory_service import InventoryService

MATRIX = "matrix"
SINGLE = "single_condition"
COMMISSIONED = "commissioned_test"
PLAN_TYPES = (MATRIX, SINGLE, COMMISSIONED)
TYPE_LABEL = {MATRIX: "矩阵实验", SINGLE: "单条件样本实验", COMMISSIONED: "委托检测"}
STATE_LABEL = {"draft": "草稿", "locked": "矩阵已锁定"}
APPROVAL_LABEL = {"draft": "草稿", "review": "评审中", "approved": "已批准", "rejected": "已驳回"}


class PlanService:
    def __init__(self, db: Session, ctx: AccessContext):
        self.db = db
        self.ctx = ctx
        self.plans = PlanRepository(db, ctx)
        self.versions = PlanVersionRepository(db, ctx)
        self.recipes = RecipeRepository(db, ctx)
        self.lots = LotRepository(db, ctx)
        self.metrics = MetricRepository(db, ctx)
        self.samples = PhysicalSampleRepository(db, ctx)
        self.tasks = ExperimentTaskRepository(db, ctx)
        self.batches = BatchRepository(db, ctx)
        self.users = UserRepository(db)
        self.inventory = InventoryService(db, ctx)
        self.audit = AuditService(db, ctx)
        self.identity = IdentityService(db, ctx)

    # ---------- 读 ----------

    def conditions(self, plan: Plan) -> list[dict]:
        if plan.plan_type != MATRIX:
            # 非矩阵方案只有一个条件组，不生成虚构的因子组合
            return [
                {"group": "C01", "levels": [], "label": "单一条件", "is_control": False}
            ]
        return [
            {"group": c.group, "levels": c.levels, "label": c.label, "is_control": c.is_control}
            for c in matrix.conditions(plan.factors or [], plan.control)
        ]

    def sample_total(self, plan: Plan) -> int:
        if plan.plan_type == MATRIX:
            return len(self.conditions(plan)) * max(1, plan.repeats)
        if plan.plan_type == SINGLE:
            return plan.sample_count or len(plan.sample_ids or [])
        return len(plan.sample_ids or [])

    def layout(self, plan: Plan) -> list[dict]:
        if plan.plan_type != MATRIX:
            return []
        recipe = self.recipes.require(plan.recipe_id, "方法不存在")
        return [
            {
                "well": a.well, "group": a.group, "repeat": a.repeat, "levels": a.levels,
                "label": a.label, "is_control": a.is_control,
            }
            for a in matrix.layout(
                plan.factors or [], plan.control, plan.repeats, recipe.plate, plan.layout, plan.seed
            )
        ]

    def lock_checks(self, plan: Plan) -> list[dict]:
        """结构冻结前的校验，按方案类型分支。"""
        recipe = self.recipes.get(plan.recipe_id)
        plate = recipe.plate if recipe else 0
        if plan.plan_type == MATRIX:
            return self._matrix_checks(plan, plate)
        if plan.plan_type == SINGLE:
            return self._single_checks(plan, plate)
        return self._commissioned_checks(plan)

    def _matrix_checks(self, plan: Plan, plate: int) -> list[dict]:
        factors = plan.factors or []
        conditions = matrix.conditions(factors, plan.control)
        total = len(conditions) * max(plan.repeats, 0) if factors else 0
        control_detail, control_ok = "未设对照（允许）", True
        if plan.control and plan.control.get("cond"):
            wanted = plan.control["cond"]
            control_ok = any(list(c.levels) == list(wanted) for c in conditions)
            control_detail = plan.control.get("label") or str(wanted)
            if not control_ok:
                control_detail += "：不在因子水平组合内"
        return [
            {
                "key": "factors", "label": "至少一个因子，每个因子 ≥ 2 个水平",
                "detail": "、".join(
                    f"{f.get('name')} {len(f.get('levels') or [])} 水平" for f in factors
                ) or "未定义因子",
                "ok": bool(factors) and all(len(f.get("levels") or []) >= 2 for f in factors),
            },
            {
                "key": "capacity", "label": "条件 × 重复 不超过方法样品位",
                "detail": f"{len(conditions)} × {plan.repeats} = {total}；方法每批 {plate} 位",
                "ok": 0 < total <= plate,
            },
            {
                "key": "repeats", "label": "重复次数可评估组内重复性",
                "detail": f"{plan.repeats} 次重复" if plan.repeats >= 2
                          else "单次重复：允许，但结果页无法给出组内 CV",
                "ok": True,
            },
            {"key": "control", "label": "对照条件在矩阵内", "detail": control_detail, "ok": control_ok},
            self._targets_check(plan),
            self._metrics_check(plan),
        ]

    def _target_options(self, plan: Plan) -> list[dict]:
        """因子可以作用的设备参数：方法里每个设备步骤及其能力声明的参数。"""
        from ..domain.steps import DEVICE, kind_of, normalize, step_id_of
        from ..repositories.resources import CapabilityRepository

        recipe = self.recipes.get(plan.recipe_id)
        capabilities = CapabilityRepository(self.db).specs()
        options = []
        for index, step in enumerate(normalize(recipe.steps if recipe else [])):
            if kind_of(step) != DEVICE:
                continue
            declared = (capabilities.get(step.get("cap", "")) or {}).get("params") or {}
            params = sorted(set(declared) | set(step.get("params") or {}))
            options.append({
                "step_id": step_id_of(step, index), "step_name": step.get("name") or f"第 {index + 1} 步",
                "capability": step.get("cap", ""),
                "params": [{"name": name, "unit": declared.get(name, "")} for name in params],
            })
        return options

    def _targets_check(self, plan: Plan) -> dict:
        """因子作用参数：声明了就必须能真的下发；没声明时如实说明条件只影响样本标签。"""
        from ..domain.steps import normalize
        from ..repositories.resources import StationRepository

        factors = plan.factors or []
        declared = [f for f in factors if f.get("target")]
        if not declared:
            return {
                "key": "targets", "label": "因子作用的设备参数",
                "detail": "未声明作用参数：条件只区分样本，设备按方法里的固定参数执行",
                "ok": True,
            }
        recipe = self.recipes.get(plan.recipe_id)
        issues = matrix.target_issues(
            factors, normalize(recipe.steps if recipe else []), StationRepository(self.db, self.ctx).specs(),
        )
        return {
            "key": "targets", "label": "因子作用的设备参数",
            "detail": "；".join(issues) or "、".join(
                f"{f.get('name')} → {f['target'].get('step_id')}.{f['target'].get('param')}" for f in declared
            ),
            "ok": not issues,
        }

    def _single_checks(self, plan: Plan, plate: int) -> list[dict]:
        total = self.sample_total(plan)
        listed = plan.sample_ids or []
        missing = [sid for sid in listed if self.samples.get(sid) is None]
        return [
            {
                "key": "samples", "label": "显式设置样本数或样本清单",
                "detail": (
                    f"样本清单 {len(listed)} 个" if listed else f"样本数 {plan.sample_count}"
                ) + (f"；不存在的样本：{'、'.join(missing)}" if missing else ""),
                "ok": total > 0 and not missing,
            },
            {
                "key": "capacity", "label": "样本数不超过方法样品位",
                "detail": f"{total}；方法每批 {plate} 位",
                "ok": 0 < total <= plate,
            },
            {
                "key": "factors", "label": "单条件方案不要求因子矩阵",
                "detail": "已按单条件校验，不强制两个因子水平",
                "ok": True,
            },
            self._metrics_check(plan),
        ]

    def _commissioned_checks(self, plan: Plan) -> list[dict]:
        listed = plan.sample_ids or []
        missing = [sid for sid in listed if self.samples.get(sid) is None]
        recipe = self.recipes.get(plan.recipe_id)
        return [
            {
                "key": "samples", "label": "使用已登记样本",
                "detail": f"{len(listed)} 个已登记样本"
                          + (f"；不存在：{'、'.join(missing)}" if missing else ""),
                "ok": bool(listed) and not missing,
            },
            {
                "key": "method", "label": "使用已发布方法",
                "detail": (
                    f"{recipe.id} v{recipe.version}（{recipe.state}）" if recipe else "未选择方法"
                ),
                "ok": bool(recipe and recipe.state == "released"),
            },
            {
                "key": "consumables", "label": "委托检测不强制定义耗材",
                "detail": "不生成新样本，也不要求 BOM",
                "ok": True,
            },
            self._metrics_check(plan),
        ]

    def _metrics_check(self, plan: Plan) -> dict:
        required = plan.required_metrics or []
        known = self.metrics.many(required)
        unknown = [m for m in required if m not in known]
        retired = [m.id for m in known.values() if m.state != "active"]
        detail = (
            "、".join(f"{known[m].code} {known[m].version}" for m in required if m in known)
            or "未指定所需检测指标"
        )
        if unknown:
            detail += f"；未登记：{'、'.join(unknown)}"
        if retired:
            detail += f"；已停用：{'、'.join(retired)}"
        return {
            "key": "metrics", "label": "所需检测指标已指定且有效",
            "detail": detail,
            "ok": bool(required) and not unknown and not retired,
        }

    def material_preview(self, plan: Plan) -> list[dict]:
        """BOM + 因子换算需求，对照每种物料的可用量。委托检测没有物料需求。"""
        if plan.plan_type == COMMISSIONED:
            return []
        recipe = self.recipes.require(plan.recipe_id, "方法不存在")
        demands = [{"factor": "方法 BOM", **item} for item in (recipe.bom or [])]
        if plan.plan_type == MATRIX:
            demands += matrix.material_demand(plan.factors or [], plan.repeats)
        rows = []
        for demand in demands:
            material, unit = demand.get("material"), demand.get("unit")
            lots = [
                lot for lot in self.lots.list()
                if lot.material == material and not self.inventory.lot_blockers(lot)
            ]
            available = sum(
                (self.inventory.balances(lot)["available"] for lot in lots), dec(0)
            )
            rows.append(
                {
                    "source": demand.get("factor", ""),
                    "material": material,
                    "unit": unit,
                    "qty": demand.get("qty"),
                    "available": f"{available:f}",
                    "lots": [lot.id for lot in lots],
                    "ok": bool(lots) and available >= dec(demand.get("qty") or 0),
                }
            )
        return rows

    def version_out(self, version: PlanVersion) -> dict:
        author = self.users.get(version.author_id) if version.author_id else None
        approver = self.users.get(version.approver_id) if version.approver_id else None
        return {
            "id": version.id,
            "version": version.version,
            "state": version.state,
            "state_label": APPROVAL_LABEL.get(version.state, version.state),
            "author_name": author.display_name if author else "",
            "approver_name": approver.display_name if approver else "",
            "approved_at": version.approved_at.isoformat(timespec="seconds") if version.approved_at else None,
            "reject_reason": version.reject_reason,
            "created_at": version.created_at.isoformat(timespec="seconds"),
        }

    def to_dict(self, plan: Plan, *, detail: bool = False) -> dict:
        conditions = self.conditions(plan)
        bound = self.plans.bound_batch_ids(plan.id)
        versions = self.versions.for_plan(plan.id)
        approved = self.versions.latest_approved(plan.id)
        payload = {
            "id": plan.id,
            "name": plan.name,
            "plan_type": plan.plan_type,
            "plan_type_label": TYPE_LABEL.get(plan.plan_type, plan.plan_type),
            "recipe_id": plan.recipe_id,
            "project_id": plan.project_id,
            "owner": plan.owner,
            "state": plan.state,
            "state_label": STATE_LABEL.get(plan.state, plan.state),
            # 审批与矩阵锁定分列，不能把 locked 当成 approved
            "approval_state": plan.approval_state,
            "approval_label": APPROVAL_LABEL.get(plan.approval_state, plan.approval_state),
            "version": plan.version,
            "approved_version": approved.version if approved else None,
            "created": plan.created,
            "goal": plan.goal,
            "repeats": plan.repeats,
            "layout": plan.layout,
            "seed": plan.seed,
            "sample_count": self.sample_total(plan),
            "sample_ids": plan.sample_ids or [],
            "required_metrics": plan.required_metrics or [],
            "resource_requirements": plan.resource_requirements or [],
            "method_version": plan.method_version,
            "condition_count": len(conditions),
            "batches": bound,
            "task_count": len(self.tasks.for_plan(plan.id)),
            "row_version": plan.row_version,
            "reject_reason": plan.reject_reason,
            "is_matrix": plan.plan_type == MATRIX,
            "delete_blockers": plan_delete_blockers(plan.state, bound),
        }
        if detail:
            checks = self.lock_checks(plan)
            payload |= {
                "factors": plan.factors,
                "control": plan.control,
                "conditions": conditions,
                "layout_preview": self.layout(plan),
                "checks": checks,
                "lockable": all(c["ok"] for c in checks),
                "materials": self.material_preview(plan),
                "target_options": self._target_options(plan),
                "versions": [self.version_out(row) for row in versions],
                "metrics": [
                    {
                        "id": row.id, "code": row.code, "name": row.name, "version": row.version,
                        "unit": row.unit, "value_type": row.value_type,
                    }
                    for row in self.metrics.many(plan.required_metrics or []).values()
                ],
                "audit": [
                    {
                        "time": e.time.isoformat(timespec="seconds"), "user": e.user,
                        "action": e.action, "before": e.before, "after": e.after,
                        "detail": e.detail, "sign": e.sign,
                    }
                    for e in self.audit.for_target(plan.id)
                ],
            }
        return payload

    def list(self) -> list[dict]:
        return [self.to_dict(plan) for plan in self.plans.list()]

    def page(self, offset: int, limit: int, plan_type: str | None = None, keyword: str = ""):
        rows, total = self.plans.page(offset, limit, plan_type, keyword)
        return [self.to_dict(row) for row in rows], total

    def get(self, plan_id: str) -> dict:
        plan = self.plans.get(plan_id)
        if not plan:
            raise NotFound("实验方案不存在")
        return self.to_dict(plan, detail=True)

    # ---------- 写 ----------

    def create(self, payload: dict, user: User) -> dict:
        recipe = self.recipes.get(payload["recipe_id"])
        if not recipe:
            raise NotFound("方法不存在")
        plan_type = payload.get("plan_type", MATRIX)
        if plan_type not in PLAN_TYPES:
            raise ValidationFailed(f"方案类型只能是 {'、'.join(PLAN_TYPES)}")
        plan = Plan(
            id=self._next_plan_id(payload["recipe_id"]),
            org_id=self.ctx.org_id,
            project_id=payload.get("project_id", ""),
            name=payload["name"],
            recipe_id=payload["recipe_id"],
            owner=user.display_name,
            state="draft",
            approval_state="draft",
            plan_type=plan_type,
            version=1,
            created=today_iso(),
            goal=payload.get("goal", ""),
            repeats=payload.get("repeats", 1),
            layout=payload.get("layout", "sequential"),
            seed=payload.get("seed", 1),
            factors=payload.get("factors") or [],
            control=payload.get("control"),
            sample_count=payload.get("sample_count", 0),
            sample_ids=payload.get("sample_ids") or [],
            required_metrics=payload.get("required_metrics") or [],
            resource_requirements=payload.get("resource_requirements") or [],
            method_version=recipe.version,
        )
        self.plans.add(plan)
        self.audit.record(
            user, "新建实验方案", plan.id, before="—", after="草稿",
            detail=f"{TYPE_LABEL.get(plan_type, plan_type)}；方法 {recipe.id} v{recipe.version}",
            object_version=plan.row_version,
        )
        self.db.commit()
        return self.to_dict(plan, detail=True)

    def _next_plan_id(self, recipe_id: str) -> str:
        prefix = f"EP-{recipe_id.replace('R-', '')}-"
        used = {plan.id for plan in self.plans.list()}
        sequence = 1
        while f"{prefix}{sequence:02d}" in used:
            sequence += 1
        return f"{prefix}{sequence:02d}"

    def patch(self, plan_id: str, changes: dict, user: User) -> dict:
        plan = self.plans.get(plan_id)
        if not plan:
            raise NotFound("实验方案不存在")
        self.plans.check_version(plan, changes.pop("row_version", None), "实验方案")
        if plan.state != "draft":
            raise StateConflict("矩阵已锁定，不能修改结构；请先解锁")
        if plan.approval_state == "approved":
            raise StateConflict(
                "已批准的版本不可修改，请用「修订」生成新版本", code="plan_approved_immutable"
            )
        if changes.get("plan_type") and changes["plan_type"] not in PLAN_TYPES:
            raise ValidationFailed(f"方案类型只能是 {'、'.join(PLAN_TYPES)}")
        for key, value in changes.items():
            setattr(plan, key, value)
        self.plans.bump(plan)
        self.audit.record(
            user, "编辑实验方案", plan_id, detail="、".join(changes),
            object_version=plan.row_version,
        )
        self.db.commit()
        return self.to_dict(plan, detail=True)

    def lock(self, plan_id: str, user: User) -> dict:
        plan = self.plans.get(plan_id)
        if not plan:
            raise NotFound("实验方案不存在")
        if plan.state == "locked":
            raise StateConflict("结构已锁定")
        failed = [c for c in self.lock_checks(plan) if not c["ok"]]
        if failed:
            raise StateConflict("锁定校验未通过", {"checks": failed})
        plan.state = "locked"
        self.plans.bump(plan)
        self.audit.record(
            user, "锁定方案结构", plan_id, before="草稿", after="结构已锁定",
            detail=(
                f"{len(self.conditions(plan))} 个条件 × {plan.repeats} 次重复；"
                f"锁定只是结构冻结，审批状态仍为 {APPROVAL_LABEL.get(plan.approval_state)}"
            ),
            object_version=plan.row_version,
        )
        self.db.commit()
        return self.to_dict(plan, detail=True)

    def unlock(self, plan_id: str, user: User) -> dict:
        plan = self.plans.get(plan_id)
        if not plan:
            raise NotFound("实验方案不存在")
        bound = self.plans.bound_batch_ids(plan_id)
        if bound:
            raise StateConflict("已有批次绑定该方案，不能解锁", {"batches": bound})
        plan.state = "draft"
        self.plans.bump(plan)
        self.audit.record(
            user, "解锁方案结构", plan_id, before="结构已锁定", after="草稿",
            object_version=plan.row_version,
        )
        self.db.commit()
        return self.to_dict(plan, detail=True)

    # ---------- 审批 ----------

    def submit(self, plan_id: str, user: User) -> dict:
        plan = self.plans.get(plan_id)
        if not plan:
            raise NotFound("实验方案不存在")
        if plan.approval_state in {"review", "approved"}:
            raise StateConflict(f"方案已是{APPROVAL_LABEL.get(plan.approval_state)}")
        failed = [c for c in self.lock_checks(plan) if not c["ok"]]
        if failed:
            raise StateConflict("方案校验未通过，已阻止提交评审", {"checks": failed})
        version = self.versions.find(plan.id, plan.version)
        if version is None:
            version = PlanVersion(
                org_id=self.ctx.org_id, plan_id=plan.id, version=plan.version,
                snapshot=self._snapshot(plan), author_id=user.id, state="review",
            )
            self.versions.add(version)
        else:
            version.state = "review"
            version.snapshot = self._snapshot(plan)
        plan.approval_state = "review"
        plan.reject_reason = ""
        self.plans.bump(plan)
        self.audit.record(
            user, "提交方案评审", plan_id, before="草稿", after="评审中",
            detail=f"版本 {plan.version}", object_version=plan.row_version,
        )
        self.db.commit()
        return self.to_dict(plan, detail=True)

    def decide(self, plan_id: str, payload: dict, user: User) -> dict:
        """批准或驳回。批准版本不可修改；作者不能批准自己写的方案。"""
        plan = self.plans.get(plan_id)
        if not plan:
            raise NotFound("实验方案不存在")
        if plan.approval_state != "review":
            raise StateConflict("只有评审中的方案可以批准或驳回")
        conclusion = payload.get("conclusion")
        if conclusion not in {"approved", "rejected"}:
            raise ValidationFailed("结论只能是 approved 或 rejected")
        version = self.versions.find(plan.id, plan.version)
        if version is None:
            raise StateConflict("找不到待审版本记录")
        if same_person(version.author_id, user.id):
            raise PermissionDenied(
                "不能批准本人编写的方案（职责分离）", code="self_approval_denied"
            )
        reason = (payload.get("reason") or "").strip()
        if conclusion == "rejected":
            if not reason:
                raise ValidationFailed("驳回必须写明理由")
            plan.approval_state = "draft"
            plan.reject_reason = reason
            version.state = "draft"
            version.reject_reason = reason
            self.plans.bump(plan)
            self.audit.record(
                user, "驳回实验方案", plan_id, before="评审中", after="草稿", detail=reason,
                object_version=plan.row_version,
            )
            self.db.commit()
            return self.to_dict(plan, detail=True)

        signature = self.identity.consume_signature(
            payload.get("signature_id"), user, "批准实验方案",
            object_ref=plan.id, object_version=plan.row_version,
        )
        from ..core.clock import now

        plan.approval_state = "approved"
        version.state = "approved"
        version.approver_id = user.id
        version.approved_at = now()
        version.signature_id = signature.id
        version.snapshot = self._snapshot(plan)
        self.plans.bump(plan)
        self.audit.record(
            user, "批准实验方案", plan_id, sign=True, meaning=signature.meaning,
            signature_id=signature.id, before="评审中", after="已批准",
            object_version=plan.row_version,
            detail=f"版本 {plan.version} 冻结，不可再修改；修订将生成新版本",
        )
        self.db.commit()
        return self.to_dict(plan, detail=True)

    def revise(self, plan_id: str, user: User) -> dict:
        """修订已批准版本：生成新版本，原版本快照不变。"""
        plan = self.plans.get(plan_id)
        if not plan:
            raise NotFound("实验方案不存在")
        if plan.approval_state != "approved":
            raise StateConflict("只有已批准的方案需要修订产生新版本")
        plan.version += 1
        plan.approval_state = "draft"
        plan.state = "draft"
        plan.reject_reason = ""
        self.plans.bump(plan)
        self.versions.add(
            PlanVersion(
                org_id=self.ctx.org_id, plan_id=plan.id, version=plan.version,
                snapshot=self._snapshot(plan), author_id=user.id, state="draft",
            )
        )
        self.audit.record(
            user, "修订实验方案", plan_id, before="已批准", after=f"草稿 v{plan.version}",
            detail="原批准版本快照保留，历史运行不受影响", object_version=plan.row_version,
        )
        self.db.commit()
        return self.to_dict(plan, detail=True)

    @staticmethod
    def _snapshot(plan: Plan) -> dict:
        return copy.deepcopy(
            {
                "id": plan.id, "name": plan.name, "plan_type": plan.plan_type,
                "version": plan.version, "goal": plan.goal, "repeats": plan.repeats,
                "layout": plan.layout, "seed": plan.seed, "factors": plan.factors,
                "control": plan.control, "sample_count": plan.sample_count,
                "sample_ids": plan.sample_ids, "required_metrics": plan.required_metrics,
                "resource_requirements": plan.resource_requirements,
                "recipe_id": plan.recipe_id, "method_version": plan.method_version,
            }
        )

    def delete(self, plan_id: str, user: User) -> dict:
        plan = self.plans.get(plan_id)
        if not plan:
            raise NotFound("实验方案不存在")
        blockers = plan_delete_blockers(plan.state, self.plans.bound_batch_ids(plan_id))
        if self.tasks.for_plan(plan_id):
            blockers.append(f"{len(self.tasks.for_plan(plan_id))} 个实验任务引用它")
        if plan.approval_state == "approved":
            blockers.append("已批准的方案不删除，请改用修订或停用")
        if blockers:
            raise StateConflict(
                "实验方案不可删除", {"blocked": [{"key": "plan", "label": b} for b in blockers]}
            )
        self.audit.record(
            user, "删除实验方案草稿", plan_id, before="草稿", after="已删除",
            detail=f"{plan.name}；无批次与任务绑定",
        )
        for version in self.versions.for_plan(plan_id):
            self.db.delete(version)
        self.db.delete(plan)
        self.db.commit()
        return {"id": plan_id, "deleted": True}
