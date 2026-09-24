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
from ..domain import approval as approval_rules
from ..domain import diffs, matrix
from ..domain.access import same_person
from ..domain.lifecycle import plan_delete_blockers
from ..models import Plan, PlanTemplate, PlanVersion, User
from ..repositories.batches import BatchRepository
from ..repositories.governance import UserRepository
from ..repositories.materials import LotRepository
from ..repositories.metrics import MetricRepository
from ..repositories.recipes import ExperimentTaskRepository, PlanRepository, PlanVersionRepository, RecipeRepository
from ..repositories.samples import PhysicalSampleRepository
from .audit_service import AuditService
from .identity_service import IdentityService, admin_self_approval, user_may
from .inventory_service import InventoryService

MATRIX = "matrix"
SINGLE = "single_condition"
COMMISSIONED = "commissioned_test"
PLAN_TYPES = (MATRIX, SINGLE, COMMISSIONED)
TYPE_LABEL = {MATRIX: "矩阵实验", SINGLE: "单条件样本实验", COMMISSIONED: "委托检测"}
STATE_LABEL = {"draft": "草稿", "locked": "矩阵已锁定"}
APPROVAL_LABEL = {"draft": "草稿", "review": "评审中", "approved": "已批准", "rejected": "已驳回"}
# 草稿与已驳回可以编辑；评审中冻结内容（审的是哪一份就是哪一份），已批准只能修订
EDITABLE_APPROVAL = {"draft", "rejected"}
SNAPSHOT_LABELS = {
    "name": "名称", "plan_type": "方案类型", "goal": "目的", "recipe_id": "实验流程", "method_version": "流程版本",
    "factors": "因子与水平", "control": "对照", "repeats": "重复次数", "layout": "布局", "seed": "随机种子",
    "design_points": "设计点", "design_space": "设计空间", "sample_count": "样本数", "sample_ids": "样本清单",
    "required_metrics": "检测指标", "resource_requirements": "资源需求",
}
RESTORABLE = ("name", "goal", "factors", "control", "repeats", "layout", "seed", "design_points", "design_space",
              "sample_count", "sample_ids", "required_metrics", "resource_requirements")
