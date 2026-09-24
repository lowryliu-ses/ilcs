"""闭环实验：外部优化器提交下一轮提案，系统校验后生成方案草稿；以及训练数据导出。

提案不是指令：校验通过只生成一份方案草稿，仍要锁定、提交、由 QA 批准后才能建批次。
自动批准要等 QA 定好设计空间与策略后再开，这里不提供。
"""
from __future__ import annotations

import csv
import hashlib
import io
import json

from sqlalchemy.orm import Session

from ..core.clock import today_iso
from ..core.context import AccessContext
from ..core.errors import NotFound, PermissionDenied, StateConflict, ValidationFailed
from ..domain import matrix
from ..domain.access import service_may_propose
from ..domain.steps import normalize
from ..models import (
    Batch, MetricDefinition, PhysicalSample, Plan, PlanProposal, ResultValue, Sample, User,
)
from ..repositories.base import ScopedRepository
from ..repositories.recipes import PlanRepository, RecipeRepository
from ..repositories.resources import StationRepository
from .audit_service import AuditService
from .identity_service import user_may


class ProposalRepository(ScopedRepository[PlanProposal]):
    model = PlanProposal

    def find(self, plan_id: str, key: str) -> PlanProposal | None:
        return self.query().filter(PlanProposal.plan_id == plan_id, PlanProposal.proposal_key == key).first()

    def for_plan(self, plan_id: str) -> list[PlanProposal]:
        return list(
            self.query().filter(PlanProposal.plan_id == plan_id).order_by(PlanProposal.created_at.desc()).all()
        )


