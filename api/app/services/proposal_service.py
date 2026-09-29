"""闭环实验：外部优化器提交下一轮提案，系统校验后生成方案草稿；训练数据快照与分析运行记录。

提案不是指令：校验通过只生成一份方案草稿，仍要锁定、提交、由 QA 批准后才能建批次。
自动批准要等 QA 定好设计空间与策略后再开，这里不提供。

训练数据要能复现：直接导出读的是「此刻」审核通过、质量有效的当前结果版本，结果以后被更正，同一个入口
导出的就变了。所以训练用快照——固化纳入的结果版本清单、排除清单、数据行与原始文件摘要，按快照导出永远一样；
分析运行记下输入哪份快照、什么程序与模型版本、参数与随机种子；提案挂在运行上。
这样才答得出「这轮参数为什么这样选、用了哪批数据、后来更正的数据是否影响了当时的建议」。
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
from ..domain.statistics import EXCLUSION_REASONS
from ..models import (
    AnalysisRun, Batch, DatasetSnapshot, FileObject, MetricDefinition, PhysicalSample, Plan, PlanProposal,
    ResultValue, Sample, User,
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
            "analysis_run_id": row.analysis_run_id or "",
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

    def require_service_grant(self, plan_id: str) -> None:
        """服务身份读快照：与提交提案同一授权（plan_proposals）。"""
        self._authorize(self._require_plan(plan_id), None)

    def submit(self, plan_id: str, payload: dict, user: User | None = None) -> dict:
        plan = self._require_plan(plan_id)
        submitter = self._authorize(plan, user)
        body = {key: payload.get(key) for key in ("points", "repeats", "rationale", "model_version", "source")}
        if payload.get("analysis_run_id"):
            # 只在挂了分析运行时计入摘要：升级前提交的提案按原样重发，摘要不变、照常回放
            body["analysis_run_id"] = payload["analysis_run_id"]
        digest = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
        existing = self.proposals.find(plan.id, payload["proposal_id"])
        if existing is not None:
            if existing.digest != digest:
                raise StateConflict("同一提案编号的内容与首次提交不一致", code="proposal_conflict")
            return {**self.out(existing), "replayed": True}

        run_id = str(payload.get("analysis_run_id") or "")
        if run_id:
            run = self.db.get(AnalysisRun, run_id)
            if run is None or run.org_id != self.ctx.org_id:
                raise NotFound("分析运行不存在")
            if run.root_plan_id != self._root_of(plan).id:
                raise StateConflict("分析运行属于别的实验活动，不能作为这份提案的依据", code="analysis_run_foreign")
        issues, points = self._validate(plan, payload)
        row = PlanProposal(
            org_id=self.ctx.org_id, plan_id=plan.id, proposal_key=payload["proposal_id"], digest=digest,
            source=payload.get("source") or "", model_version=payload.get("model_version") or "",
            rationale=payload.get("rationale") or "", points=payload.get("points") or [],
            state="rejected" if issues else "accepted", issues=issues, created_by=submitter,
            analysis_run_id=run_id,
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

    def _root_of(self, plan: Plan) -> Plan:
        root = plan
        while root.parent_plan_id:
            parent = self.plans.get(root.parent_plan_id)
            if parent is None:
                break
            root = parent
        return root

    def campaign_plan_ids(self, plan_id: str) -> list[str]:
        """同一实验活动的全部方案：沿父链找到根，再收集所有后代。"""
        root = self._root_of(self._require_plan(plan_id))
        ids, frontier = [root.id], [root.id]
        while frontier:
            children = [
                row.id for row in self.plans.query().filter(Plan.parent_plan_id.in_(frontier)).all()
            ]
            frontier = [cid for cid in children if cid not in ids]
            ids += frontier
        return ids

    HEADER = [
        "plan_id", "round_no", "batch_id", "sample_id", "physical_sample_id", "parent_physical_sample_id",
        "well", "condition_group", "is_control",
    ]
    TAIL = ["metric_code", "metric_version", "value", "unit", "result_value_id", "result_version"]

    def _dataset(self, plan_id: str) -> dict:
        """实验活动此刻的正式数据：纳入复核通过、质量有效、未被取代的当前版本，其余结果列出排除原因。"""
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
            ResultValue.org_id == self.ctx.org_id, ResultValue.assignment_id.in_(list(samples) or [""]),
        ).all()
        metrics = {m.id: m for m in self.db.query(MetricDefinition).filter(MetricDefinition.org_id == self.ctx.org_id)}
        header = [*self.HEADER, *[f"factor:{name}" for name in factor_names], *self.TAIL]
        rows, included, exclusions = [], [], []
        for value in sorted(values, key=lambda v: (v.assignment_id, v.metric_definition_id, v.result_version)):
            reason = _exclusion(value)
            if reason:
                exclusions.append({
                    "result_value_id": value.id, "result_version": value.result_version,
                    "reason": reason, "label": EXCLUSION_REASONS.get(reason, reason),
                })
                continue
            sample = samples[value.assignment_id]
            batch = batches[sample.batch_id]
            plan = plans.get(batch.plan_id)
            names = [f.get("name") for f in (plan.factors if plan else [])]
            levels = dict(zip(names, sample.levels or []))
            physical = self.db.get(PhysicalSample, sample.physical_sample_id) if sample.physical_sample_id else None
            metric = metrics.get(value.metric_definition_id)
            cells = [
                batch.plan_id, plan.round_no if plan else "", batch.id, sample.id, sample.physical_sample_id,
                physical.parent_id if physical and physical.parent_id else "", sample.well,
                sample.condition_group, int(bool(sample.is_control)), *[levels.get(name, "") for name in factor_names],
                metric.code if metric else value.metric_definition_id, metric.version if metric else "",
                value.value_num if value.value_num is not None else value.value_text, value.unit,
                value.id, value.result_version,
            ]
            rows.append(dict(zip(header, cells)))
            included.append(value)
        return {"plan_ids": plan_ids, "header": header, "rows": rows, "included": included, "exclusions": exclusions}

    @staticmethod
    def _csv(header: list[str], rows: list[dict]) -> str:
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(header)
        for row in rows:
            writer.writerow([row.get(column, "") for column in header])
        return buffer.getvalue()

    def dataset_csv(self, plan_id: str) -> str:
        """此刻的正式数据（一行一个结果）。不可复现：结果以后被更正，这里导出的就变了；训练请用快照。"""
        data = self._dataset(plan_id)
        return self._csv(data["header"], data["rows"])

    # ---------- 数据集快照 ----------

    def snapshot_out(self, row: DatasetSnapshot, detail: bool = False) -> dict:
        out = {
            "id": row.id, "plan_id": row.plan_id, "root_plan_id": row.root_plan_id, "key": row.snapshot_key,
            "digest": row.digest, "row_count": len(row.rows or []), "excluded_count": len(row.exclusions or []),
            "file_count": len(row.files or []), "note": row.note, "created_by": row.created_by,
            "created_at": row.created_at.isoformat(timespec="seconds"), "filters": row.filters or {},
        }
        if detail:
            out["changed"] = self._changed_since(row)
            out["exclusions"] = row.exclusions or []
            out["files"] = row.files or []
        return out

    def _changed_since(self, row: DatasetSnapshot) -> list[dict]:
        """快照里的结果后来变了的：被更正版本取代、复核退回、质量改判。快照本身不改，只如实列出。"""
        ids = [item["result_value_id"] for item in row.result_versions or []]
        current = {
            value.id: value for value in self.db.query(ResultValue).filter(ResultValue.id.in_(ids or [""]))
        }
        changed = []
        for item in row.result_versions or []:
            value = current.get(item["result_value_id"])
            reason = "missing" if value is None else _exclusion(value)
            if reason:
                changed.append({
                    **item, "reason": reason,
                    "label": "结果记录已不存在" if value is None else EXCLUSION_REASONS.get(reason, reason),
                    "superseded_by_id": value.superseded_by_id if value is not None else "",
                })
        return changed

    def _require_snapshot(self, plan_id: str, snapshot_id: str) -> DatasetSnapshot:
        plan = self._require_plan(plan_id)
        row = self.db.get(DatasetSnapshot, snapshot_id)
        if row is None or row.org_id != self.ctx.org_id or row.root_plan_id != self._root_of(plan).id:
            raise NotFound("数据集快照不存在")
        return row

    def create_snapshot(self, plan_id: str, payload: dict, user: User | None = None) -> dict:
        plan = self._require_plan(plan_id)
        creator = self._authorize(plan, user)
        root = self._root_of(plan)
        key = str(payload.get("key") or "").strip()
        data = self._dataset(plan_id)
        digest = hashlib.sha256(
            json.dumps(data["rows"], sort_keys=True, ensure_ascii=False, default=str).encode()
        ).hexdigest()
        if key:
            existing = self.db.query(DatasetSnapshot).filter(
                DatasetSnapshot.org_id == self.ctx.org_id, DatasetSnapshot.root_plan_id == root.id,
                DatasetSnapshot.snapshot_key == key,
            ).first()
            if existing is not None:
                if existing.digest != digest:
                    raise StateConflict(
                        "同一快照编号已固化过，内容与此刻的数据不一致：请换一个编号固化新快照",
                        code="snapshot_key_conflict",
                    )
                return {**self.snapshot_out(existing), "replayed": True}
        files = []
        for file_id in sorted({value.raw_file_id for value in data["included"] if value.raw_file_id}):
            stored = self.db.get(FileObject, file_id)
            files.append({"file_id": file_id, "checksum": stored.checksum if stored is not None else ""})
        row = DatasetSnapshot(
            org_id=self.ctx.org_id, plan_id=plan.id, root_plan_id=root.id, snapshot_key=key,
            filters={"plans": data["plan_ids"], "review_state": "approved", "quality": "valid", "current_only": True},
            result_versions=[
                {"result_value_id": value.id, "result_version": value.result_version} for value in data["included"]
            ],
            exclusions=data["exclusions"], rows=data["rows"], files=files, digest=digest,
            note=str(payload.get("note") or ""), created_by=creator,
        )
        self.db.add(row)
        self.db.flush()
        self.audit.record(
            user, "固化训练数据快照", plan.id, before="—", after=f"快照 {row.id[:8]}",
            detail=(
                f"{len(data['rows'])} 条正式结果、{len(data['exclusions'])} 条排除、{len(files)} 个原始文件；"
                f"内容摘要 {digest[:12]}"
            ),
        )
        self.db.commit()
        return {**self.snapshot_out(row), "replayed": False}

    def snapshots(self, plan_id: str) -> list[dict]:
        root = self._root_of(self._require_plan(plan_id))
        rows = self.db.query(DatasetSnapshot).filter(
            DatasetSnapshot.org_id == self.ctx.org_id, DatasetSnapshot.root_plan_id == root.id,
        ).order_by(DatasetSnapshot.created_at.desc()).all()
        return [{**self.snapshot_out(row), "changed_count": len(self._changed_since(row))} for row in rows]

    def snapshot(self, plan_id: str, snapshot_id: str) -> dict:
        return self.snapshot_out(self._require_snapshot(plan_id, snapshot_id), detail=True)

    def snapshot_csv(self, plan_id: str, snapshot_id: str) -> str:
        """按快照导出：读快照里固化的数据行，不回头查结果表，结果后来怎么变都不影响。"""
        row = self._require_snapshot(plan_id, snapshot_id)
        rows = row.rows or []
        header = list(rows[0]) if rows else [*self.HEADER, *self.TAIL]
        return self._csv(header, rows)

    # ---------- 分析运行 ----------

    def run_out(self, row: AnalysisRun) -> dict:
        return {
            "id": row.id, "plan_id": row.plan_id, "run_id": row.run_key, "snapshot_id": row.snapshot_id,
            "program": row.program, "program_version": row.program_version, "model_version": row.model_version,
            "params": row.params or {}, "seed": row.seed, "outputs": row.outputs or {},
            "created_by": row.created_by, "created_at": row.created_at.isoformat(timespec="seconds"),
        }

    def record_run(self, plan_id: str, payload: dict, user: User | None = None) -> dict:
        plan = self._require_plan(plan_id)
        creator = self._authorize(plan, user)
        snapshot = self._require_snapshot(plan_id, str(payload.get("snapshot_id") or ""))
        body = {
            key: payload.get(key)
            for key in ("snapshot_id", "program", "program_version", "model_version", "params", "seed", "outputs")
        }
        digest = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
        existing = self.db.query(AnalysisRun).filter(
            AnalysisRun.org_id == self.ctx.org_id, AnalysisRun.plan_id == plan.id,
            AnalysisRun.run_key == payload["run_id"],
        ).first()
        if existing is not None:
            if existing.digest != digest:
                raise StateConflict("同一运行编号的内容与首次登记不一致", code="analysis_run_conflict")
            return {**self.run_out(existing), "replayed": True}
        row = AnalysisRun(
            org_id=self.ctx.org_id, plan_id=plan.id, root_plan_id=snapshot.root_plan_id, run_key=payload["run_id"],
            snapshot_id=snapshot.id, program=payload.get("program") or "",
            program_version=payload.get("program_version") or "", model_version=payload.get("model_version") or "",
            params=payload.get("params") or {}, seed=str(payload.get("seed") or ""),
            outputs=payload.get("outputs") or {}, digest=digest, created_by=creator,
        )
        self.db.add(row)
        self.db.flush()
        self.audit.record(
            user, "登记分析运行", plan.id, before="—", after=f"运行 {row.run_key}",
            detail=(
                f"输入快照 {snapshot.id[:8]}（摘要 {snapshot.digest[:12]}）；程序 {row.program or '—'} "
                f"{row.program_version}；模型 {row.model_version or '—'}；随机种子 {row.seed or '—'}"
            ),
        )
        self.db.commit()
        return {**self.run_out(row), "replayed": False}

    def runs(self, plan_id: str) -> list[dict]:
        root = self._root_of(self._require_plan(plan_id))
        rows = self.db.query(AnalysisRun).filter(
            AnalysisRun.org_id == self.ctx.org_id, AnalysisRun.root_plan_id == root.id,
        ).order_by(AnalysisRun.created_at.desc()).all()
        return [self.run_out(row) for row in rows]


def _exclusion(value: ResultValue) -> str:
    """没纳入训练数据的原因；纳入返回空串。纳入口径与原来的导出一致（复核通过、质量有效、当前版本），
    原因用正式统计的同一套措辞。"""
    if value.superseded_by_id:
        return "superseded"
    if value.review_state == "pending":
        return "pending_review"
    if value.review_state != "approved":
        return "rejected_review"
    if value.quality != "valid":
        return value.quality if value.quality in EXCLUSION_REASONS else "unassessed"
    return ""
