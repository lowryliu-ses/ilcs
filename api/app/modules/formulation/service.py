"""配液模板与实验表格导入：模板维护、表格解析与预览、一次导入生成样本 + 流程草稿 + 方案草稿。

- 模板不走发布：它只决定怎么生成流程草稿，生成的流程照旧走 评审 → 批准 → 发布，那才是受控点。
  改模板要带 row_version（乐观锁）并留审计；配置在创建与修改时整体校验，有问题 422 列出全部问题。
- 预览不写库；导入时服务端按同一规则重新生成，是最终裁决，不信任前端给的预览。
- 同结构的配方表（加哪几种料、什么顺序都一样，只是量不同）沿用已有流程：每瓶的量在方案因子上，
  流程不变就不必重新评审。沿用优先已发布 > 已批准 > 评审中 > 草稿。
- 导入本身不提交、不批准：之后仍由人提交流程评审 → QA 批准发布 → 锁定并提交方案 → QA 批准方案 → 建批次。
- 上游系统（AI 配方预测、实验设计平台）用服务身份提交（`submit`）：授权范围 `formulation_imports` 列出它能向哪些模板
  提交，按它给的请求编号去重；生成的同样只是草稿，评审、批准、建批次、签名下发照旧由人做（提交方不是审批人）。
  提交方按请求编号查进度（`progress`）：流程与方案的审批状态、实验任务、批次，已复核的结果标明是否进正式统计。
- 一瓶一配方：表里的序列号是要配液的空瓶。已登记、还没进过任何批次的沿用（提醒一句）；
  已经在某个批次里有运行分配（配过液）的整张表拒绝——跑批次不改物理样本的状态，只能按运行分配判断。
  预览就把这些列出来，导入时再按同一规则核一次。
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any

from sqlalchemy.orm import Session

from ...core.clock import now
from ...core.context import AccessContext
from ...core.errors import NotFound, PermissionDenied, StateConflict, ValidationFailed
from .spreadsheet import SpreadsheetError, check_limits, read_sheet
from ...domain.recipe_rules import validate_steps
from ...domain.steps import normalize
from ...models import PhysicalSample, User
from ...repositories.materials import MaterialRepository
from ...repositories.methods import DeviceMethodRepository
from ...repositories.metrics import MetricRepository
from ...repositories.recipes import RecipeRepository
from ...repositories.resources import CapabilityRepository, StationRepository
from ...repositories.samples import PhysicalSampleRepository
from ...services.audit_service import AuditService
from ...services.sample_service import UNUSABLE_SAMPLE
from . import rules
from .models import FormulationSubmission, FormulationTemplate
from .repository import FormulationSubmissionRepository, FormulationTemplateRepository

CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,63}$")
STATE_LABEL = {"active": "在用", "retired": "已退役"}
EDITABLE = ("name", "description", "config")
# 沿用已有流程时的优先顺序：已经审过的优先，省掉重新评审
REUSE_RANK = {"released": 0, "approved": 1, "review": 2, "draft": 3}


def _steps_key(steps: list[dict]) -> str:
    return json.dumps(normalize(steps or []), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class FormulationService:
    def __init__(self, db: Session, ctx: AccessContext):
        self.db = db
        self.ctx = ctx
        self.templates = FormulationTemplateRepository(db, ctx)
        self.submissions = FormulationSubmissionRepository(db, ctx)
        self.capabilities = CapabilityRepository(db)
        self.methods = DeviceMethodRepository(db, ctx)
        self.metrics = MetricRepository(db, ctx)
        self.materials = MaterialRepository(db, ctx)
        self.recipes = RecipeRepository(db, ctx)
        self.stations = StationRepository(db, ctx)
        self.samples = PhysicalSampleRepository(db, ctx)
        self.audit = AuditService(db, ctx)

    # ---------- 读 ----------

    def _require(self, template_id: str) -> FormulationTemplate:
        template = self.templates.get(template_id)
        if template is None:
            raise NotFound("配液模板不存在")
        return template

    def _active(self, template_id: str) -> FormulationTemplate:
        template = self._require(template_id)
        if template.state != "active":
            raise StateConflict(f"配液模板 {template.code} 已退役，不能再用于导入", code="formulation_template_retired")
        return template

    def problems(self, config: Any) -> list[str]:
        """模板配置在当前主数据下的问题：能力、已发布方法、有效指标都按现在的状态核对。"""
        released = {
            method.id: {"capability_id": method.capability_id, "code": method.code, "name": method.name}
            for method in self.methods.list(state="released")
        }
        metrics = {metric.id for metric in self.metrics.active()}
        return rules.template_issues(config, self.capabilities.specs(), released, metrics)

    def out(self, template: FormulationTemplate, *, full: bool = False) -> dict[str, Any]:
        config = template.config or {}
        row = {
            "id": template.id, "code": template.code, "name": template.name, "description": template.description,
            "state": template.state, "state_label": STATE_LABEL.get(template.state, template.state),
            # 界面按它画实验参数输入与「模板规则」面板，列表里也带上
            "config": config,
            "created_by_name": template.created_by_name,
            "created_at": template.created_at.isoformat(timespec="seconds") if template.created_at else None,
            "updated_at": template.updated_at.isoformat(timespec="seconds") if template.updated_at else None,
            "row_version": template.row_version,
        }
        if full:
            problems = self.problems(config)
            row["check"] = {"ok": not problems, "problems": problems}
            row["param_specs"] = self.param_specs(config)
        return row

    def param_specs(self, config: Any) -> dict[str, dict[str, Any]]:
        """实验参数与逐瓶参数作用的那个能力参数的规格（类型、选项、单位）：界面据此给选项型参数画下拉。"""
        from ...domain.params import spec_of

        if not isinstance(config, dict):
            return {}
        capabilities = self.capabilities.specs()
        fixed = {
            step.get("key"): step for _, steps in rules.fixed_sections(config) for step in (steps if isinstance(steps, list) else [])
            if isinstance(step, dict) and step.get("key")
        }
        specs: dict[str, dict[str, Any]] = {}
        for row in [*(config.get("experiment_params") or []), *(config.get("row_params") or [])]:
            if not isinstance(row, dict) or not row.get("key"):
                continue
            step = fixed.get(row.get("step")) or {}
            capability = capabilities.get(step.get("cap"))
            if capability is None or row.get("param") not in (capability.get("params") or {}):
                continue
            spec = spec_of(capability, row["param"])
            specs[row["key"]] = {"type": spec["type"], "unit": spec["unit"], "options": spec["options"], "label": spec["label"]}
        return specs

    @staticmethod
    def read_table(filename: str, data: bytes) -> dict[str, Any]:
        try:
            sheet = read_sheet(filename, data)
        except SpreadsheetError as exc:
            raise ValidationFailed(str(exc), code="spreadsheet_invalid") from exc
        return {"filename": filename, "table": sheet.rows, "warnings": list(sheet.warnings)}

    def check(self, payload: dict[str, Any]) -> dict[str, Any]:
        """编辑器用：核对还没保存的配置；带了表格就按它试算一次（与导入同一套生成规则，不写库）。"""
        config = payload.get("config") or {}
        problems = self.problems(config)
        out: dict[str, Any] = {"ok": not problems, "problems": problems, "param_specs": self.param_specs(config)}
        if payload.get("table") is not None and isinstance(config, dict):
            draft = FormulationTemplate(
                id="", org_id=self.ctx.org_id, code="", name=str(payload.get("name") or "模板试算"),
                description=str(payload.get("description") or ""), config=config, state="active",
            )
            out["preview"] = self._generate(draft, str(payload.get("filename") or "试算表格"), payload["table"],
                                            payload.get("params") or {}, samples=False)
        return out

    def list(self, state: str | None = None) -> list[dict[str, Any]]:
        return [self.out(template) for template in self.templates.list(state)]

    def get(self, template_id: str) -> dict[str, Any]:
        return self.out(self._require(template_id), full=True)

    # ---------- 模板维护 ----------

    def _validated(self, config: Any) -> dict[str, Any]:
        problems = self.problems(config)
        if problems:
            raise ValidationFailed(
                f"配液模板配置有 {len(problems)} 处问题：{problems[0]}", {"problems": problems},
                code="formulation_template_invalid",
            )
        return copy.deepcopy(config)

    def create(self, payload: dict[str, Any], user: User) -> dict[str, Any]:
        code = str(payload.get("code") or "").strip()
        if not CODE.fullmatch(code):
            raise ValidationFailed("模板编号 2–64 位，字母数字开头，只含字母、数字、点、横线、下划线",
                                   code="formulation_template_code_invalid")
        name = str(payload.get("name") or "").strip()
        if not name:
            raise ValidationFailed("配液模板要有名称", code="formulation_template_invalid")
        if self.templates.by_code(code):
            raise StateConflict(f"配液模板编号 {code} 已存在", code="formulation_template_code_taken")
        template = FormulationTemplate(
            org_id=self.ctx.org_id, code=code, name=name, description=str(payload.get("description") or ""),
            config=self._validated(payload.get("config")), state="active", created_by=user.id,
            created_by_name=user.display_name, created_at=now(), updated_at=now(),
        )
        self.templates.add(template)
        self.audit.record(user, "新建配液模板", template.id, before="—", after=f"{code} 在用",
                          detail=name, object_version=template.row_version)
        self.db.commit()
        return self.get(template.id)

    def update(self, template_id: str, changes: dict[str, Any], expected: int | None, user: User) -> dict[str, Any]:
        template = self._require(template_id)
        self.templates.check_version(template, expected, "配液模板")
        if template.state != "active":
            raise StateConflict("已退役的配液模板不能修改", code="formulation_template_retired")
        values = {key: changes[key] for key in EDITABLE if changes.get(key) is not None}
        if "name" in values:
            values["name"] = str(values["name"]).strip()
            if not values["name"]:
                raise ValidationFailed("配液模板要有名称", code="formulation_template_invalid")
        if "config" in values:
            values["config"] = self._validated(values["config"])
        for key, value in values.items():
            setattr(template, key, value)
        template.updated_at = now()
        self.templates.bump(template)
        self.audit.record(user, "修改配液模板", template.id, after=template.code,
                          detail="、".join(sorted(values)) or "无字段变化", object_version=template.row_version)
        self.db.commit()
        return self.get(template.id)

    def retire(self, template_id: str, expected: int | None, user: User) -> dict[str, Any]:
        template = self._require(template_id)
        self.templates.check_version(template, expected, "配液模板")
        if template.state != "active":
            raise StateConflict("配液模板已退役", code="formulation_template_retired")
        template.state = "retired"
        template.updated_at = now()
        self.templates.bump(template)
        self.audit.record(user, "退役配液模板", template.id, before="在用", after="已退役",
                          detail=template.code, object_version=template.row_version)
        self.db.commit()
        return self.get(template.id)

    # ---------- 解析与预览 ----------

    def catalog(self) -> dict[str, dict[str, Any]]:
        """试剂目录：物料主数据的名称 → 类别、基本单位与单位换算（分装量核对按登记的密度估体积）。
        同名多条（不同单位登记）时取有类别的那条。"""
        catalog: dict[str, dict[str, Any]] = {}
        for material in self.materials.list():
            if material.state != "active":
                continue
            if material.name in catalog and catalog[material.name]["category"]:
                continue
            catalog[material.name] = {
                "category": material.category or "", "base_unit": material.base_unit or "",
                "conversions": dict(material.conversions or {}),
            }
        return catalog

    def _generate(self, template: FormulationTemplate, filename: str, table: list[list[Any]],
                  params: dict[str, Any], *, sheet_warnings: list[str] = (), samples: bool = True) -> dict[str, Any]:
        """生成预览。`samples` 为真时把瓶子能不能用（配过液、报废、编号被占用）也列进 issues / warnings；
        导入自己单独核（问题用 sample_unusable 报），所以传假。"""
        try:
            table = check_limits(table)
        except SpreadsheetError as exc:
            raise ValidationFailed(str(exc), code="spreadsheet_invalid") from exc
        capabilities = self.capabilities.specs()
        result = rules.generate(
            template.config or {}, self.catalog(), table, params, capabilities=capabilities,
            filename=filename or "未命名表格", template_name=template.name, description=template.description,
        )
        # 模板存进来时是好的，之后方法可能退役、指标可能停用：按现在的主数据再核一次
        result["issues"][:0] = [f"模板：{item}" for item in self.problems(template.config or {})]
        self._link_sop(template.config or {}, result)
        result["warnings"][:0] = list(sheet_warnings)
        # 读文件时的提醒（隐藏行、隐藏工作表）单独也给一份：之后改实验参数走 /preview 只提交表格、没有文件，
        # 前端要靠它把这些提醒一直留在页面上
        result["sheet_warnings"] = list(sheet_warnings)
        if samples:
            problems, notes, _ = self._sample_check(_serials(result))
            result["issues"].extend(problems)
            result["warnings"].extend(notes)
        result["warnings"].extend(self._step_warnings(result["steps"], capabilities))
        result["template_id"] = template.id
        result["filename"] = filename
        result["table"] = table
        return result

    def _step_warnings(self, steps: list[dict], capabilities: dict[str, dict]) -> list[str]:
        """生成的步骤按当前工位与方法校验一遍：导入只建草稿，这些问题在提交评审前要解决，所以只提醒。"""
        if not steps:
            return []
        from ...services.flow_expansion import resolved_steps

        resolved, method_problems = resolved_steps(self.db, self.ctx, steps)
        rows = validate_steps(resolved, self.stations.specs(), capabilities, {}, method_problems)
        return [
            f"第 {row['index'] + 1} 步「{row['name']}」：{'；'.join(row['issues']) or '当前没有可承接的工位'}"
            for row in rows if not row["ok"]
        ]

    def parse(self, template_id: str, filename: str, data: bytes) -> dict[str, Any]:
        template = self._active(template_id)
        try:
            sheet = read_sheet(filename, data)
        except SpreadsheetError as exc:
            raise ValidationFailed(str(exc), code="spreadsheet_invalid") from exc
        return self._generate(template, filename, sheet.rows, {}, sheet_warnings=sheet.warnings)

    def preview(self, template_id: str, filename: str, table: list[list[Any]], params: dict[str, Any]) -> dict[str, Any]:
        return self._generate(self._active(template_id), filename, table, params)

    # ---------- 导入 ----------

    def import_table(
        self, template_id: str, payload: dict[str, Any], user: User | None,
        submission: FormulationSubmission | None = None,
    ) -> dict[str, Any]:
        """一个事务：登记（或沿用）瓶子 → 沿用或新建流程草稿 → 新建方案草稿 → 审计。

        `user` 为空是服务身份在提交（见 `submit`）：起草人记成这个服务身份；`submission` 随最后一次提交一起落库。"""
        template = self._active(template_id)
        filename = str(payload.get("filename") or "").strip() or "未命名表格"
        result = self._generate(template, filename, payload.get("table") or [], payload.get("params") or {},
                                samples=False)
        if result["issues"]:
            raise ValidationFailed(
                f"配方表有 {len(result['issues'])} 处问题，不能导入：{result['issues'][0]}",
                {"problems": result["issues"]}, code="formulation_invalid",
            )
        config = template.config or {}
        plan_spec = result["plan"]
        samples = self._register_samples(plan_spec["sample_ids"], template, filename, config, user,
                                         result["warnings"])
        recipe, reused = self._recipe_for(result, user, filename, template)

        from ...services.plan_service import PlanService

        plan = PlanService(self.db, self.ctx).create({
            "name": str(payload.get("plan_name") or "").strip() or plan_spec["name"],
            "recipe_id": recipe.id, "plan_type": plan_spec["plan_type"], "goal": plan_spec["goal"],
            "repeats": plan_spec["repeats"], "layout": "sequential", "seed": 1,
            "factors": plan_spec["factors"], "design_points": plan_spec["design_points"],
            "sample_ids": plan_spec["sample_ids"], "required_metrics": plan_spec["required_metrics"],
        }, user)
        created = sum(1 for row in samples if row["created"])
        via = f"（{self.ctx.subject_label or '外部系统'} 提交，请求 {submission.request_id}）" if submission else ""
        self.audit.record(
            user, "配方表导入", template.id, after=plan["id"], object_version=template.row_version,
            detail=f"{filename}{via}；模板 {template.code}；流程 {recipe.id}（{'沿用' if reused else '新建草稿'}）；"
                   f"方案 {plan['id']}；{len(result['plan']['design_points'])} 个配方、{len(samples)} 瓶"
                   f"（新登记 {created}，沿用 {len(samples) - created}）",
        )
        out = {
            "template_id": template.id,
            "recipe": {"id": recipe.id, "name": recipe.name, "state": recipe.state, "reused": reused},
            "plan": {"id": plan["id"], "name": plan["name"], "state": plan["state"]},
            "samples": samples,
            "warnings": result["warnings"],
        }
        if submission is not None:
            submission.plan_id, submission.recipe_id = plan["id"], recipe.id
            submission.result = {"request_id": submission.request_id, "template": template.code, **out}
            self.submissions.add(submission)
        self.db.commit()
        return out

    # ---------- 外部系统提交（服务身份） ----------

    def _service_template(self, code: str) -> FormulationTemplate:
        """服务身份要在 `formulation_imports` 里被授权这个模板（all 或模板编号）。别的组织的、不存在的一律 404。"""
        if not self.ctx.is_service:
            raise PermissionDenied("这个入口只给服务身份用；人员在「配方导入」页面导入", code="service_only")
        if not service_may_import(self.ctx.scopes, code):
            raise PermissionDenied(f"该服务身份没有向配液模板 {code} 提交配方表的授权",
                                   code="formulation_import_not_authorized")
        template = self.templates.by_code(code)
        if template is None:
            raise NotFound("配液模板不存在")
        return template

    def submit(self, code: str, payload: dict[str, Any]) -> dict[str, Any]:
        """上游系统提交一张配方表：与界面导入同一套校验与生成，生成的是流程与方案草稿。按请求编号去重：
        同一编号同一内容回放首次结果（`replayed: true`），内容不同 409；表格有问题 422 列出全部问题，什么都不建。"""
        template = self._service_template(code)
        request_id = str(payload.get("request_id") or "").strip()
        body = {key: payload.get(key) for key in ("filename", "table", "params", "plan_name")}
        digest = hashlib.sha256(
            json.dumps(body, sort_keys=True, ensure_ascii=False, default=str).encode()
        ).hexdigest()
        existing = self.submissions.find(self.ctx.subject_id, request_id)
        if existing is not None:
            if existing.digest != digest or existing.template_id != template.id:
                raise StateConflict(f"请求编号 {request_id} 已经提交过，内容与这次不一致：换一个请求编号",
                                    code="submission_conflict")
            return {**existing.result, "replayed": True}
        submission = FormulationSubmission(
            org_id=self.ctx.org_id, service_id=self.ctx.subject_id, source=self.ctx.subject_label or "",
            request_id=request_id, digest=digest, template_id=template.id, template_code=template.code,
            filename=str(body.get("filename") or "").strip(), plan_id="", recipe_id="", created_at=now(),
        )
        self.import_table(template.id, {**body, "filename": body.get("filename") or f"外部提交 {request_id}"},
                          None, submission=submission)
        return {**submission.result, "replayed": False}

    def progress(self, code: str, request_id: str) -> dict[str, Any]:
        """提交方按请求编号查进度：流程与方案的审批状态、实验任务、批次进度、每瓶的检测结果。只看得到自己提交的。

        结果标明复核状态与是否进正式统计（`official`）：审核通过、质量有效、没被更正版本取代的才算；
        模拟设备的示意值照实标出来（`simulated`），不冒充实测。曲线只给点数，完整的点按结果编号去取。"""
        from ...domain.statistics import EXCLUSION_REASONS
        from ...models import Batch, ExperimentTask, Report, ResultValue
        from ...repositories.batches import AnalysisTaskRepository
        from ...repositories.recipes import PlanRepository
        from ...services.batch_service import BatchService

        template = self._service_template(code)
        row = self.submissions.find(self.ctx.subject_id, str(request_id or "").strip())
        if row is None or row.template_id != template.id:
            raise NotFound("没有这个请求编号的提交")
        plan = PlanRepository(self.db, self.ctx).get(row.plan_id)
        recipe = self.recipes.get(row.recipe_id)
        tasks = (self.db.query(ExperimentTask)
                 .filter(ExperimentTask.org_id == self.ctx.org_id, ExperimentTask.plan_id == row.plan_id)
                 .order_by(ExperimentTask.id).all())
        batches = (self.db.query(Batch).filter(Batch.org_id == self.ctx.org_id, Batch.plan_id == row.plan_id)
                   .order_by(Batch.created_at).all())
        batch_service = BatchService(self.db, self.ctx)
        batch_rows = []
        for batch in batches:
            summary = batch_service.summary_out(batch)
            batch_rows.append({key: summary.get(key) for key in (
                "id", "state", "state_label", "task_id", "step_count", "current_step", "sample_count",
                "sample_done", "starts_at", "ends_at", "failure_reason",
            )})
        tasks_of = AnalysisTaskRepository(self.db, self.ctx)
        analysis = [task for batch in batches for task in tasks_of.for_batch(batch.id)]
        values = (self.db.query(ResultValue)
                  .filter(ResultValue.org_id == self.ctx.org_id,
                          ResultValue.analysis_task_id.in_([task.id for task in analysis]),
                          ResultValue.superseded_by_id == "")
                  .order_by(ResultValue.physical_sample_id, ResultValue.created_at).all()
                  if analysis else [])
        definitions = self.metrics.many(sorted({value.metric_definition_id for value in values}))
        results = []
        for value in values:
            definition = definitions.get(value.metric_definition_id)
            reason = _official_reason(value)
            flags = {flag.get("code") for flag in value.flags or [] if isinstance(flag, dict)}
            series = value.value_series or None
            results.append({
                "result_id": value.id, "sample_id": value.physical_sample_id,
                "metric": definition.code if definition else value.metric_definition_id,
                "metric_name": definition.name if definition else "",
                "value": value.value_num if value.value_num is not None else (value.value_text or None),
                "series_points": sum(len(trace.get("x") or []) for trace in (series or {}).get("traces") or [])
                if series else None,
                "unit": value.unit, "review_state": value.review_state, "quality": value.quality,
                "official": reason is None, "excluded": EXCLUSION_REASONS.get(reason, "") if reason else "",
                "simulated": "simulated" in flags, "station_id": value.station_id,
            })
        batch_ids = [batch.id for batch in batches]
        reports = self.db.query(Report).filter(
            Report.org_id == self.ctx.org_id,
            (Report.plan_id == row.plan_id) | Report.batch_id.in_(batch_ids or [""]),
        ).all()
        return {
            "request_id": row.request_id, "template": row.template_code,
            "submitted_at": row.created_at.isoformat(timespec="seconds"),
            "recipe": {"id": recipe.id, "name": recipe.name, "state": recipe.state, "version": recipe.version}
            if recipe else {"id": row.recipe_id},
            "plan": {"id": plan.id, "name": plan.name, "state": plan.state, "approval_state": plan.approval_state,
                     "version": plan.version} if plan else {"id": row.plan_id},
            "samples": [item["id"] for item in (row.result or {}).get("samples") or []],
            "tasks": [{"id": task.id, "state": task.state, "batch_id": task.batch_id, "parent_id": task.parent_id}
                      for task in tasks],
            "batches": batch_rows,
            "results": results,
            "reports": [{"id": report.id, "code": report.code, "title": report.title} for report in reports],
        }

    def _sample_check(self, serials: list[str]) -> tuple[list[str], list[str], dict[str, PhysicalSample | None]]:
        """瓶身序列号能不能当空瓶配液：(问题, 提醒, 已登记的样本)。只读，预览与导入共用。

        - 已在某个批次里有运行分配：配过液了，拒绝——一瓶只配一个配方，否则两次运行的结果挂在同一个样本上；
        - 报废 / 耗尽、编号被别的组织占用、是别的样本的条码：拒绝；
        - 已登记但没进过批次（例如改了表重新导入）：沿用，提醒一句。
        """
        existing: dict[str, PhysicalSample | None] = {}
        problems: list[str] = []
        notes: list[str] = []
        from ...services.batch_service import BatchService

        batches = BatchService(self.db, self.ctx)
        for serial in serials:
            sample = self.samples.get(serial)
            existing[serial] = sample
            if sample is not None:
                # 与建批次同一条口径：没终止的批次、或已向设备发过指令的终止批次里有它，就是配过液了
                used_by = batches.sample_used_by(serial)
                if used_by:
                    problems.append(f"序列号 {serial} 已在批次 {used_by} 里配过液，不能再作为空瓶导入")
                elif sample.lifecycle_state in UNUSABLE_SAMPLE:
                    problems.append(f"样本 {serial} {UNUSABLE_SAMPLE[sample.lifecycle_state]}，不能再用于配液")
                else:
                    notes.append(f"序列号 {serial} 已登记，将沿用")
                continue
            if self.db.get(PhysicalSample, serial) is not None:
                # 别的组织登记过同一个编号：主键冲突，也不能泄漏对方的样本信息
                problems.append(f"样本编号 {serial} 已被占用，请换用别的瓶身序列号")
                continue
            holder = self.samples.by_barcode(serial)
            if holder is not None:
                problems.append(f"序列号 {serial} 已是样本 {holder.id} 的条码")
        return problems, notes, existing

    def _actor_name(self, user: User | None) -> str:
        return user.display_name if user is not None else (self.ctx.subject_label or "外部系统")

    def _actor_id(self, user: User | None) -> str:
        return user.id if user is not None else f"service:{self.ctx.subject_id}"

    def _register_samples(self, serials: list[str], template: FormulationTemplate, filename: str,
                          config: dict, user: User | None, warnings: list[str]) -> list[dict[str, Any]]:
        """瓶身序列号 → 物理样本。已登记且没配过液的沿用；配过液、报废 / 耗尽的整张表拒绝（先全部核对，再写）。"""
        problems, notes, existing = self._sample_check(serials)
        warnings.extend(notes)
        if problems:
            raise ValidationFailed(f"有 {len(problems)} 个瓶子不能使用：{problems[0]}", {"problems": problems},
                                   code="sample_unusable")
        rows = []
        source = f"配方表 {filename}（模板 {template.code}）"
        for serial in serials:
            if existing[serial] is not None:
                rows.append({"id": serial, "created": False})
                continue
            # 与样本登记同一套字段：编号与条码都是瓶身序列号，扫码即可找到
            sample = PhysicalSample(
                id=serial, org_id=self.ctx.org_id, barcode=serial, source=source,
                sample_type=str(config.get("sample_type") or ""), custodian=self._actor_name(user),
                lifecycle_state="registered", origin="registered", created_by=self._actor_id(user),
            )
            self.samples.add(sample)
            self.audit.record(user, "登记样本", sample.id, before="—", after="已登记",
                              detail=f"条码 {sample.barcode}；来源 {source}", object_version=sample.row_version)
            rows.append({"id": serial, "created": True})
        return rows

    def _link_sop(self, config: dict, result: dict[str, Any]) -> None:
        """模板用 `sop` 指定了 SOP：生成的流程关联它当前生效的版本，步骤的 `sop_step`（步骤标题）换成这一版的
        步骤标识 `sop_step_key`——批次里每个节点就能带出对应的作业指导，SOP 修订插入步骤也还对得上。

        没有生效版本、标题在这一版里找不到、设备能力超出 SOP 的适用范围，都是问题：这样的流程发布不了或建不了批次，
        不如导入时就说清楚。没指定 SOP 时只去掉 `sop_step`。
        """
        from ...domain.sop_steps import scope_outside
        from ...repositories.sops import SopRepository, SopVersionRepository

        steps = result.get("steps") or []
        titles = {index: step.pop("sop_step") for index, step in enumerate(steps) if "sop_step" in step}
        code = str(((config.get("sop") or {}) if isinstance(config.get("sop"), dict) else {}).get("code") or "").strip()
        if not code:
            return
        sop = SopRepository(self.db, self.ctx).by_code(code)
        version = SopVersionRepository(self.db, self.ctx).effective(sop.id, now()) if sop is not None else None
        if version is None:
            result["issues"].append(f"模板指定的 SOP {code} 没有生效版本：先发布 SOP，再导入配方表")
            return
        label = f"{code} {version.version}"
        keys = {str(row.get("title") or ""): str(row.get("key") or "") for row in version.steps or []}
        for index, title in titles.items():
            if keys.get(str(title)):
                steps[index]["sop_step_key"] = keys[str(title)]
            else:
                result["issues"].append(
                    f"第 {index + 1} 步「{steps[index].get('name')}」对应的 SOP 步骤「{title}」在 {label} 里没有"
                )
        outside = scope_outside(steps, version.capability_scope)
        if outside:
            result["issues"].append(f"设备能力 {'、'.join(outside)} 不在 {label} 的适用范围内")
        result["recipe"]["sop_version_id"] = version.id
        result["recipe"]["sop"] = {"code": code, "version": version.version, "title": sop.title}

    def _recipe_for(self, result: dict[str, Any], user: User | None, filename: str, template: FormulationTemplate):
        """同结构沿用：步骤完全一致（含对应的 SOP 步骤）、关联同一版 SOP、每批样品位一致、BOM 为空、没退役、
        没被标记待修订。SOP 出了新版本，旧版本的流程就不再沿用，按新版本生成。"""
        steps, spec = result["steps"], result["recipe"]
        wanted = _steps_key(steps)
        sop_version_id = spec.get("sop_version_id") or ""
        candidates = [
            recipe for recipe in self.recipes.list()
            if recipe.state in REUSE_RANK and not recipe.needs_revision and not (recipe.bom or [])
            and (recipe.sop_version_id or "") == sop_version_id
            and int(recipe.plate or 0) == int(spec["plate"]) and _steps_key(recipe.steps) == wanted
        ]
        if candidates:
            candidates.sort(key=lambda recipe: (REUSE_RANK[recipe.state], -self._number(recipe.id)))
            return candidates[0], True
        from ...services.recipe_service import RecipeService

        recipe = RecipeService(self.db, self.ctx).create_from_steps(
            spec["name"], int(spec["plate"]), steps, user, bom=[], risk=spec["risk"], design=spec["design"],
            sop_version_id=sop_version_id,
            note=f"由配方表 {filename} 生成（配液模板 {template.code}）",
        )
        return recipe, False

    @staticmethod
    def _number(recipe_id: str) -> int:
        digits = re.sub(r"\D", "", recipe_id or "")
        return int(digits) if digits else 0


def service_may_import(scopes: dict | None, code: str) -> bool:
    """服务身份的 `formulation_imports`：all，或允许提交的配液模板编号列表。"""
    granted = (scopes or {}).get("formulation_imports")
    return granted == "all" or (isinstance(granted, list) and code in granted)


def _official_reason(value) -> str | None:
    """一条结果为什么不进正式统计（与 domain.statistics 同一套原因）；进的话 None。曲线不算数值，不按这条判。"""
    if value.superseded_by_id:
        return "superseded"
    if value.not_measured_reason or (value.value_num is None and not value.value_text and not value.value_series):
        return "not_measured"
    if value.review_state != "approved":
        return "pending_review" if value.review_state == "pending" else "rejected_review"
    if value.quality != "valid":
        return value.quality if value.quality in {"suspect", "invalid"} else "unassessed"
    return None


def _serials(result: dict[str, Any]) -> list[str]:
    """预览里的瓶身序列号，按行序去重（重复序列号本身已是表格问题）。"""
    out: list[str] = []
    for row in result.get("rows") or []:
        if row.get("serial") and row["serial"] not in out:
            out.append(row["serial"])
    return out
