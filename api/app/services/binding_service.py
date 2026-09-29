"""前馈参数的下发时求值：从上游步骤取值、乘系数、换单位、核对范围，写进指令参数并留下记录。

取值只认确定的事实：
- 设备来源取该步最近检查点的回执。检查点质量必须正常：现场核实写入的检查点质量是 uncertain，
  系统不替人把「应该称过了」当成读数用；
- 人工来源取该步最近一次完成的记录；
- 逐样本时每个在用样本各取一个值（设备按孔位回报的 `delivered.wells`，或按样本录入的字段）。

任何一项取不到、换不了单位、超出预期范围 / 设备方法范围 / 工位极限，整条指令都不下发（不离开系统），
由调用方挂起批次并报警——少一个样本的设定值，不能拿别的样本或流程里的缺省值顶上。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from ..core.context import AccessContext
from ..domain import workflow
from ..domain.bindings import bindings_of, compute, decimal_text, expect_of
from ..domain.params import canonical_unit, decimal_of, spec_of
from ..domain.steps import DEVICE, MANUAL, kind_of, normalize, step_id_of
from ..models import Batch, Sample, StepRun
from ..repositories.execution import CheckpointRepository
from ..repositories.resources import CapabilityRepository


@dataclass
class Resolution:
    """一条指令上全部前馈参数的求值结果。"""

    batch_values: dict[str, float] = field(default_factory=dict)
    well_values: dict[str, dict[str, float]] = field(default_factory=dict)
    records: list[dict[str, Any]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def apply(self, params: dict[str, Any]) -> dict[str, Any]:
        """并进指令参数：整批的值写在参数上，逐样本的值写进 `params.wells`，与矩阵逐孔参数同一个位置。"""
        merged = dict(params)
        merged.update(self.batch_values)
        if self.well_values:
            wells = {well: dict(values) for well, values in (merged.get("wells") or {}).items()}
            for well, values in self.well_values.items():
                wells.setdefault(well, {}).update(values)
            merged["wells"] = wells
        return merged


@dataclass
class _Source:
    """来源步骤当前有效的那次执行：设备步骤的检查点回执，或人工步骤的记录值。"""

    kind: str
    ref: str
    attempt: int
    values: dict[str, Any]
    problem: str = ""


class BindingResolver:
    def __init__(self, db: Session, ctx: AccessContext):
        self.db = db
        self.ctx = ctx
        self.checkpoints = CheckpointRepository(db)
        self.capabilities = CapabilityRepository(db)

    def resolve(
        self, batch: Batch, step: dict[str, Any], targets: dict[str, Sample] | None,
        station_limits: dict[str, list[float]] | None,
    ) -> Resolution:
        """`targets` 是这一步的处理对象：设备孔位 → 在用样本（与矩阵逐孔参数同一口径）；
        `station_limits` 是这条指令目标工位对本能力的参数极限（{参数: [下限, 上限]}）。"""
        result = Resolution()
        bindings = bindings_of(step)
        if not bindings:
            return result
        steps = normalize((batch.recipe_snapshot or {}).get("steps") or [])
        ids = [step_id_of(row, position) for position, row in enumerate(steps)]
        capability = self.capabilities.specs().get(step.get("cap") or "")
        method_rules = (step.get("method") or {}).get("params") or {}
        factors = (batch.plan_snapshot or {}).get("factors") or []
        positions: dict[str, str] | None = None
        for param, binding in bindings.items():
            spec = spec_of(capability, param)
            label = spec["label"]
            source_id = str(binding.get("source_step_id") or "")
            field_name = str(binding.get("field") or "")
            scope = binding.get("scope") or "batch"
            source_unit = canonical_unit(binding.get("unit"))
            target_unit = spec["unit"]
            if source_id not in ids:
                result.problems.append(f"{label}：前馈来源步骤 {source_id} 不在批次流程里")
                continue
            origin = steps[ids.index(source_id)]
            origin_name = origin.get("name") or source_id
            source = self._source(batch, origin, ids.index(source_id))
            if source.problem:
                result.problems.append(f"{label}：{source.problem}")
                continue
            coefficient = binding.get("coefficient") if isinstance(binding.get("coefficient"), dict) else {}
            factor_name = str(coefficient.get("factor") or "").strip()
            factor_index = next(
                (i for i, row in enumerate(factors) if str(row.get("name") or "") == factor_name), None,
            ) if factor_name else None
            if factor_name and factor_index is None:
                result.problems.append(f"{label}：前馈系数引用的因子「{factor_name}」不在批次方案里")
                continue
            if coefficient and not factor_name and (decimal_of(coefficient.get("value")) or 0) <= 0:
                # 流程校验已拦下，这里兜底：写了系数却不完整，不能悄悄退回成「不乘系数」
                result.problems.append(f"{label}：前馈系数配置不完整（没有因子名，也没有大于 0 的系数值）")
                continue
            # 逐样本的来源、或系数按样本的因子水平取值：每个在用样本各算一个值
            per_sample = scope == "sample" or bool(factor_name)
            if per_sample and not targets:
                result.problems.append(f"{label}：这一步没有在用样本，逐样本前馈无从取值")
                continue
            rows: list[tuple[str, Sample | None]] = list(targets.items()) if per_sample else [("", None)]
            if scope == "sample" and source.kind == DEVICE and positions is None:
                from .sample_service import SampleService

                positions = SampleService(self.db, self.ctx).device_wells(batch.id)
            window = (station_limits or {}).get(param)
            missing: list[str] = []
            rejected: list[str] = []
            outputs: list[tuple[str, Sample | None, Decimal, Decimal, Decimal | None, str]] = []
            unconvertible = False
            for well, sample in rows:
                name = (sample.well or sample.id) if sample is not None else "整批"
                raw = decimal_of(_raw(source, field_name, scope, sample, positions))
                if raw is None:
                    missing.append(name)
                    continue
                if factor_name:
                    levels = list(sample.levels or []) if sample is not None else []
                    level = levels[factor_index] if factor_index < len(levels) else None
                    factor_value = decimal_of(level)
                    factor_unit = str(factors[factor_index].get("unit") or "")
                    if factor_value is None:
                        missing.append(f"{name} 的因子「{factor_name}」水平")
                        continue
                else:
                    factor_value = decimal_of(coefficient.get("value")) if coefficient else None
                    factor_unit = str(coefficient.get("unit") or "") if coefficient else ""
                value = compute(raw, source_unit, target_unit, factor_value, factor_unit)
                if value is None:
                    unconvertible = True
                    result.problems.append(
                        f"{label}：来源单位 {source_unit or '（未写）'}、系数单位 {factor_unit or '—'} "
                        f"换算不到参数单位 {target_unit or '（未登记）'}"
                    )
                    break
                bound = _bounds_problem(value, binding, method_rules.get(param) or {}, window, spec)
                if bound:
                    rejected.append(f"{name} {decimal_text(value)} {target_unit}（{bound}）")
                    continue
                outputs.append((well, sample, raw, value, factor_value, factor_unit))
            if missing:
                result.problems.append(
                    f"{label}：{'、'.join(missing[:8])}{' 等' if len(missing) > 8 else ''} 没有「{origin_name}」的 "
                    f"{field_name} 值"
                )
            if rejected:
                result.problems.append(f"{label}：计算值不能下发——{'；'.join(rejected[:6])}")
            if unconvertible or missing or rejected:
                continue
            for well, sample, raw, value, factor_value, factor_unit in outputs:
                number = float(value)
                if sample is None:
                    result.batch_values[param] = number
                else:
                    result.well_values.setdefault(well, {})[param] = number
                result.records.append({
                    "param": param, "label": label, "scope": scope,
                    "sample_id": sample.id if sample is not None else "",
                    "well": well if sample is not None else "",
                    "source_step_id": source_id, "source_name": origin_name, "source_kind": source.kind,
                    "source_ref": source.ref, "source_attempt": source.attempt, "field": field_name,
                    "raw": decimal_text(raw), "unit": source_unit,
                    "coefficient": decimal_text(factor_value) if factor_value is not None else "",
                    "coefficient_unit": canonical_unit(factor_unit) if factor_value is not None else "",
                    "coefficient_source": f"factor:{factor_name}" if factor_name else ("flow" if coefficient else ""),
                    "value": decimal_text(value), "target_unit": target_unit,
                })
        return result

    def _source(self, batch: Batch, origin: dict[str, Any], index: int) -> _Source:
        name = origin.get("name") or step_id_of(origin, index)
        kind = kind_of(origin)
        if kind == DEVICE:
            checkpoint = self.checkpoints.latest_for_step(batch.id, index)
            if checkpoint is None:
                return _Source(DEVICE, "", 0, {}, f"「{name}」还没有检查点（设备回执）")
            run = self.db.get(StepRun, checkpoint.step_run_id) if checkpoint.step_run_id else None
            attempt = run.attempt if run is not None else 0
            if run is not None and run.state != workflow.COMPLETED:
                return _Source(DEVICE, checkpoint.id, attempt, {}, f"「{name}」最近一次执行是 {run.state}，不是已完成")
            payload = checkpoint.payload or {}
            if (payload.get("quality") or "good") != "good":
                return _Source(
                    DEVICE, checkpoint.id, attempt, {},
                    f"「{name}」的检查点质量为 {payload.get('quality')}（如现场核实写入），"
                    "读数不能自动用作设定值，请人工确认",
                )
            return _Source(DEVICE, checkpoint.id, attempt, payload.get("delivered") or {})
        if kind == MANUAL:
            latest = (
                self.db.query(StepRun)
                .filter(
                    StepRun.batch_id == batch.id, StepRun.step_id == step_id_of(origin, index),
                    StepRun.state == workflow.COMPLETED,
                )
                .order_by(StepRun.attempt.desc())
                .first()
            )
            if latest is None:
                return _Source(MANUAL, "", 0, {}, f"「{name}」还没有完成的人工记录")
            return _Source(MANUAL, latest.id, latest.attempt, (latest.form_data or {}).get("values") or {})
        return _Source(kind, "", 0, {}, f"「{name}」不是设备或人工步骤，不能作前馈来源")


def _raw(
    source: _Source, field_name: str, scope: str, sample: Sample | None, positions: dict[str, str] | None,
) -> Any:
    """来源值：整批取字段本身；逐样本时设备取 `wells[该样本的设备孔位][字段]`，人工取按样本录入的值。"""
    if scope != "sample" or sample is None:
        value = source.values.get(field_name)
        return None if isinstance(value, dict) else value
    if source.kind == DEVICE:
        wells = source.values.get("wells") or {}
        key = (positions or {}).get(sample.id, sample.well)
        entry = wells.get(key) if isinstance(wells, dict) else None
        return entry.get(field_name) if isinstance(entry, dict) else None
    value = source.values.get(field_name)
    return value.get(sample.id) if isinstance(value, dict) else None


def _bounds_problem(
    value: Decimal, binding: dict[str, Any], rule: dict[str, Any], window: Any, spec: dict[str, Any],
) -> str:
    """计算值不能下发的原因；能下发返回空串。预期范围是流程批准的窗口，窗口外同样不下发。"""
    expect = expect_of(binding)
    if expect is not None and (value < Decimal(str(expect[0])) or value > Decimal(str(expect[1]))):
        return f"超出流程声明的预期范围 [{expect[0]:g}, {expect[1]:g}]"
    low, high = decimal_of(rule.get("min")), decimal_of(rule.get("max"))
    if (low is not None and value < low) or (high is not None and value > high):
        return f"超出设备方法允许的 [{rule.get('min', '−∞')}, {rule.get('max', '∞')}]"
    if isinstance(window, (list, tuple)) and len(window) == 2:
        if value < Decimal(str(window[0])) or value > Decimal(str(window[1])):
            return f"超出工位极限 [{window[0]}, {window[1]}]"
    if spec["type"] == "integer" and value != value.to_integral_value():
        return "参数要求整数"
    return ""
