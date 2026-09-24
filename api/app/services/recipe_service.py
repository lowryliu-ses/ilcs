from __future__ import annotations

import copy

from sqlalchemy.orm import Session

from ..core.clock import today_iso
from ..core.context import AccessContext
from ..core.errors import NotFound, PermissionDenied, StateConflict, ValidationFailed
from ..domain.access import same_person
from ..domain.lifecycle import recipe_delete_blockers
from ..domain.recipe_rules import EDITABLE_STATES, is_valid, recipe_checks, validate_steps
from ..domain.steps import KIND_NAMES, assign_step_ids, kind_of, normalize, resource_demand
from ..models import Recipe, User
from ..repositories.batches import BatchRepository
from ..repositories.recipes import PlanRepository, RecipeRepository
from ..repositories.resources import CapabilityRepository, StationRepository
from ..repositories.sops import SopVersionRepository
from .audit_service import AuditService
from .identity_service import IdentityService, admin_self_approval, user_may
from .flow_expansion import resolved_steps
from .simulation_service import SimulationService, content_hash

def _content_hash(recipe: Recipe) -> str:
    return content_hash(recipe)


STATE_LABEL = {
    "draft": "草稿", "review": "评审中", "approved": "已批准", "released": "已发布", "retired": "已退役",
}


