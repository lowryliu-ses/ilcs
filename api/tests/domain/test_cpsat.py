"""CP-SAT 多批次模型。没装 ortools 的环境跳过（服务层此时用内置顺序搜索）。"""
import pytest

pytest.importorskip("ortools")

from app.domain.cpsat import JobSpec, StepSpec, solve  # noqa: E402


def test_parallel_channels_let_two_batches_overlap_on_a_cycler():
    jobs = [
        JobSpec("B1", (StepSpec(0, 30, ("CYC",)),)),
        JobSpec("B2", (StepSpec(0, 30, ("CYC",)),)),
    ]
    one = solve(jobs, {"CYC": 1}, {})
    two = solve(jobs, {"CYC": 2}, {})
    assert (one.status, one.span_min) == ("optimal", 60)
    assert (two.status, two.span_min) == ("optimal", 30), "两通道时两批同时跑"
    assert one.gap_pct == 0.0


def test_dependency_graph_runs_branches_in_parallel_and_joins():
    # 0 → {1 在 A, 2 在 B} → 3：分支并行，汇合等两边都完
    steps = (
        StepSpec(0, 10, ("A",)),
        StepSpec(1, 20, ("A",), preds=(0,)),
        StepSpec(2, 40, ("B",), preds=(0,)),
        StepSpec(3, 5, ("A",), preds=(1, 2)),
    )
    solution = solve([JobSpec("B1", steps)], {"A": 1, "B": 1}, {}, transfer_min=10)
    starts = solution.starts["B1"]
    assert starts[1] == 10 and starts[2] == 20, "换工位的分支要加转运时长"
    assert starts[3] >= 20 + 40 + 10, "汇合等最后一个前驱结束并转运回来"


def test_existing_bookings_and_hard_gap_are_respected():
    steps = (StepSpec(0, 10, ("A",)), StepSpec(1, 10, ("A", "B"), preds=(0,), max_gap=5))
    solution = solve([JobSpec("B1", steps)], {"A": 1, "B": 1}, {"A": [(10, 60)], "B": []}, transfer_min=3)
    assert solution.stations["B1"][1] == "B", "A 被占到 60，硬时限 5 min 内只能去 B"
    assert solution.starts["B1"][1] == 13


def test_order_prefers_heavier_weighted_batch_first():
    jobs = [
        JobSpec("LOW", (StepSpec(0, 30, ("A",)),), weight=1),
        JobSpec("URGENT", (StepSpec(0, 30, ("A",)),), weight=5),
    ]
    assert solve(jobs, {"A": 1}, {}).order == ["URGENT", "LOW"]


def test_infeasible_hard_gap_is_reported_not_guessed():
    steps = (StepSpec(0, 10, ("A",)), StepSpec(1, 10, ("B",), preds=(0,), max_gap=5))
    solution = solve([JobSpec("B1", steps)], {"A": 1, "B": 1}, {}, transfer_min=10)
    assert solution.status == "infeasible" and not solution.order
