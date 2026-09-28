"""核心链路三次评审（2026-09-28）排程回归：服务层用例。

真实调用 ScheduleService / RescheduleService 的方法，仓储与上下文用替身，不经过数据库（与评审附件
reproduce_scheduling.py、补充复核 supplement/scheduling 同一写法）。目标行为；修复前标 xfail(strict=True)。
"""
from datetime import datetime, timedelta
from types import SimpleNamespace as N
from unittest.mock import MagicMock, patch

import pytest

from app.domain.capability import StationSpec
from app.domain.scheduling import Interval, SchedulingContext, peak_load
from app.models import Allocation, Station
from app.repositories.batches import AllocationRepository
from app.services.reschedule_service import RescheduleService
from app.services.schedule_service import ScheduleService

T = datetime(2026, 9, 28, 8)


def M(minutes: float) -> datetime:
    return T + timedelta(minutes=minutes)


def pending(package: str):
    return pytest.mark.xfail(strict=True, reason=f"{package} 待修")


def _alloc(batch_id, index, station, start, end, kind="work", units=1):
    # units：这段时间窗占几份通道（按样本计通道的工位是批次的样本数），与 Allocation 模型一致
    return N(batch_id=batch_id, step_index=index, station_id=station, kind=kind, starts_at=M(start), ends_at=M(end),
             units=units)


def _device(step_id, cap, dur, after):
    return {"step_id": step_id, "name": step_id, "cap": cap, "dur": dur, "after": after}


# ---------- T05 经静置汇合的提前对齐（WP10） ----------


def test_realign_through_a_wait_join_waits_for_the_other_branch():
    """A、B 并行 → 静置 10 min → C。B 仍跑到 09:30，A 提前 30 min：C 不能被提前到 09:10，仍是 09:40。"""
    steps = [
        {"step_id": "prep", "name": "prep", "kind": "wait", "dur": 30, "after": []},
        _device("a", "cap.a", 30, ["prep"]),
        _device("b", "cap.b", 90, []),
        {"step_id": "settle", "name": "settle", "kind": "wait", "dur": 10, "after": ["a", "b"]},
        _device("c", "cap.c", 10, ["settle"]),
    ]
    rows = [_alloc("B", 1, "A", 60, 90), _alloc("B", 2, "B", 0, 90), _alloc("B", 4, "C", 100, 110)]
    repo = MagicMock()
    repo.for_batch.return_value = rows
    repo.work_step.return_value = rows[0]
    repo.shift_steps.side_effect = lambda bid, shifts: AllocationRepository.shift_steps(repo, bid, shifts)
    service = object.__new__(ScheduleService)
    service.db, service.ctx, service.allocations, service.audit = MagicMock(), MagicMock(), repo, MagicMock()
    service.frozen_steps = lambda _: {0, 1, 2}
    service.step_ends = lambda _: {0: M(30), 1: M(90), 2: M(90), 3: M(100), 4: M(110)}
    service._cross_batch_overlaps = lambda _: []
    service._raise_dependency_alarm = lambda *a, **k: None
    service.dependency_conflicts = lambda _: []
    service._feasibility_problems = lambda _: []
    batch = N(id="B", recipe_snapshot={"steps": steps})
    with patch("app.services.schedule_service.lock_schedule"):
        service.realign(batch, 1, M(30))
    assert rows[2].starts_at == M(100), rows[2].starts_at


# ---------- T08 纯等待流程的重排建议（WP10） ----------


def _pure_wait_services(batch):
    schedule = object.__new__(ScheduleService)
    schedule.db, schedule.ctx = MagicMock(), MagicMock()
    schedule.allocations = MagicMock()
    schedule.allocations.for_batch.return_value = []
    schedule.context = lambda _: SchedulingContext(stations=[])
    schedule.upstream_batch_ids = lambda _: []
    schedule._task_of = lambda _: None
    schedule.dependency_floor = lambda *a, **k: (None, [])
    schedule.carrier_roles = lambda _: set()
    reschedule = object.__new__(RescheduleService)
    reschedule.db, reschedule.ctx = MagicMock(), MagicMock()
    reschedule.schedule = schedule
    reschedule.allocations = schedule.allocations
    reschedule.batches = MagicMock()
    reschedule.batches.get.return_value = batch
    reschedule.audit = MagicMock()
    return schedule, reschedule


