"""一个方案分多批执行的纯规则：每批几个样本、矩阵每批几次重复、拆分方式对应的依赖、父任务汇总，
按样本计通道的排程，以及跨批合并统计里的批次差异。不需要数据库。"""
from datetime import datetime, timedelta

import pytest

from app.domain import statistics
from app.domain import tasks as rules
from app.domain.capability import StationSpec
from app.domain.scheduling import WORK, Interval, SchedulingContext, SchedulingError, plan_steps
from app.domain.statistics import Observation, build_dataset

T0 = datetime(2026, 9, 28, 8, 0)


# ---------- 分批 ----------


def test_default_split_uses_fewest_batches_and_balances_them():
    assert rules.split_sizes(20, 8) == [7, 7, 6], "3 批，每批样本数接近"
    assert rules.split_sizes(16, 8) == [8, 8]
    assert rules.split_sizes(8, 8) == [8]
    assert rules.split_sizes(20, 8, per_batch=8) == [8, 8, 4], "指定每批几个就按它装满"
    assert rules.split_sizes(20, 8, parts=4) == [5, 5, 5, 5]
    assert rules.offsets([7, 7, 6]) == [0, 7, 14]


def test_no_batch_may_exceed_the_flow_capacity():
    with pytest.raises(ValueError, match="超过流程每批样品位"):
        rules.split_sizes(20, 8, per_batch=9)
    with pytest.raises(ValueError, match="至少要分 3 份"):
        rules.split_sizes(20, 8, parts=2)
    with pytest.raises(ValueError):
        rules.split_sizes(0, 8)


def test_matrix_batches_are_complete_blocks():
    """按重复拆，每批都包含全部条件：2 个条件各 10 次、每批最多 8 位 → 每批每个条件 4/3/3 次。"""
    assert rules.matrix_blocks(2, 10, 8) == [4, 3, 3]
    assert rules.matrix_blocks(2, 10, 8, per_batch=8) == [4, 4, 2]
    assert rules.matrix_blocks(2, 10, 8, per_batch=7) == [3, 3, 3, 1], "每批向下取整到条件数的整数倍"
    assert rules.matrix_blocks(4, 2, 8, parts=2) == [1, 1]
    with pytest.raises(ValueError, match="每批放不下全部条件"):
        rules.matrix_blocks(12, 2, 8)
    with pytest.raises(ValueError, match="每批至少要有每个条件的一次重复"):
        rules.matrix_blocks(2, 2, 8, parts=3)


def test_split_modes_only_add_dependencies_for_real_process_order():
    ids = ["T1", "T2", "T3"]
    assert rules.split_dependencies(ids, "parallel") == {task: ([], "run_completed") for task in ids}
    pilot = rules.split_dependencies(ids, "pilot")
    assert pilot["T1"] == ([], "run_completed")
    assert pilot["T2"] == (["T1"], "data_validated") and pilot["T3"] == (["T1"], "data_validated")
    sequential = rules.split_dependencies(ids, "sequential")
    assert sequential["T2"] == (["T1"], "run_completed") and sequential["T3"] == (["T2"], "run_completed")
    with pytest.raises(ValueError):
        rules.split_dependencies(ids, "whenever")


def test_portion_counts():
    assert rules.portion_count({}) is None
    assert rules.portion_count({"count": 7, "offset": 7}) == 7
    assert rules.portion_count({"repeats": 3, "offset": 4}, conditions=2) == 6
    assert rules.portion_count({"groups": {"C01": 1, "C02": 2}}) == 3


def test_a_shortfall_keeps_the_parent_open():
    assert rules.aggregate(["done", "done", "cancelled"]) == "done", "没有短缺时取消的子任务照旧不算"
    assert rules.aggregate(["done", "shortfall"]) == "shortfall", "某个子树待补测，整件事就还没完"
    assert rules.aggregate(["running", "shortfall"]) == "running"
    assert rules.shortfall(20, 13, 0, 0) == 7
    assert rules.shortfall(20, 13, 7, 0) == 0, "补测中的样本算在还在做的里面"
    assert rules.shortfall(20, 13, 0, 7) == 0, "签名放弃之后没有短缺"


# ---------- 批次差异 ----------


def _obs(batch: str, value: float, group: str = "C01") -> Observation:
    return Observation(
        assignment_id=f"{batch}-{value}", analysis_task_id="T", round_no=1, metric_id="M", result_version=1,
        value=value, unit="mAh", quality="valid", review_state="approved", condition_group=group, batch_id=batch,
    )


def test_f_distribution_tail_matches_tables():
    assert statistics.f_survival(3.885, 2, 12) == pytest.approx(0.05, abs=5e-4)
    assert statistics.f_survival(4.965, 1, 10) == pytest.approx(0.05, abs=5e-4)
    assert statistics.f_survival(0, 2, 10) == 1.0