class RecipeService:
    def __init__(self, db: Session, ctx: AccessContext):
        self.db = db
        self.ctx = ctx
        self.recipes = RecipeRepository(db, ctx)
        self.plans = PlanRepository(db, ctx)
        self.batches = BatchRepository(db, ctx)
        self.stations = StationRepository(db, ctx)
        self.capabilities = CapabilityRepository(db)
        self.sop_versions = SopVersionRepository(db, ctx)
        self.audit = AuditService(db, ctx)
        self.identity = IdentityService(db, ctx)

    # ---------- 读 ----------

    def validation_of(self, recipe: Recipe) -> list[dict]:
        # 设备方法引用先解析：缺省参数、适用型号与程序要参与工位匹配
        steps, method_problems = resolved_steps(self.db, self.ctx, recipe.steps or [])
        return validate_steps(
            steps, self.stations.specs(), self.capabilities.specs(),
            self._subflow_problems(recipe), method_problems,
        )

    def _subflow_problems(self, recipe: Recipe) -> dict[str, list[str]]:
        """子流程引用的问题：展开到底，不存在 / 未发布 / 循环引用 / 嵌套过深都在这里查出来。"""
        from ..domain.steps import SUBFLOW
        from ..domain.subflow import step_problems
        from .flow_expansion import resolver

        steps = [step for step in normalize(recipe.steps or []) if kind_of(step) == SUBFLOW]
        if not steps:
            return {}
        resolve = resolver(self.db, self.ctx)
        return {step["step_id"]: step_problems(step, resolve, recipe.id) for step in steps}

    def _expanded_critical(self, recipe: Recipe) -> float | None:
        """有子流程时按展开后的步骤算关键路径；展开失败（引用有问题）返回 None，校验会另行报出。"""
        from ..domain.graph import critical_path_min
        from ..domain.subflow import SubflowError, has_subflow
        from .flow_expansion import expanded_steps

        if not has_subflow(recipe.steps or []):
            return None
        try:
            steps, _ = expanded_steps(self.db, self.ctx, recipe)
        except SubflowError:
            return None
        return critical_path_min(steps)

    def to_dict(self, recipe: Recipe, *, detail: bool = False) -> dict:
        validation = self.validation_of(recipe)
        payload = {
            "id": recipe.id,
            "name": recipe.name,
            "version": recipe.version,
            "state": recipe.state,
            "state_label": STATE_LABEL.get(recipe.state, recipe.state),
            "owner": recipe.owner,
            "updated": recipe.updated,
            "plate": recipe.plate,
            "risk": recipe.risk,
            "design": recipe.design,
            "golden_batch_id": recipe.golden_batch_id,
            "parent": recipe.parent,
            "needs_revision": recipe.needs_revision,
            "sop_version_id": recipe.sop_version_id,
            "author_user_id": recipe.author_user_id,
            "submitted_by": recipe.submitted_by,
            "row_version": recipe.row_version,
            "valid": is_valid(validation),
            "step_count": len(recipe.steps or []),
            "critical_path_min": self._critical(recipe),
            "resource_demand": resource_demand(recipe.steps or []),
            "delete_blockers": self.deletable(recipe),
        }
        if detail:
            recoveries = {c.id: c.recovery for c in self.capabilities.list()}
            brief = self._sop_brief(recipe.sop_version_id) if recipe.sop_version_id else None
            payload |= {
                "steps": recipe.steps,
                # 编辑器在本地给新步骤分配标识（拖线建依赖要立刻能引用），必须避开用过的
                "used_step_ids": self._step_ids_of(recipe),
                "simulation": recipe.simulation or {},
                "simulation_current": bool(recipe.simulation)
                and (recipe.simulation or {}).get("content_hash") == _content_hash(recipe),
                "bom": recipe.bom,
                "history": recipe.history,
                "diff": recipe.diff,
                "validation": validation,
                "sop": brief,
                "checks": recipe_checks(
                    normalize(recipe.steps or []), validation, recipe.bom or [], recipe.risk,
                    recipe.sop_version_id,
                    f"{brief['code']} {brief['title']} {brief['version']}" if brief else "",
                    self._expanded_critical(recipe),
                ),
                "recovery_by_capability": {
                    step.get("cap"): recoveries.get(step.get("cap"), {}) for step in recipe.steps or []
                },
                "plans": [{"id": p.id, "name": p.name, "state": p.state} for p in self.plans.for_recipe(recipe.id)],
            }
        return payload

    def _critical(self, recipe: Recipe) -> float:
        from ..domain.graph import critical_path_min

        expanded = self._expanded_critical(recipe)
        return expanded if expanded is not None else critical_path_min(normalize(recipe.steps or []))

    def list(self) -> list[dict]:
        recipes = self.recipes.list()
        self._prime_reference_cache(recipes)
        try:
            return [self.to_dict(recipe) for recipe in recipes]
        finally:
            self._reference_cache = None

    def get(self, recipe_id: str) -> dict:
        recipe = self.recipes.get(recipe_id)
        if not recipe:
            raise NotFound("方法不存在")
        return self.to_dict(recipe, detail=True)

    def _sop_brief(self, sop_version_id: str) -> dict | None:
        version = self.sop_versions.get(sop_version_id)
        if version is None:
            return None
        from ..models import Sop

        sop = self.db.get(Sop, version.sop_id)
        return {
            "sop_version_id": version.id,
            "code": sop.code if sop else "",
            "title": sop.title if sop else "",
            "version": version.version,
            "state": version.state,
            "file_id": version.file_id,
            "file_checksum": version.file_checksum,
            "requires_training_ack": version.requires_training_ack,
        }

    # ---------- 写 ----------

    def create(self, name: str, plate: int, copy_from: str | None, user: User) -> dict:
        source = self.recipes.get(copy_from) if copy_from else None
        recipe_id = self._next_recipe_id()
        recipe = Recipe(
            id=recipe_id,
            name=name,
            version="0.1.0",
            state="draft",
            owner=user.display_name,
            updated=today_iso(),
            plate=plate,
            steps=copy.deepcopy(source.steps) if source else [],
            bom=copy.deepcopy(source.bom) if source else [],
            history=[{"v": "0.1.0", "state": "draft", "note": "新建", "by": user.display_name, "at": today_iso()}],
            author_user_id=user.id,
            used_step_ids=self._step_ids_of(source) if source else [],
        )
        self.recipes.add(recipe)
        self.audit.record(user, "新建配方", recipe_id, before="—", after="草稿")
        self.db.commit()
        return self.to_dict(recipe, detail=True)

    def _next_recipe_id(self) -> str:
        used = {recipe.id for recipe in self.recipes.list()}
        numbers = [int(rid[2:]) for rid in used if rid.startswith("R-") and rid[2:].isdigit()]
        candidate = max(numbers, default=200) + 1
        while f"R-{candidate}" in used:
            candidate += 1
        return f"R-{candidate}"

    @staticmethod
    def _step_ids_of(recipe: Recipe) -> list[str]:
        ids = set(recipe.used_step_ids or [])
        ids |= {
            str(step.get("step_id"))
            for step in (recipe.steps or [])
            if isinstance(step, dict) and step.get("step_id")
        }
        return sorted(ids)

    def patch(self, recipe_id: str, changes: dict, user: User) -> dict:
        recipe = self._require(recipe_id)
        expected_version = changes.pop("row_version", None)
        if recipe.state not in EDITABLE_STATES:
            raise StateConflict("只有草稿可编辑")
        # 两人同时编辑同一草稿：后提交的一方必须看到冲突，不静默覆盖
        self.recipes.check_version(recipe, expected_version, "方法草稿")
        before = {key: getattr(recipe, key) for key in changes}
        if "steps" in changes:
            # 已用过的 step_id 不复用：删掉的步骤 ID 也记在 used_step_ids 里，
            # 否则删掉 s03 再加一步，新步骤又叫 s03，历史批次里的 s03 就换了意思
            used = set(self._step_ids_of(recipe))
            changes["steps"] = assign_step_ids(changes["steps"], used)
            recipe.used_step_ids = sorted(
                used | {str(step["step_id"]) for step in changes["steps"] if step.get("step_id")}
            )
        if "sop_version_id" in changes and changes["sop_version_id"]:
            version = self.sop_versions.get(changes["sop_version_id"])
            if version is None:
                raise NotFound("SOP 版本不存在")
            if version.state != "published":
                raise ValidationFailed("只能关联已发布的 SOP 版本")
        step_diff = self._step_diff(before.get("steps") or [], changes["steps"]) if "steps" in changes else []
        for key, value in changes.items():
            setattr(recipe, key, value)
        recipe.updated = today_iso()
        self.recipes.bump(recipe)
        if step_diff:
            recipe.diff = step_diff
            recipe.history = [*recipe.history, {
                "v": recipe.version, "state": "draft",
                "note": f"编辑步骤：{len(step_diff)} 处变更",
                "by": user.display_name, "at": today_iso(),
            }]
        meta_detail = "；".join(f"{k}: {before[k]} → {v}" for k, v in changes.items() if k not in {"steps", "bom"})
        self.audit.record(
            user, "编辑配方草稿", recipe_id,
            detail="；".join(filter(None, [
                meta_detail,
                f"步骤 {len(step_diff)} 处变更" if step_diff else "",
                "BOM 已更新" if "bom" in changes else "",
            ])),
        )
        self.db.commit()
        return self.to_dict(recipe, detail=True)

    def _step_diff(self, old: list[dict], new: list[dict]) -> list[list[str]]:
        """逐位比对步骤，生成 +/- 差异行，写入版本历史供评审对照。"""
        def line(step: dict) -> str:
            kind = KIND_NAMES.get(kind_of(step), "设备")
            params = "，".join(f"{k}={v}" for k, v in (step.get("params") or {}).items())
            hard = step.get("hard") or {}
            tail = f" · 硬时限 ≤{hard.get('maxGapMin')} min（自{hard.get('from')}）" if hard else ""
            return (
                f"[{kind}] {step.get('name')} · {step.get('cap') or '—'} · "
                f"{params or '无参数'} · {step.get('dur') or '—'} min{tail}"
            )

        diff: list[list[str]] = []
        for index in range(max(len(old), len(new))):
            a = old[index] if index < len(old) else None
            b = new[index] if index < len(new) else None
            if a == b:
                continue
            if a is not None:
                diff.append(["-", f"第 {index + 1} 步 {line(a)}"])
            if b is not None:
                diff.append(["+", f"第 {index + 1} 步 {line(b)}"])
        return diff

    def submit(self, recipe_id: str, user: User) -> dict:
        recipe = self._require(recipe_id)
        if recipe.state != "draft":
            raise StateConflict("只有草稿可提交评审")
        if not is_valid(self.validation_of(recipe)):
            raise StateConflict("能力校验未通过，已阻止提交")
        if not recipe.steps:
            raise StateConflict("方法没有步骤")

        # 执行前仿真：结构、所有分支路径、可达性、能力与硬时限在空实验室里排得下
        SimulationService(self.db, self.ctx).require_feasible(recipe, "提交评审")
        recipe.state = "review"
        recipe.submitted_by = user.id
        self.recipes.bump(recipe)
        recipe.history = [*recipe.history, {
            "v": recipe.version, "state": "review", "note": "提交评审", "by": user.display_name, "at": today_iso(),
        }]
        self.audit.record(user, "提交配方评审", f"{recipe_id} v{recipe.version}", before="草稿", after="评审中")
        self.db.commit()
        return self.to_dict(recipe, detail=True)

    def transition_with_signature(self, recipe_id: str, target_state: str, signature_id: str, user: User) -> dict:
        recipe = self._require(recipe_id)
        rules = {
            "approved": ("review", "recipe.approve", "批准配方"),
            "released": ("approved", "recipe.release", "发布配方"),
            "retired": ("released", "recipe.release", "退役配方"),
        }
        if target_state not in rules:
            raise StateConflict("不支持的目标状态")
        required_state, permission, action = rules[target_state]
        if not user_may(self.ctx, user, permission):
            raise PermissionDenied(f"当前角色无权限：{permission}")
        if recipe.state != required_state:
            raise StateConflict(f"只有{STATE_LABEL[required_state]}配方可{action[:2]}")
        if target_state != "retired" and not is_valid(self.validation_of(recipe)):
            raise StateConflict("能力校验未通过，已阻止")
        if target_state == "approved":

            # 批准时按当时的工位与能力再跑一次：评审期间工位退役、能力停用都可能让它排不下
            SimulationService(self.db, self.ctx).require_feasible(recipe, "批准")
        if target_state == "approved" and (
            same_person(recipe.author_user_id, user.id) or same_person(recipe.submitted_by, user.id)
        ) and not admin_self_approval(self.db, self.ctx, user, recipe.id, "批准本人编写或提交的方法"):
            # 管理员也不例外：同一个人编写、提交又批准，审批就只剩形式
            raise PermissionDenied(
                "不能批准本人编写或提交的方法（职责分离）", code="self_approval_denied",
            )
        # 签名必须针对这个方法的这个版本：为别的对象或旧版本签的票据不能挪用
        signature = self.identity.consume_signature(
            signature_id, user, action, object_ref=recipe.id, object_version=recipe.row_version,
            strict=True,
        )
        before = recipe.state
        recipe.state = target_state
        self.recipes.bump(recipe)
        superseded = None
        if target_state == "released":
            recipe.needs_revision = False
            superseded = self._retire_parent(recipe, user)
        recipe.history = [*recipe.history, {
            "v": recipe.version, "state": target_state, "note": action, "by": user.display_name, "at": today_iso(),
        }]
        self.audit.record(
            user, action, f"{recipe_id} v{recipe.version}", sign=True, meaning=signature.meaning,
            before=STATE_LABEL[before], after=STATE_LABEL[target_state], signature_id=signature.id,
            object_version=recipe.row_version,
            detail=f"原发布版本 {superseded} 同时退役" if superseded else "",
        )
        self.db.commit()
        return self.to_dict(recipe, detail=True)

    def _retire_parent(self, recipe: Recipe, user: User) -> str | None:
        """修订版发布即取代来源版本：同一方法不能同时有两个「已发布」。"""
        if not recipe.parent:
            return None
        parent = self.recipes.get(recipe.parent)
        if parent is None or parent.state != "released":
            return None
        parent.state = "retired"
        self.recipes.bump(parent)
        parent.history = [*parent.history, {
            "v": parent.version, "state": "retired", "note": f"被修订版 {recipe.id} v{recipe.version} 取代",
            "by": user.display_name, "at": today_iso(),
        }]
        self.audit.record(
            user, "退役配方", f"{parent.id} v{parent.version}", before="已发布", after="已退役",
            detail=f"被修订版 {recipe.id} v{recipe.version} 取代；已建批次仍引用原快照",
        )
        return f"{parent.id} v{parent.version}"

    def create_revision(self, recipe_id: str, user: User) -> dict:
        source = self._require(recipe_id)
        if source.state != "released":
            raise StateConflict("只有已发布配方可新建修订草稿")
        # 修订号取已有最大号 + 1，不按数量算：删掉 r1 后按数量会再造一个 r2 撞主键
        siblings = [
            row.id for row in self.db.query(Recipe).filter(Recipe.id.like(f"{recipe_id}-r%")).all()
        ]
        numbers = [
            int(rid[len(recipe_id) + 2:]) for rid in siblings if rid[len(recipe_id) + 2:].isdigit()
        ]
        sequence = max(numbers, default=0) + 1
        major, minor, _ = (source.version.split(".") + ["0", "0"])[:3]
        # 版本号同理：同一来源的两个修订不能拿到同一个版本号
        sibling_minors = [
            int((row.version.split(".") + ["0", "0"])[1])
            for row in self.db.query(Recipe).filter(Recipe.parent == recipe_id).all()
            if (row.version.split(".") + ["0", "0"])[1].isdigit()
            and row.version.split(".")[0] == major
        ]
        next_minor = max([int(minor), *sibling_minors]) + 1
        version = f"{major}.{next_minor}.0"
        revision = Recipe(
            id=f"{recipe_id}-r{sequence}",
            name=source.name,
            version=version,
            state="draft",
            owner=user.display_name,
            updated=today_iso(),
            plate=source.plate,
            risk=source.risk,
            design=source.design,
            parent=recipe_id,
            sop_version_id=source.sop_version_id,
            bom=copy.deepcopy(source.bom),
            steps=copy.deepcopy(source.steps),
            history=[{"v": version, "state": "draft", "note": f"由 {recipe_id} v{source.version} 派生",
                      "by": user.display_name, "at": today_iso()}],
            author_user_id=user.id,
            used_step_ids=self._step_ids_of(source),
        )
        self.recipes.add(revision)
        self.audit.record(user, "新建修订草稿", revision.id, before=f"{recipe_id} v{source.version}", after="草稿")
        self.db.commit()
        return self.to_dict(revision, detail=True)

    def delete(self, recipe_id: str, user: User) -> dict:
        """删除草稿。出过批次或派生过修订就不行——那份记录已经被别处引用。"""
        recipe = self._require(recipe_id)
        blockers = self.deletable(recipe)
        if blockers:
            raise StateConflict("配方不可删除", {"blocked": [{"key": "recipe", "label": b} for b in blockers]})
        self.audit.record(
            user, "删除配方草稿", f"{recipe_id} v{recipe.version}", before="草稿", after="已删除",
            detail=f"{recipe.name}；{len(recipe.steps or [])} 步，无批次与计划引用",
        )
        self.db.delete(recipe)
        self.db.commit()
        return {"id": recipe_id, "deleted": True}

    def _prime_reference_cache(self, recipes: list[Recipe]) -> None:
        """列表页要给每行算删除守卫，逐行扫全表是 O(n²)，这里一次查完。"""
        children: dict[str, list[str]] = {}
        for recipe in recipes:
            if recipe.parent:
                children.setdefault(recipe.parent, []).append(recipe.id)
        batches: dict[str, list[str]] = {}
        for batch_id, recipe_id in self.batches.recipe_pairs():
            batches.setdefault(recipe_id, []).append(batch_id)
        plans: dict[str, list[str]] = {}
        for plan in self.plans.list():
            plans.setdefault(plan.recipe_id, []).append(plan.id)
        self._reference_cache = {"children": children, "batches": batches, "plans": plans}

    def deletable(self, recipe: Recipe) -> list[str]:
        """给界面用：不能删的理由，空列表表示可删。"""
        cache = getattr(self, "_reference_cache", None)
        if cache:
            children = cache["children"].get(recipe.id, [])
            batch_ids = cache["batches"].get(recipe.id, [])
            plan_ids = cache["plans"].get(recipe.id, [])
        else:
            children = [r.id for r in self.recipes.list() if r.parent == recipe.id]
            batch_ids = self.batches.ids_for_recipe(recipe.id)
            plan_ids = [p.id for p in self.plans.for_recipe(recipe.id)]
        blockers = recipe_delete_blockers(recipe.state, batch_ids, children)
        if plan_ids:
            blockers.append(f"{len(plan_ids)} 个实验计划引用它：{'、'.join(plan_ids[:5])}")
        return blockers

    def set_golden_batch(self, recipe_id: str, batch_id: str, signature_id: str, user: User) -> dict:
        recipe = self._require(recipe_id)
        signature = self.identity.consume_signature(signature_id, user, "设定黄金批次")
        before = recipe.golden_batch_id or "—"
        recipe.golden_batch_id = batch_id
        self.audit.record(
            user, "设定黄金批次", f"{recipe_id} ← {batch_id}", sign=True, meaning=signature.meaning,
            before=before, after=batch_id, signature_id=signature.id,
        )
        self.db.commit()
        return self.to_dict(recipe, detail=True)

    def _require(self, recipe_id: str) -> Recipe:
        recipe = self.recipes.get(recipe_id)
        if not recipe:
            raise NotFound("方法不存在")
        return recipe