def test_applied_proposal_for_a_pure_wait_flow_moves_the_planned_start():
    batch = N(id="W", state="scheduled", priority=2, created_at=T, planned_start_at=M(120),
              recipe_snapshot={"steps": [{"step_id": "w", "name": "静置", "kind": "wait", "dur": 60}]})
    schedule, reschedule = _pure_wait_services(batch)
    with patch("app.repositories.workflow.StepRunRepository") as runs, \
            patch("app.services.reschedule_service.now", return_value=T), \
            patch("app.services.schedule_service.lock_schedule"), \
            patch("app.repositories.resources.StationRepository") as stations:
        runs.return_value.for_batch.return_value = []
        stations.return_value.list.return_value = []
        after, impact, _ = reschedule.plan([batch], {})
        proposal = N(id="P", state="pending", before={"W": []}, after=after, impact=impact, trigger="manual")
        reschedule._require = lambda _: proposal
        reschedule._live_indices = lambda _: set()
        reschedule._final_problems = lambda _: []
        reschedule.out = lambda row: {"state": row.state}
        assert reschedule.apply("P", None)["state"] == "applied"
        assert batch.planned_start_at == M(5)
        assert schedule.batch_end(batch) == M(65)


# ---------- A07 提前对齐也查共享资产（WP11） ----------


def _asset_realign_service(all_rows):
    def query(*args):
        if args and args[0] is Station.id and len(args) > 1 and args[1] is Station.channels:
            return N(all=lambda: [("ST-A", 1), ("ST-B", 1), ("ST-C", 1)])
        if args and args[0] is Allocation:
            chain = N()
            chain.join = lambda *a, **k: chain
            chain.filter = lambda *a, **k: chain
            chain.all = lambda: [(row, "ORG") for row in all_rows]
            return chain
        raise AssertionError(f"unexpected query {args}")

    repo = MagicMock()
    repo.for_batch.side_effect = lambda bid: [row for row in all_rows if row.batch_id == bid]
    repo.work_step.side_effect = lambda bid, i: next(
        row for row in all_rows if row.batch_id == bid and row.step_index == i and row.kind == "work"
    )
    repo.shift_steps.side_effect = lambda bid, shifts: AllocationRepository.shift_steps(repo, bid, shifts)
    service = object.__new__(ScheduleService)
    service.db = MagicMock()
    service.db.query.side_effect = query
    service.ctx = N(org_id="ORG")
    service.allocations = repo
    service.audit = MagicMock()
    service.frozen_steps = lambda _: {0}
    service.step_ends = lambda _: {0: M(90), 1: M(120)}
    return service


def test_early_realign_is_withdrawn_when_it_would_overload_a_shared_asset():
    """资产 X（容量 1）映射 ST-A 与 ST-B；P 在 ST-B 09:00-09:30。Q 第 1 步不能被提前到与 P 同时。"""
    rows_q = [_alloc("Q", 0, "ST-C", 0, 90), _alloc("Q", 1, "ST-A", 90, 120), _alloc("Q", 1, "ST-A", 120, 130, "clean")]
    rows_p = [_alloc("P", 0, "ST-B", 60, 90)]
    all_rows = rows_q + rows_p
    service = _asset_realign_service(all_rows)
    service._asset_overloads = lambda batch_id, planned=None, ignore_ids=frozenset(): (
        ["资产 X 09:00-09:30 超容量"] if batch_id == "Q" and peak_load(
            [(Interval(row.starts_at, row.ends_at), 1) for row in all_rows if row.station_id in {"ST-A", "ST-B"}],
            Interval(rows_q[1].starts_at, rows_q[1].ends_at),
        ) > 1 else []
    )
    batch = N(id="Q", recipe_snapshot={"steps": [
        {"step_id": "s0", "name": "prep", "cap": "cap.c", "dur": 90},
        {"step_id": "s1", "name": "work", "cap": "cap.a", "dur": 30},
    ]})
    with patch("app.services.schedule_service.lock_schedule"):
        result = service.realign(batch, 1, M(60))
    assert result["shifted_min"] == 0 and result.get("waiting"), result
    assert (rows_q[1].starts_at, rows_q[1].ends_at) == (M(90), M(120))