TEMPLATE_FIELDS = ("goal", "factors", "control", "repeats", "layout", "seed", "design_space", "sample_count",
                   "required_metrics", "resource_requirements")


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
            for c in matrix.conditions(plan.factors or [], plan.control, plan.design_points or None)
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
        recipe = self.recipes.require(plan.recipe_id, "流程不存在")
        return [
            {
                "well": a.well, "group": a.group, "repeat": a.repeat, "levels": a.levels,
                "label": a.label, "is_control": a.is_control,
            }
            for a in matrix.layout(
                plan.factors or [], plan.control, plan.repeats, recipe.plate, plan.layout, plan.seed,
                plan.design_points or None,
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
        points = plan.design_points or []
        conditions = matrix.conditions(factors, plan.control, points or None)
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
                "ok": bool(factors) and (
                    bool(points) or all(len(f.get("levels") or []) >= 2 for f in factors)
                ),
            },
            self._design_space_check(plan),
            {
                "key": "capacity", "label": "条件 × 重复 不超过流程样品位",
                "detail": f"{len(conditions)} × {plan.repeats} = {total}；流程每批 {plate} 位",
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

    def _design_space_check(self, plan: Plan) -> dict:
        """显式设计点必须落在设计空间内；没有设计点的全因子方案，设计空间只约束以后的提案。"""
        points = plan.design_points or []
        if not points:
            return {
                "key": "design_space", "label": "设计点在设计空间内",
                "detail": "全因子方案；设计空间只约束后续提案" if plan.design_space else "未设置设计空间",
                "ok": True,
            }
        issues = matrix.point_issues(plan.factors or [], points, plan.design_space or {})
        return {
            "key": "design_space", "label": "设计点在设计空间内",
            "detail": "；".join(issues[:6]) or f"{len(points)} 个设计点均在设计空间内",
            "ok": not issues,
        }

    def _target_options(self, plan: Plan) -> list[dict]:
        """因子可以作用的设备参数：流程里每个设备步骤及其能力声明的参数。"""
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
                "detail": "未声明作用参数：条件只区分样本，设备按流程里的固定参数执行",
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
                "key": "capacity", "label": "样本数不超过流程样品位",
                "detail": f"{total}；流程每批 {plate} 位",
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
                "key": "method", "label": "使用已发布流程",
                "detail": (
                    f"{recipe.id} v{recipe.version}（{recipe.state}）" if recipe else "未选择流程"
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
        recipe = self.recipes.require(plan.recipe_id, "流程不存在")
        demands = [{"factor": "流程 BOM", **item} for item in (recipe.bom or [])]
        if plan.plan_type == MATRIX:
            demands += matrix.material_demand(plan.factors or [], plan.repeats, plan.design_points or None)
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
            "approvals": self._approvals_out(version.approvals or []),
        }

    def _approvals_out(self, levels: list[dict]) -> list[dict]:
        rows = []
        for row in levels:
            assignee = self.users.get(row.get("assignee_id")) if row.get("assignee_id") else None
            decider = self.users.get(row.get("decided_by")) if row.get("decided_by") else None
            rows.append({
                **row, "assignee_name": assignee.display_name if assignee else "",
                "decided_by_name": decider.display_name if decider else "",
            })
        return rows

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
            # 当前版本的逐级审批进度
            "approvals": self._approvals_out(
                (self.versions.find(plan.id, plan.version).approvals or [])
                if self.versions.find(plan.id, plan.version) is not None else []
            ),
            "is_matrix": plan.plan_type == MATRIX,
            "delete_blockers": plan_delete_blockers(plan.state, bound),
        }
        if detail:
            checks = self.lock_checks(plan)
            payload |= {
                "factors": plan.factors,
                "control": plan.control,
                "design_points": plan.design_points or [],
                "design_space": plan.design_space or {},
                "parent_plan_id": plan.parent_plan_id,
                "round_no": plan.round_no,
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
        if payload.get("template_id"):
            # 套用模板：模板给缺省结构，请求里显式给的字段优先
            template = self._template(payload["template_id"])
            if template.retired:
                raise StateConflict("方案模板已停用")
            merged = {**copy.deepcopy(template.body or {}), "plan_type": template.plan_type}
            merged.update({key: value for key, value in payload.items() if value not in (None, "", [], {})})
            if not merged.get("recipe_id") and template.recipe_id:
                merged["recipe_id"] = template.recipe_id
            payload = merged
        if not payload.get("recipe_id"):
            raise ValidationFailed("方案必须选择实验流程（模板没有建议流程时请在请求里给 recipe_id）")
        recipe = self.recipes.get(payload["recipe_id"])
        if not recipe:
            raise NotFound("流程不存在")
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
            design_space=payload.get("design_space") or {},
            design_points=payload.get("design_points") or [],
            sample_count=payload.get("sample_count", 0),
            sample_ids=payload.get("sample_ids") or [],
            required_metrics=payload.get("required_metrics") or [],
            resource_requirements=payload.get("resource_requirements") or [],
            method_version=recipe.version,
        )
        self.plans.add(plan)
        self.audit.record(
            user, "新建实验方案", plan.id, before="—", after="草稿",
            detail=f"{TYPE_LABEL.get(plan_type, plan_type)}；流程 {recipe.id} v{recipe.version}",
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
        if plan.approval_state == "review":
            raise StateConflict("评审中的方案不能修改：审的是哪一份就是哪一份；需要改请先驳回", code="plan_in_review")
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

    def submit(self, plan_id: str, user: User, approvers: list[dict] | None = None) -> dict:
        """提交评审。可以带多级审批（每级可指定审批人），不带就是一级「QA 审批」。"""
        plan = self.plans.get(plan_id)
        if not plan:
            raise NotFound("实验方案不存在")
        if plan.approval_state in {"review", "approved"}:
            raise StateConflict(f"方案已是{APPROVAL_LABEL.get(plan.approval_state)}")
        failed = [c for c in self.lock_checks(plan) if not c["ok"]]
        if failed:
            raise StateConflict("方案校验未通过，已阻止提交评审", {"checks": failed})
        try:
            levels = approval_rules.build_levels(approvers)
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        candidates = {row["id"]: row for row in self.approvers()} if any(row["assignee_id"] for row in levels) else {}
        for row in levels:
            if not row["assignee_id"]:
                continue
            if same_person(row["assignee_id"], user.id):
                raise ValidationFailed(f"第 {row['level']} 级不能指定提交人本人审批（职责分离）")
            if row["assignee_id"] not in candidates:
                # 只接受本组织有效成员里有批准权限的人：指定一个审不了的人，评审会一直卡住
                raise ValidationFailed(f"第 {row['level']} 级指定的审批人不是本组织有批准方案权限的成员")
        assigned = [row["assignee_id"] for row in levels if row["assignee_id"]]
        if len(assigned) != len(set(assigned)):
            raise ValidationFailed("同一个人不能被指定审批两级")
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
        version.approvals = levels
        before = APPROVAL_LABEL.get(plan.approval_state, plan.approval_state)
        plan.approval_state = "review"
        plan.reject_reason = ""
        self.plans.bump(plan)
        self.audit.record(
            user, "提交方案评审", plan_id, before=before, after="评审中",
            detail=f"版本 {plan.version}；{len(levels)} 级审批：" + " → ".join(
                row["label"] + (f"（指定 {candidates[row['assignee_id']]['display_name']}）" if row["assignee_id"] else "")
                for row in levels
            ),
            object_version=plan.row_version,
        )
        self.db.commit()
        return self.to_dict(plan, detail=True)

    def decide(self, plan_id: str, payload: dict, user: User) -> dict:
        """逐级批准或驳回。最后一级通过才算批准（版本冻结）；任一级驳回，方案回到「已驳回」。

        作者不能审任何一级；指定了审批人的级别只有他能审；同一个人不能审两级。每一级批准都要签名。
        """
        from ..core.clock import now

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
        levels = list(version.approvals or []) or approval_rules.build_levels(None)
        level = approval_rules.current(levels)
        reasons = approval_rules.blockers(levels, user.id, version.author_id, conclusion)
        author_only = reasons == ["不能审批本人编写的方案（职责分离）"]
        if reasons and not (author_only and admin_self_approval(self.db, self.ctx, user, plan.id, "批准本人编写的方案")):
            code = "self_approval_denied" if any("本人" in reason for reason in reasons) else "approver_not_assigned"
            raise PermissionDenied("；".join(reasons), code=code)
        reason = (payload.get("reason") or "").strip()
        stamp = now().isoformat(timespec="seconds")
        if conclusion == "rejected":
            if not reason:
                raise ValidationFailed("驳回必须写明理由")
            version.approvals, _ = approval_rules.record(levels, user.id, "rejected", stamp, reason)
            plan.approval_state = "rejected"
            plan.reject_reason = reason
            version.state = "rejected"
            version.reject_reason = reason
            self.plans.bump(plan)
            self.audit.record(
                user, "驳回实验方案", plan_id, before="评审中", after="已驳回",
                detail=f"第 {level['level']} 级（{level['label']}）驳回：{reason}", object_version=plan.row_version,
            )
            self.db.commit()
            return self.to_dict(plan, detail=True)

        signature = self.identity.consume_signature(
            payload.get("signature_id"), user, "批准实验方案",
            object_ref=plan.id, object_version=plan.row_version,
        )
        version.approvals, done = approval_rules.record(levels, user.id, "approved", stamp, reason, signature.id)
        if not done:
            following = approval_rules.current(version.approvals)
            self.audit.record(
                user, "方案逐级审批", plan_id, sign=True, meaning=signature.meaning, signature_id=signature.id,
                before=f"第 {level['level']} 级待审", after=f"第 {following['level']} 级待审",
                detail=f"{level['label']} 通过；下一级 {following['label']}", object_version=plan.row_version,
            )
            self.db.commit()
            return self.to_dict(plan, detail=True)

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
            detail=f"版本 {plan.version} 冻结，不可再修改；修订将生成新版本"
            + (f"；{len(levels)} 级审批全部通过" if len(levels) > 1 else ""),
        )
        self.db.commit()
        return self.to_dict(plan, detail=True)

    def withdraw(self, plan_id: str, reason: str, user: User) -> dict:
        """撤回评审：回到草稿，已有的逐级结论作废。提交人本人或有编辑权限的人可以撤回。"""
        plan = self.plans.get(plan_id)
        if not plan:
            raise NotFound("实验方案不存在")
        if plan.approval_state != "review":
            raise StateConflict("只有评审中的方案可以撤回")
        version = self.versions.find(plan.id, plan.version)
        if not (version is not None and same_person(version.author_id, user.id)) and not self.ctx.has("plan.edit"):
            raise PermissionDenied("只有提交人或有编辑权限的人可以撤回评审")
        plan.approval_state = "draft"
        if version is not None:
            version.state = "draft"
            version.approvals = []
        self.plans.bump(plan)
        self.audit.record(user, "撤回方案评审", plan_id, before="评审中", after="草稿",
                          detail=reason or "撤回后修改再提交", object_version=plan.row_version)
        self.db.commit()
        return self.to_dict(plan, detail=True)

    # ---------- 版本对比与恢复 ----------

    def diff(self, plan_id: str, from_version: int, to_version: int | None = None) -> dict:
        plan = self.plans.get(plan_id)
        if not plan:
            raise NotFound("实验方案不存在")
        source = self.versions.find(plan.id, from_version)
        if source is None:
            raise NotFound(f"方案版本 v{from_version} 不存在")
        if to_version is None or to_version == plan.version:
            target, label = self._snapshot(plan), f"v{plan.version}（当前）"
        else:
            row = self.versions.find(plan.id, to_version)
            if row is None:
                raise NotFound(f"方案版本 v{to_version} 不存在")
            target, label = row.snapshot or {}, f"v{to_version}"
        return {
            "plan_id": plan.id, "from": f"v{from_version}", "to": label,
            "changes": diffs.diff(source.snapshot or {}, target, SNAPSHOT_LABELS, ignore=("id", "version")),
        }

    def restore(self, plan_id: str, from_version: int, user: User, expected: int | None = None) -> dict:
        """把历史版本的内容恢复到当前草稿（版本号不变，审批另走）。历史版本快照本身不动。"""
        plan = self.plans.get(plan_id)
        if not plan:
            raise NotFound("实验方案不存在")
        self.plans.check_version(plan, expected, "实验方案")
        if plan.approval_state not in EDITABLE_APPROVAL or plan.state != "draft":
            raise StateConflict("只有未锁定的草稿或已驳回的方案可以恢复历史内容；已批准的请先修订", code="plan_not_editable")
        source = self.versions.find(plan.id, from_version)
        if source is None:
            raise NotFound(f"方案版本 v{from_version} 不存在")
        snapshot = source.snapshot or {}
        changes = diffs.diff(self._snapshot(plan), snapshot, SNAPSHOT_LABELS, ignore=("id", "version", "recipe_id", "method_version", "plan_type"))
        for key in RESTORABLE:
            if key in snapshot:
                setattr(plan, key, copy.deepcopy(snapshot[key]))
        self.plans.bump(plan)
        self.audit.record(
            user, "恢复方案历史内容", plan_id, before=f"v{plan.version} 草稿", after=f"内容取自 v{from_version}",
            detail="、".join(row["label"] for row in changes) or "内容相同，无变化", object_version=plan.row_version,
        )
        self.db.commit()
        return self.to_dict(plan, detail=True)

    def approvers(self) -> list[dict]:
        """本组织里有「批准实验方案」权限的有效成员：提交评审时给各级指定审批人用。"""
        from ..domain.permissions import ROLE_NAMES
        from ..models import roles_of
        from ..repositories.organization import MembershipRepository

        rows = []
        for membership in MembershipRepository(self.db).for_org(self.ctx.org_id):
            if membership.state != "active":
                continue
            user = self.users.get(membership.user_id)
            if user is None or not user_may(None, user, "plan.approve"):
                continue
            rows.append({"id": user.id, "display_name": user.display_name,
                         "roles": [ROLE_NAMES.get(role, role) for role in roles_of(user)]})
        return sorted(rows, key=lambda row: row["display_name"])

    # ---------- 方案模板 ----------

    def template_out(self, template: PlanTemplate) -> dict:
        return {
            "id": template.id, "name": template.name, "description": template.description,
            "plan_type": template.plan_type, "plan_type_label": TYPE_LABEL.get(template.plan_type, template.plan_type),
            "recipe_id": template.recipe_id, "body": template.body or {}, "source_plan_id": template.source_plan_id,
            "retired": template.retired, "row_version": template.row_version,
            "created_at": template.created_at.isoformat(timespec="seconds") if template.created_at else None,
        }

    def templates(self, include_retired: bool = False) -> list[dict]:
        query = self.db.query(PlanTemplate).filter(PlanTemplate.org_id == self.ctx.org_id)
        if not include_retired:
            query = query.filter(PlanTemplate.retired.is_(False))
        return [self.template_out(row) for row in query.order_by(PlanTemplate.name).all()]

    def _template(self, template_id: str) -> PlanTemplate:
        template = self.db.get(PlanTemplate, template_id)
        if template is None or template.org_id != self.ctx.org_id:
            raise NotFound("方案模板不存在")
        return template

    def create_template(self, payload: dict, user: User) -> dict:
        """新建模板：从一个已有方案取结构（from_plan_id），或直接给字段。"""
        body: dict = {}
        plan_type = payload.get("plan_type") or MATRIX
        recipe_id = payload.get("recipe_id") or ""
        source = ""
        if payload.get("from_plan_id"):
            plan = self.plans.get(payload["from_plan_id"])
            if plan is None:
                raise NotFound("来源方案不存在")
            snapshot = self._snapshot(plan)
            body = {key: copy.deepcopy(snapshot.get(key)) for key in TEMPLATE_FIELDS if key in snapshot}
            plan_type, recipe_id, source = plan.plan_type, recipe_id or plan.recipe_id, plan.id
        else:
            body = {key: copy.deepcopy(payload[key]) for key in TEMPLATE_FIELDS if payload.get(key) is not None}
        if plan_type not in PLAN_TYPES:
            raise ValidationFailed(f"方案类型只能是 {'、'.join(PLAN_TYPES)}")
        if not str(payload.get("name") or "").strip():
            raise ValidationFailed("模板名称必填")
        template = PlanTemplate(
            org_id=self.ctx.org_id, name=payload["name"], description=payload.get("description", ""),
            plan_type=plan_type, recipe_id=recipe_id, body=body, source_plan_id=source, created_by=user.id,
        )
        self.db.add(template)
        self.db.flush()
        self.audit.record(user, "新建方案模板", template.id, before="—", after="可用",
                          detail=f"{template.name}（{TYPE_LABEL.get(plan_type, plan_type)}）" + (f"；取自 {source}" if source else ""))
        self.db.commit()
        return self.template_out(template)

    def retire_template(self, template_id: str, user: User) -> dict:
        template = self._template(template_id)
        template.retired = True
        template.row_version = int(template.row_version or 0) + 1
        self.audit.record(user, "停用方案模板", template.id, before="可用", after="停用", detail=template.name)
        self.db.commit()
        return self.template_out(template)

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
                "design_points": plan.design_points or [], "design_space": plan.design_space or {},
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