class ProposalService:
    def __init__(self, db: Session, ctx: AccessContext):
        self.db = db
        self.ctx = ctx
        self.plans = PlanRepository(db, ctx)
        self.recipes = RecipeRepository(db, ctx)
        self.proposals = ProposalRepository(db, ctx)
        self.audit = AuditService(db, ctx)

    def out(self, row: PlanProposal) -> dict:
        return {
            "id": row.id, "plan_id": row.plan_id, "proposal_id": row.proposal_key, "source": row.source,
            "model_version": row.model_version, "rationale": row.rationale, "points": row.points,
            "state": row.state, "issues": row.issues or [], "created_plan_id": row.created_plan_id,
            "created_at": row.created_at.isoformat(timespec="seconds"),
        }

    def list(self, plan_id: str) -> list[dict]:
        self._require_plan(plan_id)
        return [self.out(row) for row in self.proposals.for_plan(plan_id)]

    def _require_plan(self, plan_id: str) -> Plan:
        plan = self.plans.get(plan_id)
        if plan is None:
            raise NotFound("实验方案不存在")
        return plan

    def _authorize(self, plan: Plan, user: User | None) -> str:
        if self.ctx.is_service:
            if not service_may_propose(self.ctx.scopes, plan.id):
                raise PermissionDenied("该服务身份没有向此方案提交提案的授权")
            return f"service:{self.ctx.subject_label or self.ctx.subject_id}"
        if user is None or not user_may(self.ctx, user, "plan.edit"):
            raise PermissionDenied("当前角色不能提交实验提案")
        return user.id

    def submit(self, plan_id: str, payload: dict, user: User | None = None) -> dict:
        plan = self._require_plan(plan_id)
        submitter = self._authorize(plan, user)
        body = {key: payload.get(key) for key in ("points", "repeats", "rationale", "model_version", "source")}
        digest = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
        existing = self.proposals.find(plan.id, payload["proposal_id"])
        if existing is not None:
            if existing.digest != digest:
                raise StateConflict("同一提案编号的内容与首次提交不一致", code="proposal_conflict")
            return {**self.out(existing), "replayed": True}

        issues, points = self._validate(plan, payload)
        row = PlanProposal(
            org_id=self.ctx.org_id, plan_id=plan.id, proposal_key=payload["proposal_id"], digest=digest,
            source=payload.get("source") or "", model_version=payload.get("model_version") or "",
            rationale=payload.get("rationale") or "", points=payload.get("points") or [],
            state="rejected" if issues else "accepted", issues=issues, created_by=submitter,
        )
        self.proposals.add(row)
        if issues:
            self.audit.record(
                None, "拒绝实验提案", plan.id, before="—", after="已拒绝",
                detail=f"{row.source or submitter} 提案 {row.proposal_key}：{'；'.join(issues[:5])}",
            )
            self.db.commit()
            raise ValidationFailed(
                "提案不在已批准的设计空间内，未生成方案", {"issues": issues, "proposal": self.out(row)},
                code="proposal_rejected",
            )
        draft = self._draft_from(plan, points, payload, row)
        row.created_plan_id = draft.id
        self.audit.record(
            None, "接受实验提案", plan.id, before="—", after=f"生成草稿 {draft.id}",
            detail=(
                f"{row.source or submitter} 提案 {row.proposal_key}（模型 {row.model_version or '—'}）："
                f"{len(points)} 个设计点；草稿仍需锁定、提交并由 QA 批准"
            ),
        )
        self.db.commit()
        return {**self.out(row), "replayed": False}

    def _validate(self, plan: Plan, payload: dict) -> tuple[list[str], list[list]]:
        issues: list[str] = []
        if plan.plan_type != "matrix":
            return ["只有矩阵方案可以接收提案"], []
        if plan.approval_state != "approved":
            # 设计空间随审批冻结；没批准的方案，边界本身还没人签字
            return ["来源方案尚未批准，设计空间未冻结"], []
        if not plan.design_space:
            return ["来源方案没有设置设计空间，不能接收外部提案"], []
        factors = plan.factors or []
        names = [factor.get("name") for factor in factors]
        points: list[list] = []
        for number, raw in enumerate(payload.get("points") or [], start=1):
            missing = [name for name in names if name not in raw]
            extra = [key for key in raw if key not in names]
            if missing or extra:
                issues.append(
                    f"第 {number} 个点"
                    + (f"缺少因子 {'、'.join(missing)}" if missing else "")
                    + (f"有未知因子 {'、'.join(extra)}" if extra else "")
                )
                continue
            points.append([raw[name] for name in names])
        issues += matrix.point_issues(factors, points, plan.design_space) if points else []
        recipe = self.recipes.get(plan.recipe_id)
        repeats = payload.get("repeats") or plan.repeats
        if recipe is not None and len(points) * max(1, repeats) > recipe.plate:
            issues.append(f"{len(points)} 个点 × {repeats} 次重复超过流程每批 {recipe.plate} 个样品位")
        if points and recipe is not None:
            proposed = [
                {**factor, "levels": sorted({point[index] for point in points})}
                for index, factor in enumerate(factors)
            ]
            issues += matrix.target_issues(
                proposed, normalize(recipe.steps or []), StationRepository(self.db, self.ctx).specs(),
            )
        return issues, points

    def _draft_from(self, plan: Plan, points: list[list], payload: dict, proposal: PlanProposal) -> Plan:
        from .plan_service import PlanService

        factors = [
            {**factor, "levels": sorted({point[index] for point in points})}
            for index, factor in enumerate(plan.factors or [])
        ]
        round_no = (plan.round_no or 1) + 1
        draft = Plan(
            id=PlanService(self.db, self.ctx)._next_plan_id(plan.recipe_id),
            org_id=self.ctx.org_id, project_id=plan.project_id,
            name=f"{plan.name} · 第 {round_no} 轮",
            recipe_id=plan.recipe_id, owner=plan.owner, state="draft", approval_state="draft",
            plan_type="matrix", version=1, created=today_iso(),
            goal=(f"由 {proposal.source or '提案方'} 提出（模型 {proposal.model_version or '—'}）："
                  f"{proposal.rationale}")[:2000],
            repeats=payload.get("repeats") or plan.repeats, layout=plan.layout, seed=plan.seed + round_no,
            factors=factors, control=None, design_points=points, design_space=plan.design_space,
            required_metrics=plan.required_metrics or [], resource_requirements=plan.resource_requirements or [],
            method_version=plan.method_version, parent_plan_id=plan.id, round_no=round_no,
        )
        self.plans.add(draft)
        return draft

    # ---------- 训练数据导出 ----------

    def campaign_plan_ids(self, plan_id: str) -> list[str]:
        """同一实验活动的全部方案：沿父链找到根，再收集所有后代。"""
        root = self._require_plan(plan_id)
        while root.parent_plan_id:
            parent = self.plans.get(root.parent_plan_id)
            if parent is None:
                break
            root = parent
        ids, frontier = [root.id], [root.id]
        while frontier:
            children = [
                row.id for row in self.plans.query().filter(Plan.parent_plan_id.in_(frontier)).all()
            ]
            frontier = [cid for cid in children if cid not in ids]
            ids += frontier
        return ids

    def dataset_csv(self, plan_id: str) -> str:
        """一行一个正式结果：条件、样本谱系、结果版本。只导出复核通过且质量有效的当前版本。"""
        plan_ids = self.campaign_plan_ids(plan_id)
        plans = {pid: self.plans.get(pid) for pid in plan_ids}
        factor_names: list[str] = []
        for plan in plans.values():
            for factor in plan.factors or []:
                if factor.get("name") not in factor_names:
                    factor_names.append(factor.get("name"))
        batches = {
            b.id: b for b in self.db.query(Batch).filter(
                Batch.org_id == self.ctx.org_id, Batch.plan_id.in_(plan_ids),
            )
        }
        samples = {
            s.id: s for s in self.db.query(Sample).filter(
                Sample.org_id == self.ctx.org_id, Sample.batch_id.in_(list(batches) or [""]),
            )
        }
        values = self.db.query(ResultValue).filter(
            ResultValue.org_id == self.ctx.org_id,
            ResultValue.assignment_id.in_(list(samples) or [""]),
            ResultValue.review_state == "approved",
            ResultValue.quality == "valid",
            ResultValue.superseded_by_id == "",
        ).all()
        metrics = {m.id: m for m in self.db.query(MetricDefinition).filter(MetricDefinition.org_id == self.ctx.org_id)}
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow([
            "plan_id", "round_no", "batch_id", "sample_id", "physical_sample_id", "parent_physical_sample_id",
            "well", "condition_group", "is_control", *[f"factor:{name}" for name in factor_names],
            "metric_code", "metric_version", "value", "unit", "result_value_id", "result_version",
        ])
        for value in sorted(values, key=lambda v: (v.assignment_id, v.metric_definition_id)):
            sample = samples[value.assignment_id]
            batch = batches[sample.batch_id]
            plan = plans.get(batch.plan_id)
            names = [f.get("name") for f in (plan.factors if plan else [])]
            levels = dict(zip(names, sample.levels or []))
            physical = self.db.get(PhysicalSample, sample.physical_sample_id) if sample.physical_sample_id else None
            metric = metrics.get(value.metric_definition_id)
            writer.writerow([
                batch.plan_id, plan.round_no if plan else "", batch.id, sample.id, sample.physical_sample_id,
                physical.parent_id if physical and physical.parent_id else "", sample.well,
                sample.condition_group, int(bool(sample.is_control)), *[levels.get(name, "") for name in factor_names],
                metric.code if metric else value.metric_definition_id, metric.version if metric else "",
                value.value_num if value.value_num is not None else value.value_text, value.unit,
                value.id, value.result_version,
            ])
        return buffer.getvalue()