# ---------- A08 保持延长不移动已完成的转运（WP11） ----------


def test_extend_after_hold_leaves_the_finished_transfer_in_place():
    rows = [_alloc("B", 0, "ST-A", 0, 30), _alloc("B", 1, "AGV-01", 30, 40, "transfer"), _alloc("B", 1, "ST-B", 40, 70),
            _alloc("B", 1, "ST-B", 70, 80, "clean"), _alloc("B", 2, "ST-C", 80, 110)]
    service = object.__new__(ScheduleService)
    service.allocations = MagicMock()
    service.allocations.for_batch.return_value = rows
    service.frozen_steps = lambda _: {0, 1}
    batch = N(id="B", recipe_snapshot={"steps": [
        {"step_id": "a", "name": "a", "cap": "cap.a", "dur": 30},
        {"step_id": "b", "name": "b", "cap": "cap.b", "dur": 30},
        {"step_id": "c", "name": "c", "cap": "cap.c", "dur": 30},
    ]})
    with patch("app.services.schedule_service.now", return_value=M(50)):
        service.extend_after_hold(batch, {1}, 60)
    transfer, work, clean, tail = rows[1], rows[2], rows[3], rows[4]
    assert (transfer.starts_at, transfer.ends_at) == (M(30), M(40))
    assert (work.starts_at, work.ends_at) == (M(40), M(130))
    assert (clean.starts_at, clean.ends_at) == (M(130), M(140))
    assert tail.starts_at == M(140)


# ---------- A09 重排建议保留在途批次的窗口（WP11） ----------


def test_proposal_keeps_the_windows_of_a_batch_whose_steps_are_all_frozen():
    step = {"step_id": "x", "name": "x", "cap": "cap.x", "dur": 60}
    running = N(id="R", state="running", priority=2, created_at=T, recipe_snapshot={"steps": [{**step, "dur": 120}]})
    waiting = N(id="S", state="scheduled", priority=2, created_at=T, recipe_snapshot={"steps": [step]})
    rows = {
        "R": [_alloc("R", 0, "ST-X", 0, 120)],
        "S": [_alloc("S", 0, "ST-X", 130, 190)],
    }
    schedule = object.__new__(ScheduleService)
    schedule.db, schedule.ctx = MagicMock(), MagicMock()
    schedule.allocations = MagicMock()
    schedule.allocations.for_batch.side_effect = lambda bid: list(rows[bid])
    schedule.context = lambda *a, **k: SchedulingContext(stations=[StationSpec(id="ST-X", limits={"cap.x": {}})], clean_min=0)
    schedule.upstream_batch_ids = lambda _: []
    schedule._task_of = lambda _: None
    schedule.dependency_floor = lambda *a, **k: (None, [])
    schedule.carrier_roles = lambda _: set()
    schedule.frozen_steps = lambda b: {0} if b.id == "R" else set()
    schedule.step_ends = lambda b: {0: rows[b.id][0].ends_at}
    schedule.batch_end = lambda b: rows[b.id][0].ends_at
    reschedule = object.__new__(RescheduleService)
    reschedule.db, reschedule.ctx = MagicMock(), MagicMock()
    reschedule.schedule, reschedule.allocations = schedule, schedule.allocations
    reschedule.batches, reschedule.audit = MagicMock(), MagicMock()
    with patch("app.services.reschedule_service.now", return_value=T):
        after, _, _ = reschedule.plan([running, waiting], {})
    work = next(row for row in after["S"]["allocations"] if row["kind"] == "work")
    assert datetime.fromisoformat(work["starts_at"]) >= M(120), work
