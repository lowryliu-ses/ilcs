"""执行前仿真与验证。

两种用法：

- **方法可执行性**（`feasibility`）：在空时间线上跑一遍——结构完整、每条分支路径都走得完、没有永远走不到的
  节点、能力与硬时限在没有别的批次干扰时排得下。提交评审与批准都要求它通过：一个在空实验室里都排不下、
  或者有走不到的节点的方法，批准了也没法执行。
- **按需仿真**（`simulate`）：在此基础上叠加当前时间线与 N 个并发批次，看排队等待、工位负荷、转运、
  物料够不够、依赖哪些外部事件。这些随现场状态变化，只给提示，不挡审批。

所有排程判断都交给同一个排程器（`scheduling.plan_steps`），仿真不另造一套约束。结果记在方法上
（内容摘要 + 结论），界面据此显示「已验证」；方法内容改过，旧的结论自动失效。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from ..core.clock import as_utc, now
from ..core.context import AccessContext
from ..core.errors import NotFound, StateConflict
from ..domain import dryrun, graph
from ..domain.recipe_rules import validate_steps
from ..domain.scheduling import TRANSFER, WORK, SchedulingError, makespan, plan_steps
from ..domain.steps import KIND_NAMES, WAIT, kind_of, normalize, step_id_of
from ..domain.subflow import SubflowError, has_subflow, merge_bom
from ..models import Recipe
from ..repositories.recipes import RecipeRepository
from ..repositories.materials import LotRepository
from ..repositories.resources import CapabilityRepository, StationRepository
from .flow_expansion import expanded_steps, resolved_steps
from .inventory_service import InventoryService
from .schedule_service import ScheduleService

PASS, WARN, BLOCKED, NA = "pass", "warn", "blocked", "not_applicable"


def content_hash(recipe: Recipe) -> str:
    body = json.dumps({"steps": recipe.steps or [], "bom": recipe.bom or []}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(body.encode()).hexdigest()[:16]


def _check(key: str, label: str, state: str, detail: str, **extra) -> dict:
    return {"key": key, "label": label, "state": state, "ok": state != BLOCKED, "detail": detail, **extra}


class SimulationService:
    def __init__(self, db: Session, ctx: AccessContext):
        self.db = db
        self.ctx = ctx
        self.recipes = RecipeRepository(db, ctx)
        self.stations = StationRepository(db, ctx)
        self.capabilities = CapabilityRepository(db)
        self.schedule = ScheduleService(db, ctx)

    def _require(self, recipe_id: str) -> Recipe:
        recipe = self.recipes.get(recipe_id)
        if recipe is None:
            raise NotFound("流程不存在")
        return recipe

    # ---------- 入口 ----------

    def feasibility(self, recipe: Recipe, persist: bool = True) -> dict:
        return self._run(recipe, concurrency=1, start_from=None, use_timeline=False, persist=persist)

    def simulate(self, recipe_id: str, concurrency: int = 1, start_from: datetime | None = None,
                 use_timeline: bool = True) -> dict:
        recipe = self._require(recipe_id)
        result = self._run(recipe, concurrency=max(1, min(int(concurrency), 20)), start_from=start_from,
                           use_timeline=use_timeline, persist=True)
        self.db.commit()
        return result

    def require_feasible(self, recipe: Recipe, action: str) -> dict:
        """提交评审 / 批准前的硬门槛。结论写在流程上，不通过就拒绝并列出原因。"""
        result = self.feasibility(recipe, persist=True)
        if not result["ok"]:
            raise StateConflict(
                f"执行前仿真未通过，不能{action}",
                {"blocked": [{"key": row["key"], "label": f"{row['label']}：{row['detail']}"}
                             for row in result["checks"] if row["state"] == BLOCKED]},
                code="simulation_failed",
            )
        return result

    # ---------- 仿真 ----------

    def _expanded(self, recipe: Recipe) -> tuple[list[dict], list[dict], str]:
        steps = normalize(recipe.steps or [])
        bom = list(recipe.bom or [])
        if not has_subflow(steps):
            return resolved_steps(self.db, self.ctx, steps)[0], bom, ""
        try:
            expanded, extra = expanded_steps(self.db, self.ctx, recipe)
        except SubflowError as error:
            return steps, bom, error.message
        return expanded, merge_bom(bom, extra), ""

    def _run(self, recipe: Recipe, *, concurrency: int, start_from: datetime | None, use_timeline: bool,
             persist: bool) -> dict:
        steps, bom, expand_error = self._expanded(recipe)
        checks: list[dict] = []
        begin = as_utc(start_from) or (now() + timedelta(minutes=5))

        # 1 结构
        validation = validate_steps(steps, self.stations.specs(), self.capabilities.specs()) if not expand_error else []
        broken = [row for row in validation if not row["ok"]]
        if expand_error:
            checks.append(_check("structure", "流程结构", BLOCKED, f"子流程无法展开：{expand_error}"))
        elif not steps:
            checks.append(_check("structure", "流程结构", BLOCKED, "流程没有步骤"))
        elif broken:
            checks.append(_check(
                "structure", "流程结构", BLOCKED,
                "；".join(f"第 {row['index'] + 1} 步：{(row['issues'] or row['blockers'] or ['不可承接'])[0]}" for row in broken[:5]),
            ))
        else:
            checks.append(_check(
                "structure", "流程结构", PASS,
                f"{len(steps)} 步（展开子流程后），节点类型："
                + "、".join(f"{KIND_NAMES.get(k, k)} {n}" for k, n in _count_kinds(steps).items()),
            ))
        if expand_error or not steps:
            return self._finish(recipe, checks, [], [], {}, concurrency, persist)

        durations = [float(step.get("dur") or 0) for step in steps]
        # 2 分支路径与可达性
        found, truncated = dryrun.paths(steps, durations)
        dead = dryrun.unreachable(steps, found)
        ids = [step_id_of(step, index) for index, step in enumerate(steps)]
        path_rows = [
            {"choices": dryrun.case_labels(steps, path.choices), "steps": len(path.executed),
             "duration_min": path.duration_min}
            for path in found
        ]
        if dead:
            checks.append(_check(
                "reachable", "所有节点可达", BLOCKED,
                "任何一种分支走法都到不了：" + "、".join(f"第 {i + 1} 步「{steps[i].get('name') or ids[i]}」" for i in dead),
            ))
        else:
            longest = max((row["duration_min"] for row in path_rows), default=0)
            shortest = min((row["duration_min"] for row in path_rows), default=0)
            checks.append(_check(
                "reachable", "所有节点可达", WARN if truncated else PASS,
                f"{len(path_rows)} 种分支走法{'（组合过多，只列出前 ' + str(dryrun.MAX_PATHS) + ' 种）' if truncated else ''}，"
                f"关键路径 {shortest:g}–{longest:g} min",
            ))
        loops = dryrun.loop_overhead(steps, durations)
        if loops:
            worst = sum(row["extra_min_worst"] for row in loops)
            checks.append(_check(
                "loops", "回环有上限", PASS,
                "；".join(f"{row['label']} 最多 {row['max_loops']} 次，最坏多 {row['extra_min_worst']:g} min" for row in loops)
                + f"；合计最坏多 {worst:g} min",
            ))
        else:
            checks.append(_check("loops", "回环有上限", NA, "流程没有回环"))

        # 3 空时间线：能力、参数范围、硬时限、转运在没有别的批次干扰时排不排得下
        empty = self.schedule.context({"__none__"})
        empty.busy = {}
        empty.asset_bookings = {}
        empty.held_station_ids = set()
        empty.unavailable_station_ids = {}
        empty.allow_unclean = True
        # 方法本身能不能执行，与工位此刻是否故障 / 离线无关：只剔除已退役的工位
        empty.stations = [replace(spec, status="idle") for spec in empty.stations if not spec.retired]
        planned = []
        try:
            planned = plan_steps(steps, begin, empty)
            span = round(makespan(planned).total_seconds() / 60)
            used = "、".join(sorted({a.station_id for a in planned if a.kind == WORK}))
            checks.append(_check(
                "resources", "能力与硬时限可排", PASS,
                (f"空实验室里排得下：跨度 {span} min，用到工位 {used}" if planned else "流程不占工位"),
            ))
        except SchedulingError as error:
            checks.append(_check("resources", "能力与硬时限可排", BLOCKED, f"空实验室里都排不下：{error.message}"))

        # 4 转运路径
        transfers = [a for a in planned if a.kind == TRANSFER]
        moves = _station_changes(steps, planned)
        if not moves:
            checks.append(_check("transfer", "转运路径", NA, "各步在同一工位或不占工位，不需要转运"))
        elif not empty.transfer_station_ids:
            checks.append(_check("transfer", "转运路径", WARN, f"需要 {moves} 次换工位，但没有承运工位（AGV / 机械臂）：需要人工搬运"))
        else:
            checks.append(_check(
                "transfer", "转运路径", PASS,
                f"{moves} 次换工位，已排 {len(transfers)} 段转运（承运工位 {'、'.join(empty.transfer_station_ids)}）",
            ))

        # 5 当前时间线 + 并发
        load: dict[str, float] = {}
        if use_timeline:
            checks.append(self._timeline_check(steps, begin, concurrency, planned, load))

        # 6 物料
        checks.append(self._material_check(bom, concurrency))

        # 7 外部事件
        waits = sorted({
            (step.get("wait_for") or {}).get("event") for step in steps
            if kind_of(step) == WAIT and (step.get("wait_for") or {}).get("mode") == "event"
        } - {None, ""})
        checks.append(_check(
            "events", "外部事件", WARN if waits else NA,
            (f"执行时要等外部事件：{'、'.join(waits)}（仿真按计划时长估算；上线前确认发送方已接入）" if waits
             else "流程不依赖外部事件"),
        ))
        return self._finish(recipe, checks, path_rows, loops, load, concurrency, persist)

    def _timeline_check(self, steps: list[dict], begin: datetime, concurrency: int, alone, load: dict) -> dict:
        context = self.schedule.context({"__none__"})
        try:
            plans = [plan_steps(steps, begin, context) for _ in range(concurrency)]
        except SchedulingError as error:
            return _check("timeline", f"当前时间线上 {concurrency} 个批次", WARN, f"排不下：{error.message}")
        work = [a for plan in plans for a in plan if a.kind == WORK]
        if not work:
            return _check("timeline", f"当前时间线上 {concurrency} 个批次", NA, "流程不占工位")
        first_start = min(a.starts_at for a in work)
        last_end = max(a.ends_at for a in work)
        alone_span = makespan(alone).total_seconds() / 60 if alone else 0
        span = (last_end - begin).total_seconds() / 60
        busy: dict[str, float] = defaultdict(float)
        for a in work:
            busy[a.station_id] += (a.ends_at - a.starts_at).total_seconds() / 60
        load.update({station: round(minutes) for station, minutes in sorted(busy.items(), key=lambda item: -item[1])})
        wait = max(0.0, (first_start - begin).total_seconds() / 60)
        state = WARN if wait > 60 or span > alone_span * concurrency * 1.5 + 60 else PASS
        return _check(
            "timeline", f"当前时间线上 {concurrency} 个批次", state,
            f"最早 {first_start:%m-%d %H:%M} 开工（等待 {wait:.0f} min），全部 {last_end:%m-%d %H:%M} 完成，"
            f"总跨度 {span:.0f} min；最忙工位 {next(iter(load), '—')}",
        )

    def _material_check(self, bom: list[dict], concurrency: int) -> dict:
        if not bom:
            return _check("materials", "物料够用", NA, "流程不消耗 BOM 物料")
        lots = LotRepository(self.db, self.ctx)
        inventory = InventoryService(self.db, self.ctx)
        short = []
        rows = []
        for item in bom:
            need = Decimal(str(item.get("qty") or 0)) * concurrency
            available = sum(
                (inventory.balances(lot)["available"] for lot in lots.released_for(item.get("material"), item.get("unit"))),
                Decimal(0),
            )
            rows.append(f"{item.get('material')} 需 {need.normalize()}{item.get('unit')} / 可用 {available.normalize()}")
            if available < need:
                short.append(item.get("material"))
        return _check(
            "materials", "物料够用", WARN if short else PASS,
            ("不足：" + "、".join(short) + "；" if short else "") + "；".join(rows),
        )

    def _finish(self, recipe: Recipe, checks: list[dict], path_rows: list[dict], loops: list[dict], load: dict,
                concurrency: int, persist: bool) -> dict:
        ok = all(row["state"] != BLOCKED for row in checks)
        result = {
            "recipe_id": recipe.id, "version": recipe.version, "content_hash": content_hash(recipe),
            "at": now().isoformat(timespec="seconds"), "ok": ok, "concurrency": concurrency,
            "checks": checks, "paths": path_rows, "loops": loops, "station_load_min": load,
            "summary": {
                "blocked": len([row for row in checks if row["state"] == BLOCKED]),
                "warn": len([row for row in checks if row["state"] == WARN]),
                "pass": len([row for row in checks if row["state"] == PASS]),
            },
        }
        if persist:
            recipe.simulation = result
        return result


def _count_kinds(steps: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for step in steps:
        counts[kind_of(step)] = counts.get(kind_of(step), 0) + 1
    return counts


def _station_changes(steps: list[dict], planned) -> int:
    """沿依赖边数「前驱与后继落在不同工位」的次数：每一次都意味着一段转运。"""
    station = {a.step_index: a.station_id for a in planned if a.kind == WORK}
    before = graph.predecessors(steps)
    changes = 0
    for index, parents in enumerate(before):
        if index not in station:
            continue
        for parent in parents:
            if parent in station and station[parent] != station[index]:
                changes += 1
    return changes