def test_batch_breakdown_and_effect():
    same = [_obs("B1", v) for v in (3.1, 3.2, 3.3)] + [_obs("B2", v) for v in (3.15, 3.2, 3.25)]
    dataset = build_dataset(same, "M")
    rows = statistics.batch_breakdown(dataset)
    assert [row["batch_id"] for row in rows] == ["B1", "B2"] and rows[0]["n_included"] == 3
    quiet = statistics.batch_effect(dataset)
    assert quiet["df1"] == 1 and quiet["df2"] == 4 and quiet["significant"] is False

    shifted = [_obs("B1", v) for v in (3.1, 3.2, 3.3)] + [_obs("B2", v) for v in (2.1, 2.2, 2.3)]
    loud = statistics.batch_effect(build_dataset(shifted, "M"))
    assert loud["significant"] is True and loud["p"] < 0.001

    assert statistics.batch_effect(build_dataset([_obs("B1", 1.0), _obs("B1", 2.0)], "M")) is None, "一批谈不上批次差异"


def test_matrix_batch_effect_is_measured_after_removing_condition_means():
    """两个条件差得很远，但每批都有两个条件：减去条件均值后批次之间没有差异。"""
    rows = []
    for batch in ("B1", "B2"):
        rows += [_obs(batch, v, "C01") for v in (1.0, 1.1)] + [_obs(batch, v, "C02") for v in (5.0, 5.1)]
    effect = statistics.batch_effect(build_dataset(rows, "M"))
    assert effect["method"] == "anova_centered"
    assert effect["df2"] == 8 - 2 - 1, "残差自由度扣掉条件组"
    assert effect["significant"] is False


# ---------- 按样本计通道 ----------

CYCLER = StationSpec(id="CYC", channels=8, per_sample=True, limits={"cap.test": {}})
TEST_STEP = {"name": "循环", "cap": "cap.test", "dur": 60}


def _ctx(**extra) -> SchedulingContext:
    return SchedulingContext(stations=[CYCLER], busy={}, clean_min=0, **extra)


def test_per_sample_channels_count_the_samples_of_each_batch():
    ctx = _ctx()
    first = next(a for a in plan_steps([TEST_STEP], T0, ctx, samples=8) if a.kind == WORK)
    assert first.units == 8 and first.starts_at == T0
    second = next(a for a in plan_steps([TEST_STEP], T0, ctx, samples=8) if a.kind == WORK)
    assert second.starts_at == T0 + timedelta(minutes=60), "8 通道一次只放得下一批 8 颗"

    ctx = _ctx()
    five = next(a for a in plan_steps([TEST_STEP], T0, ctx, samples=5) if a.kind == WORK)
    three = next(a for a in plan_steps([TEST_STEP], T0, ctx, samples=3) if a.kind == WORK)
    assert five.starts_at == three.starts_at == T0, "5 颗加 3 颗正好 8 个通道，可以同时跑"


def test_per_batch_channels_are_unchanged():
    per_batch = StationSpec(id="CYC", channels=2, limits={"cap.test": {}})
    ctx = SchedulingContext(stations=[per_batch], busy={}, clean_min=0)
    windows = [next(a for a in plan_steps([TEST_STEP], T0, ctx, samples=8) if a.kind == WORK) for _ in range(3)]
    assert [w.units for w in windows] == [1, 1, 1], "按批计的工位一个批次占 1 份，与样本数无关"
    assert [w.starts_at for w in windows] == [T0, T0, T0 + timedelta(minutes=60)]


def test_a_batch_larger_than_the_channels_never_fits():
    with pytest.raises(SchedulingError, match="放不下这一批 9 个样本"):
        plan_steps([TEST_STEP], T0, _ctx(), samples=9)


def test_existing_windows_carry_their_units_into_asset_capacity():
    busy = {"CYC": [Interval(T0, T0 + timedelta(minutes=30), 6)]}
    ctx = SchedulingContext(
        stations=[CYCLER], busy=busy, station_asset={"CYC": "ASSET"}, asset_capacity={"ASSET": 8}, clean_min=0,
    )
    work = next(a for a in plan_steps([TEST_STEP], T0, ctx, samples=3) if a.kind == WORK)
    assert work.starts_at == T0 + timedelta(minutes=30), "已占 6 份，再要 3 份超过资产容量 8"


def test_cpsat_uses_sample_demands_on_per_sample_stations():
    pytest.importorskip("ortools")
    from app.domain.cpsat import JobSpec, StepSpec, solve

    jobs = [JobSpec("B1", (StepSpec(0, 30, ("CYC",)),), samples=8), JobSpec("B2", (StepSpec(0, 30, ("CYC",)),), samples=8)]
    full = solve(jobs, {"CYC": 8}, {}, per_sample={"CYC"})
    assert full.span_min == 60, "两批各 8 颗，8 通道只能一批接一批"
    small = [JobSpec("B1", (StepSpec(0, 30, ("CYC",)),), samples=5), JobSpec("B2", (StepSpec(0, 30, ("CYC",)),), samples=3)]
    assert solve(small, {"CYC": 8}, {}, per_sample={"CYC"}).span_min == 30
    assert solve(jobs, {"CYC": 8}, {"CYC": [(0, 30, 1)]}, per_sample={"CYC"}).span_min == 90, "已有占用带份数"
