"""设备回报的检测值 → 检测结果。

设备方法的输出项关联了指标（`metric_id`）时，设备这一步回报的值按样本写成该指标的检测结果：进「数据审核」
待复核，复核通过后进结果分析、报告，也才进闭环的训练数据——与仪器结果回传是同一条链，不用人从批次记录里抄数。

- 按样本记：取 `delivered.wells[孔位][键]`，孔位 → 样本与逐孔参数同一口径（`BatchService._step_targets`）。
  设备只给了顶层值（批次级读数）时照 `dataquality.output_flags` 的口径当作每个孔位的值，并打上「批次级读数」标记。
- 每个样本一张「设备回报」检测任务，要求指标 = 这个批次快照里所有输出项关联的指标（建任务时冻结）。
- 按「指令 + 孔位」去重（回传事件表的同一唯一键）：回执重放不重复写；同一步重做（返工、续跑）的新读数取代上一版，
  留版本链，重新待复核。别的步骤已经回报过同一指标（当前值沿更正链追到的是另一步的设备回报）的不写、报警：
  当成更正取代会让前一阶段的数据悄悄离开正式统计。发布流程与建批次时已按 `metric_link_problems` 拦下这种配置，
  这里是最后一道（已发布的老流程、派生指标按代码在写入时才取定）。
- 曲线型输出（`{"x": [...], "y": [...]}` 或几条 `traces`）按曲线指标记；曲线声明了派生的数值指标时一并写
  （要求指标里含这些派生指标，建任务时一起冻结）。同一步重做出了新曲线时，从上一版曲线派生的数值跟着重新派生
  （与人工更正曲线时一样）；设备显式回报的、人工改过的派生值不动。回传事件里只留曲线的概要，完整的点在结果里。
- 设备回执的质量不是 good（设备自己说读数可能无效）：照写、置可疑、打「设备质量」标记，复核时下结论。
- 值不成立（类型、单位不对）不写并报警；越界照写、置可疑、打标，与回传同一口径。内置模拟给的是示意值：
  打「模拟设备示意值」标记，照常走审核与报告，但不进闭环训练数据（`proposal_service._exclusion`）。走真实协议接入的
  外部模拟设备（工位当前采用的接入验收由自报为模拟器的设备通过）回报的值同样打这个标记：驱动是真的，数不是实测。
- 与设备步骤完成同一个事务，不自己提交；出错只报警，不挡流程推进——值留在检查点上，可以人工补录。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from ..core.clock import now
from ..core.context import AccessContext
from ..domain import dataquality
from ..domain import series as curves
from ..domain.methods import shared_metric_problems
from ..domain.metrics import check_value
from ..domain.steps import normalize
from ..models import AnalysisTask, Batch, Command, IngestEvent, ResultValue, Sample
from ..repositories.batches import AnalysisTaskRepository
from ..repositories.metrics import IngestEventRepository, MetricRepository, ResultValueRepository
from .alarm_service import AlarmService
from .analysis_service import AnalysisService, digest, stored_value, value_columns

METHOD = "设备回报"
SIMULATED = "simulated"
SIMULATED_NOTES = {
    "builtin": "内置模拟设备按方法输出规则给的示意值，不是实测",
    "device": "模拟设备回报的值（工位的接入验收由自报为模拟器的设备通过），不是实测",
}


def linked_rules(step: dict) -> list[dict]:
    """这一步方法输出项里关联了指标的那些规则（步骤快照里冻结的方法）。"""
    outputs = ((step or {}).get("method") or {}).get("outputs") or []
    return [rule for rule in outputs if isinstance(rule, dict) and str(rule.get("metric_id") or "").strip()]


def linked_metrics(snapshot: dict) -> list[str]:
    """批次快照里所有设备步骤关联的指标：「设备回报」检测任务的要求集合。按首次出现排序，稳定。"""
    seen: list[str] = []
    for step in normalize((snapshot or {}).get("steps") or []):
        for rule in linked_rules(step):
            metric_id = str(rule["metric_id"]).strip()
            if metric_id not in seen:
                seen.append(metric_id)
    return seen


def metric_link_problems(db: Session, ctx: AccessContext, steps: list[dict]) -> dict[str, list[str]]:
    """同一指标被两个设备步骤关联（含曲线派生出的数值指标）的问题：{后关联的那一步: [问题]}。

    `steps` 是展开子流程、解析过设备方法之后的步骤（方法快照里有输出项）。派生指标按代码取当时在用的版本，
    与写入时（`DeviceResultService._required`）同一个取法。规则见 `domain.methods.shared_metric_problems`。
    """
    steps = normalize(steps or [])
    linked = list(dict.fromkeys(str(rule["metric_id"]).strip() for step in steps for rule in linked_rules(step)))
    if not linked:
        return {}
    metrics = MetricRepository(db, ctx)
    definitions = metrics.many(linked)
    derived: dict[str, list[str]] = {}
    names = {metric_id: definition.code for metric_id, definition in definitions.items()}
    for metric_id, definition in definitions.items():
        if definition.value_type != "series":
            continue
        for spec in (definition.rules or {}).get("derived") or []:
            target = metrics.by_code(str(spec.get("metric") or ""))
            if target is not None and target.value_type == "number" and target.state == "active":
                derived.setdefault(metric_id, []).append(target.id)
                names[target.id] = target.code
    return shared_metric_problems(steps, derived, names)


class DeviceResultService:
    def __init__(self, db: Session, ctx: AccessContext):
        self.db = db
        self.ctx = ctx
        self.tasks = AnalysisTaskRepository(db, ctx)
        self.metrics = MetricRepository(db, ctx)
        self.events = IngestEventRepository(db, ctx)
        self.values = ResultValueRepository(db, ctx)
        self.analysis = AnalysisService(db, ctx)
        self.alarms = AlarmService(db, ctx)

    def record(self, batch: Batch, command: Command, step: dict, delivered: dict, origin: str,
               quality: str = "good") -> dict[str, Any]:
        """写这一步的检测结果。返回 {written, samples, problems}；不提交。`quality` 是回执的质量。"""
        rules = linked_rules(step)
        if not rules:
            return {"written": 0, "samples": 0, "problems": []}
        simulated = self._simulated(command.station_id, origin)
        instrument = self._instrument(command.station_id, simulated)
        from .batch_service import BatchService

        targets = BatchService(self.db, self.ctx)._step_targets(batch, step) or {}
        problems: list[str] = []
        if not targets:
            return {"written": 0, "samples": 0, "problems": []}
        definitions = self.metrics.many([str(rule["metric_id"]).strip() for rule in rules])
        required = self._required(batch.recipe_snapshot or {})
        wells = delivered.get("wells") if isinstance(delivered.get("wells"), dict) else {}
        overall, per_well = dataquality.receipt_quality({"quality": quality, "delivered": delivered})
        source = f"device:{command.station_id}"
        collected_at = command.updated_at if isinstance(command.updated_at, datetime) else now()
        written = 0
        samples = 0
        for well, sample in sorted(targets.items()):
            task = self._task_for(batch, sample, required)
            event_id = f"{command.id}:{well}"
            if self.events.find(source, task.id, event_id) is not None:
                continue  # 回执重放：这个样本这一步已经记过
            prepared: list[dict] = []
            for rule in rules:
                key = str(rule.get("key") or "")
                metric_id = str(rule["metric_id"]).strip()
                definition = definitions.get(metric_id)
                if definition is None or definition.state != "active":
                    problems.append(f"输出 {key} 关联的指标 {metric_id} 不存在或已停用")
                    continue
                if metric_id not in (task.required_metrics or []):
                    problems.append(f"样本 {sample.id} 的设备回报任务没有要求指标 {definition.code}")
                    continue
                row = wells.get(well) if isinstance(wells.get(well), dict) else {}
                value, batch_level = row.get(key), False
                if value is None and delivered.get(key) is not None:
                    value, batch_level = delivered.get(key), len(targets) > 1
                if value is None:
                    continue  # 缺必报项由输出规则检查打标报警，这里不重复
                unit = str(rule.get("unit") or definition.unit)
                errors = check_value(definition.value_type, definition.unit, definition.rules or {}, value, unit)
                if errors:
                    problems.append(f"样本 {sample.id} 的 {definition.code}：{'；'.join(errors)}")
                    continue
                flags = dataquality.range_flags(definition.value_type, definition.rules or {}, value, definition.code)
                prepared.append({"definition": definition, "value": stored_value(definition, value), "unit": unit,
                                 "flags": flags, "batch_level": batch_level, "key": key})
            if not prepared:
                continue
            current = self.values.current_for_task(task.id)
            prepared = self._this_step_only(prepared, current, batch, command, sample, problems)
            if not prepared:
                continue
            # 同一步重做出了新曲线：上一版曲线派生的数值跟着重新派生（_write 取代旧值）
            derived, _ = self.analysis._derived_rows(prepared, list(task.required_metrics or []), current, rederive=True)
            for row in derived:
                row.update({"batch_level": any(item["batch_level"] for item in prepared), "key": row["definition"].code})
            prepared.extend(self._this_step_only(derived, current, batch, command, sample, problems))
            others = {metric: value for metric, value in current.items()
                      if metric not in {row["definition"].id for row in prepared}}
            # 前后逻辑规则：设备值不因冲突拒收（值是设备真实回报的），拒收级也只打标，交审核下结论
            for problem in self.analysis._logic_check(others, prepared):
                for row in prepared:
                    row["flags"] = [*row["flags"], dataquality.flag("logic", problem["label"])]
            # 设备说这次读数不可信：值照写，置可疑、打标，复核的人下结论
            well_quality = per_well.get(well, overall)
            if well_quality != "good":
                for row in prepared:
                    row["flags"] = [*row["flags"], dataquality.flag(
                        "device_quality", f"设备回执质量为 {well_quality}：设备标记这次读数可能无效",
                    )]
            # 曲线在事件里只留概要：完整的点已经在结果里，也还在设备回执里
            recorded = {row["key"]: curves.summary(row["value"]) if row["definition"].value_type == "series" else row["value"]
                        for row in prepared}
            event = IngestEvent(
                org_id=batch.org_id, source=source, analysis_task_id=task.id, event_id=event_id,
                digest=digest({"command": command.id, "well": well, "values": recorded}),
                payload={"batch_id": batch.id, "command_id": command.id, "step_index": command.step_index,
                         "station_id": command.station_id, "well": well, "origin": origin, "quality": well_quality,
                         "values": recorded},
                state="accepted",
            )
            self.db.add(event)
            self.db.flush()
            written += self._write(task, sample, event, prepared, current, command, simulated, instrument, collected_at)
            samples += 1
            self.analysis._refresh_task_state(task)
            event.response = {"task_id": task.id, "task_state": task.state, "written": len(prepared)}
        if problems:
            self.alarms.raise_alarm(
                severity=3, source_type="batch", source_id=batch.id,
                message=f"第 {command.step_index + 1} 步设备回报的检测值有 {len(problems)} 项没能写成结果：{problems[0]}"[:500],
                response="核对设备方法输出项关联的指标与单位；值仍在批次检查点里，可在检测任务里人工补录。",
                owner="数据审核员", origin="system", condition_key=f"data:{batch.id}:{command.id}:results",
            )
        return {"written": written, "samples": samples, "problems": problems}

    def _this_step_only(self, rows: list[dict], current: dict[str, ResultValue], batch: Batch, command: Command,
                        sample: Sample, problems: list[str]) -> list[dict]:
        """去掉当前值是别的步骤回报的那几项：不写，记成问题（报警）。同一步重做照常取代。"""
        kept: list[dict] = []
        steps = normalize((batch.recipe_snapshot or {}).get("steps") or [])
        for row in rows:
            previous = current.get(row["definition"].id)
            origin = self._origin_step(previous) if previous is not None else None
            if origin is None or origin == command.step_index:
                kept.append(row)
                continue
            name = steps[origin].get("name") if 0 <= origin < len(steps) else ""
            problems.append(
                f"样本 {sample.id} 的 {row['definition'].code} 已由第 {origin + 1} 步「{name}」回报，"
                f"第 {command.step_index + 1} 步的读数没有写：一个样本每个指标只保留一条当前结果，写进去会把前一步的值"
                "当成旧版本取代。两步要分开记，请各关联一个指标"
            )
        return kept

    def _origin_step(self, value: ResultValue | None) -> int | None:
        """这条当前值最初是哪一步设备回报的（沿更正链往回找）；人工录入、外部回传的返回 None。"""
        seen: set[str] = set()
        while value is not None and value.id not in seen:
            seen.add(value.id)
            if value.ingest_event_id:
                event = self.db.get(IngestEvent, value.ingest_event_id)
                if event is None or not str(event.source or "").startswith("device:"):
                    return None
                index = (event.payload or {}).get("step_index")
                return index if isinstance(index, int) else None
            value = self.db.get(ResultValue, value.revises_id) if value.revises_id else None
        return None

    def _required(self, snapshot: dict) -> list[str]:
        """「设备回报」任务的要求指标：输出项关联的指标，加上其中曲线指标声明派生的（在用的）数值指标。"""
        required = linked_metrics(snapshot)
        for definition in self.metrics.many(required).values():
            if definition.value_type != "series":
                continue
            for spec in (definition.rules or {}).get("derived") or []:
                target = self.metrics.by_code(str(spec.get("metric") or ""))
                if target is not None and target.value_type == "number" and target.state == "active" and target.id not in required:
                    required.append(target.id)
        return required

    def _task_for(self, batch: Batch, sample: Sample, required: list[str]) -> AnalysisTask:
        """这个样本的「设备回报」检测任务：有就沿用，没有就建（要求指标冻结为批次快照里关联的指标）。"""
        for task in self.tasks.for_sample(sample.id):
            if task.method == METHOD and task.state != "cancelled":
                return task
        snapshot = batch.recipe_snapshot or {}
        task = self.analysis.create_task({
            "sample_id": sample.id, "physical_sample_id": sample.physical_sample_id,
            "method": METHOD, "method_version": f"{batch.recipe_id} v{snapshot.get('version') or ''}".strip(),
            "required_metrics": required,
        })
        self.db.flush()
        self.analysis.audit.record(
            None, "建立检测任务", task.id, before="—", after="待采集",
            detail=f"批次 {batch.id} 样本 {sample.id} 的设备回报；要求指标 {len(required)} 项已冻结",
        )
        return task

    def _write(self, task: AnalysisTask, sample: Sample, event: IngestEvent, prepared: list[dict],
               current: dict[str, ResultValue], command: Command, simulated: str, instrument: str,
               collected_at: datetime) -> int:
        for row in prepared:
            definition = row["definition"]
            flags = [*row["flags"], *(row.get("marks") or [])]
            if row["batch_level"]:
                flags.append(dataquality.flag("batch_level", "设备只回报了批次级读数，按每个样本各记一份"))
            if simulated:
                flags.append(dataquality.flag(SIMULATED, SIMULATED_NOTES[simulated]))
            previous = current.get(definition.id)
            value = ResultValue(
                org_id=task.org_id, analysis_task_id=task.id, physical_sample_id=task.physical_sample_id,
                assignment_id=task.sample_id or sample.id, metric_definition_id=definition.id,
                ingest_event_id=event.id, **value_columns(definition, row["value"]), unit=row["unit"],
                collected_at=collected_at, parser_version=self._parser(command),
                result_version=self.values.max_version(task.id, definition.id) + 1,
                revises_id=previous.id if previous is not None else "",
                # 越界、逻辑冲突才置可疑；「模拟示意值」「批次级读数」只是来历说明
                quality="suspect" if row["flags"] else "unassessed", review_state="pending",
                provenance="device", entered_by="", flags=flags,
                station_id=command.station_id, instrument=instrument,
            )
            self.db.add(value)
            self.db.flush()
            if previous is not None:
                # 同一步重做（返工、续跑）的新读数：取代上一版，旧版保留，重新待复核（别的步骤回报的已在 _this_step_only 去掉）
                previous.superseded_by_id = value.id
                previous.row_version = int(previous.row_version or 0) + 1
        return len(prepared)

    def _simulated(self, station_id: str, origin: str) -> str:
        """这一步的值是不是模拟出来的：builtin 内置模拟适配器，device 外部模拟设备，空串是真设备。

        外部模拟设备走真实协议、回执和真设备一样，只能看工位当前采用的接入验收：放行这版连接配置的那次验收
        是由自报为模拟器的设备通过的（正式环境拒绝接入模拟器，那里不会出现）。"""
        if origin == "simulation":
            return "builtin"
        from ..models import AcceptanceRun, Adapter

        adapter = self.db.query(Adapter).filter(Adapter.station_id == station_id).first()
        run = self.db.get(AcceptanceRun, adapter.accepted_run_id) if adapter is not None and adapter.accepted_run_id else None
        return "device" if run is not None and run.simulator else ""

    def _instrument(self, station_id: str, simulated: str) -> str:
        """测出这个值的仪器：工位关联资产的序列号（没有就资产编号），模拟的另外注明。"""
        from ..repositories.resources import AssetRepository, StationRepository

        station = StationRepository(self.db, self.ctx).get(station_id)
        asset = AssetRepository(self.db, self.ctx).get(station.asset_id) if station and station.asset_id else None
        label = (asset.serial or asset.asset_no) if asset is not None else ""
        prefix = {"builtin": "内置模拟", "device": "模拟设备"}.get(simulated)
        if prefix:
            return f"{prefix}（{label}）" if label else prefix
        return label

    @staticmethod
    def _parser(command: Command) -> str:
        method = command.method or {}
        return f"{method.get('code')} v{method.get('version')}" if method.get("code") else ""
