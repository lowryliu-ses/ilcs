"""核心链路二次评审（2026-09-27）回归。

每条用例对应评审里的一个缺口（N01–N12）或复核时补充的问题，先在未修复的代码上确认会失败，修复后通过：
- N01 设备明确回复「结论未知」：动作保留占用、终止要发给它、不能一键重试
- N02 协同工位与主设备同属一台资产：按工位份数计资产负载，并加上新动作自己要的份数
- N03 / 孔位身份：矩阵参数按样本当前所在的实体孔位下发（分装落到另一块板、布局放进不同板型），
  逐样本质检按同一个孔位读设备回报
- N04 「数据复核通过」逐任务核对指标齐全、审核通过、没有无效数据，并覆盖每个在用样本
- N05 实体分装：孔位规范化；同一块实体载具的同一孔位只能有一个在途样本，子样留在母样孔时交接占用
- N06 两台设备的终止回执并发处理：批次锁内汇总；执行器兜底收尾；界面可以手动重新汇总
- N07 按实际进度对齐只平移本步在图上的后继，不动已开出或无关的分支
- N08 尾段重排按图上每个前驱的实际 / 计划结束排，不按列表里的上一项
- N09 拆分按任务锁定的方案版本取样本，不读方案当前（可能是草稿）的内容
- N10 父任务迁移版本只迁同方案的后代
- N11 协同工位同样要清洗确认；做过需要清洗的协同动作后转为待清洗
- N12 选协同工位时联合核对共享资产容量；预览与写入用同一道容量检查
- 未走的分支不算已开出：滚动重排与手动重排照常处理已选路径
- 载具角色名 main 保留给主载具
"""
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier

import pytest
from sqlalchemy.orm.attributes import flag_modified

from test_automation_extensions import _placements, _run, _with_robot
from test_core_chain_review import (
    CAPACITY, _abort, _approve, _approved_plan, _command, _commands, _detail, _dispatch, _foreign_command,
    _new_batch, _parallel_roots, _plan_task, _reshape, _session, _task, _task_batch, _verify, _without_hard,
)
from test_failure_paths import running_batch  # noqa: F401  （复用 fixture）
from test_gate_split import measured  # noqa: F401
from test_labware_transfer import _register, clean_labware  # noqa: F401


# ---------- 工具 ----------


@pytest.fixture()
def devices(monkeypatch, reset_runtime):
    """可编排的异步真实驱动：在 R1 用例 `scripted` 的基础上可以指定回执状态。

    `submit[工位]` 指定投递回执（缺省 accepted）；`query[指令]` 指定查询回执（缺省：在 `finished` 里回 done，
    否则回 running）。
    """
    from app.adapters.base import AdapterContract, CommandResult
    from app.adapters.registry import REAL_IMPLEMENTATIONS, reset_cache
    from app.core.clock import now
    from app.models import Adapter

    state: dict = {"finished": set(), "submit": {}, "query": {}, "calls": []}
    swapped: dict[str, dict] = {}

    class Device:
        def __init__(self, record):
            self.station_id = record.station_id
            self.contract = AdapterContract(
                kind="real", protocol="test-second-review", supports_query=True, supports_hold=True,
                supports_abort=True,
            )

        def healthcheck(self):
            return {"reachable": True}

        def _result(self, command_id, value):
            return CommandResult(
                command_id=command_id, state=value, device_ts=now(), quality="good",
                origin="real:test-second-review",
            )

        def submit(self, request):
            state["calls"].append((self.station_id, request.type, request.command_id))
            return self._result(request.command_id, state["submit"].get(self.station_id, "accepted"))

        def query(self, command_id):
            if command_id in state["query"]:
                return self._result(command_id, state["query"][command_id])
            return self._result(command_id, "done" if command_id in state["finished"] else "running")

        def hold(self, request):
            return self._result(request.command_id, "done")

        def abort(self, request):
            state["calls"].append((self.station_id, "abort", request.target_command_id))
            return self._result(request.command_id, "done")

    monkeypatch.setitem(REAL_IMPLEMENTATIONS, "test_second_review", Device)

    def use(*station_ids: str) -> dict:
        with _session() as db:
            for station_id in station_ids:
                adapter = db.get(Adapter, station_id)
                swapped.setdefault(
                    station_id,
                    {key: getattr(adapter, key) for key in ("kind", "driver", "protocol", "config_version")},
                )
                adapter.kind, adapter.driver, adapter.protocol = "real", "test_second_review", "test-second-review"
                adapter.config_version += 1
            db.commit()
        reset_cache()
        return state

    state["use"] = use
    yield state
    with _session() as db:
        for station_id, original in swapped.items():
            adapter = db.get(Adapter, station_id)
            for key, value in original.items():
                setattr(adapter, key, value)
            adapter.current_command_id = ""
        db.commit()
    reset_cache()


def _work(batch_id: str, step_index: int):
    from app.models import Allocation

    with _session() as db:
        row = db.query(Allocation).filter(
            Allocation.batch_id == batch_id, Allocation.step_index == step_index, Allocation.kind == "work",
        ).one()
        return row.station_id, row.starts_at, row.ends_at


def _shift_plan(batch_id: str, minutes: float) -> None:
    """把批次的全部时间窗往前挪：模拟「计划是很早以前排的」，此刻开出的步骤就是迟到的。"""
    from app.models import Allocation

    with _session() as db:
        for row in db.query(Allocation).filter(Allocation.batch_id == batch_id).all():
            row.starts_at -= timedelta(minutes=minutes)
            row.ends_at -= timedelta(minutes=minutes)
        db.commit()


def _target_factor(db, batch_id: str, step_id: str, param: str, factor_index: int = 1) -> None:
    """让方案的一个因子作用到某一步的设备参数上，并像建批次时那样按孔位冻结条件参数。"""
    from app.domain.matrix import condition_params
    from app.models import Batch, Sample

    batch = db.get(Batch, batch_id)
    snapshot = dict(batch.plan_snapshot or {})
    factors = [dict(factor) for factor in snapshot.get("factors") or []]
    factors[factor_index] = {**factors[factor_index], "target": {"step_id": step_id, "param": param}}
    rows = [
        {"well": sample.well, "levels": list(sample.levels or [])}
        for sample in db.query(Sample).filter(Sample.batch_id == batch_id).all()
    ]
    snapshot["factors"] = factors
    snapshot["condition_params"] = condition_params(factors, rows)
    batch.plan_snapshot = snapshot
    flag_modified(batch, "plan_snapshot")
    db.commit()


def _split_step(step_id: str = "s01", count: int = 2) -> dict:
    return {
        "step_id": step_id, "kind": "split", "name": "分装扣电",
        "split": {"count": count, "child_type": "扣电", "mode": "physical"},
    }


def _long_wait(step_id: str) -> dict:
    return {"step_id": step_id, "kind": "wait", "name": "静置", "dur": 600, "wait_for": {"mode": "duration"}}


def _split_ready(operator, batch_id: str, executor) -> tuple[dict, dict]:
    detail = _run(operator, batch_id, executor, rounds=6, until=("done", "fault"))
    split_run = next(row for row in detail["step_runs"] if row["kind"] == "split")
    assert split_run["state"] == "ready", detail["failure_reason"]
    return detail, split_run


# ---------- N01 设备明确回复「结论未知」 ----------


def test_device_reporting_unknown_keeps_the_station_until_checked(operator, devices, running_batch, db, executor):
    """N01：设备收到指令却回「结论未知」：动作可能仍在进行，工位不能给别的批次，也不能一键重试。"""
    from app.models import Adapter

    board = devices["use"]("ST-05")
    board["submit"]["ST-05"] = "unknown"
    executor()
    first = _commands(operator, running_batch, "dispatch")[0]
    assert (first["state"], first["delivery_state"]) == ("unknown", "delivered")
    db.expire_all()
    assert db.get(Adapter, "ST-05").current_command_id == first["id"], "设备可能仍在动作：工位上的当前指令不清空"
    options = operator.get(f"/api/batches/{running_batch}/recovery-options").json()
    assert options["blind_retry_allowed"] is False, "设备自己都说不清结果：不能一键重试"

    board["submit"].clear()
    _, other = _foreign_command(operator, "ST-05", "cap.vacuum_dry")
    executor()
    assert _command(db, other).state == "sent", "结论未知的动作保留占用，核查前不投递新动作"
    assert _command(db, first["id"]).outcome == "unknown", "投递事实与动作结论分开记"

    assert _verify(operator, first["id"], "not_executed").status_code == 200
    executor()
    assert _command(db, other).state == "running", "现场确认未执行后占用释放"


def test_status_query_reporting_unknown_keeps_the_station(operator, devices, running_batch, db, executor):
    """N01：设备先接受、之后查询回「结论未知」：同样保留占用。"""
    board = devices["use"]("ST-05")
    executor()
    first = _commands(operator, running_batch, "dispatch")[0]
    assert first["state"] == "running"

    board["query"][first["id"]] = "unknown"
    executor()
    after = _command(db, first["id"])
    assert (after.state, after.delivery_state) == ("unknown", "delivered")
    _, other = _foreign_command(operator, "ST-05", "cap.vacuum_dry")
    executor()
    assert _command(db, other).state == "sent", "查询回「结论未知」同样保留占用"
    assert _command(db, first["id"]).outcome == "unknown"


def test_abort_is_sent_to_a_device_that_reported_unknown(operator, devices, running_batch, db, executor):
    """N01：终止时，结论未知的动作也在要停的目标里；不能不问设备就直接判为已终止。"""
    devices["use"]("ST-05")["submit"]["ST-05"] = "unknown"
    executor()
    first = _commands(operator, running_batch, "dispatch")[0]

    aborted = _abort(operator, running_batch)
    assert aborted.status_code == 200, aborted.text
    assert aborted.json()["state"] == "aborting", "设备可能仍在动作：要等它确认停止"
    stops = _commands(operator, running_batch, "abort")
    assert [(row["station_id"], row["target_command_id"]) for row in stops] == [("ST-05", first["id"])]

    executor()
    assert _detail(operator, running_batch)["state"] == "aborted"
    assert ("ST-05", "abort", first["id"]) in devices["calls"]


def test_explicit_device_failure_still_releases_the_station(operator, devices, running_batch, db, executor):
    """N01 的边界：设备明确回报失败（已停止）不属于「结论未知」，工位照常释放。"""
    board = devices["use"]("ST-05")
    board["submit"]["ST-05"] = "failed"
    executor()
    first = _commands(operator, running_batch, "dispatch")[0]
    assert (first["state"], first["delivery_state"], first["outcome"]) == ("unknown", "delivered", "failed")
    board["submit"].clear()
    _, other = _foreign_command(operator, "ST-05", "cap.vacuum_dry")
    executor()
    assert _command(db, other).state == "running"


# ---------- N02 协同工位与主设备同属一台资产 ----------


def test_assist_on_the_same_asset_counts_every_station_it_holds(operator, devices, db, executor):
    """N02：一条动作同时占用同一资产的主工位和协同工位，就是占了两份容量。"""
    from app.models import Asset, Command, Station

    devices["use"]("ST-05", "AGV-01", "AGV-02")
    stations = ("ST-05", "AGV-01", "AGV-02")
    with _session() as session:
        asset = Asset(
            org_id="ORG-001", asset_no=f"AS-T{uuid.uuid4().hex[:6]}", name="共享资产（测试）", capacity=2,
            calibration_applicable=False, calibration_exempt_reason="测试资产",
        )
        session.add(asset)
        session.flush()
        original = {sid: session.get(Station, sid).asset_id for sid in stations}
        for sid in stations:
            session.get(Station, sid).asset_id = asset.id
        session.commit()
    try:
        _, paired = _foreign_command(operator, "ST-05", "cap.vacuum_dry")
        with _session() as session:
            session.get(Command, paired).assist_station_ids = ["AGV-01"]
            session.commit()
        executor()
        assert _command(db, paired).state == "running"

        _, third = _foreign_command(operator, "AGV-02", "cap.transfer")
        executor()
        assert _command(db, third).state == "sent", "主工位 + 协同工位已占满容量 2，第三个工位不能再开工"

        with _session() as session:
            session.get(Command, paired).state = "cancelled"
            session.commit()
        executor()
        assert _command(db, third).state == "running"

        _, needs_two = _foreign_command(operator, "ST-05", "cap.vacuum_dry")
        with _session() as session:
            session.get(Command, needs_two).assist_station_ids = ["AGV-01"]
            session.commit()
        executor()
        assert _command(db, needs_two).state == "sent", "只剩一份容量，要两份的动作不能开工"
    finally:
        with _session() as session:
            for sid, asset_id in original.items():
                session.get(Station, sid).asset_id = asset_id
            session.commit()


# ---------- N03 / 孔位身份 ----------


def test_matrix_parameters_follow_the_children_to_the_second_plate(operator, clean_labware, executor, db):
    """N03：母样分装到另一块板后，下游设备步骤的逐孔参数落在子样的实体孔位上，条件随谱系继承。"""
    from app.models import Command

    batch_id = _new_batch(operator)
    plate = _register(operator, "HOTEL-01/S03", type_id="LT-PLATE-96")
    assert operator.post(f"/api/batches/{batch_id}/labware", {"labware_id": plate["id"], "role": "B"}).status_code == 200
    _reshape(db, batch_id, lambda steps: [
        _split_step("s01"),
        {"step_id": "s02", "name": "二次注液", "cap": "cap.assemble", "params": {"electrolyte": 60}, "dur": 30,
         "labware": "B"},
    ])
    _target_factor(db, batch_id, "s02", "electrolyte")
    _dispatch(operator, batch_id)
    detail, split_run = _split_ready(operator, batch_id, executor)

    wells = [f"{row}{col}" for row in "CD" for col in range(1, 9)]
    placements = _placements(detail, wells)
    confirmed = operator.post(
        f"/api/step-runs/{split_run['id']}/split", {"placements": placements, "labware_role": "B"},
    )
    assert confirmed.status_code == 200, confirmed.text
    levels = {row["id"]: row["levels"] for row in detail["samples"]}
    expected = {row["well"]: {"electrolyte": levels[row["parent_sample_id"]][1]} for row in placements}
    command = next(row for row in _commands(operator, batch_id, "dispatch") if row["step_index"] == 1)
    with _session() as session:
        params = session.get(Command, command["id"]).params
    assert params["wells"] == expected, "设备收到的是子样所在的孔位与继承来的条件，不是母样的孔位"


def test_remapped_tray_uses_physical_wells_for_parameters_and_sample_gates(
    operator, clean_labware, executor, db, measured,
):
    """孔位身份：2×4 布局放进 1×8 托盘，逐孔参数与逐样本质检都按托盘上的实体孔位。"""
    from app.models import Command, PhysicalSample, Sample

    batch_id = _new_batch(operator)
    tray = _register(operator, "HOTEL-01/S01")
    assert operator.post(f"/api/batches/{batch_id}/labware", {"labware_id": tray["id"]}).status_code == 200
    _reshape(db, batch_id, lambda steps: [
        _without_hard(steps[2]),
        {"step_id": "s05", "kind": "gate", "name": "开路电压逐样本质检",
         "gate": {"source_step_id": "s03", "field": "ocv", "min": 2.5, "max": 4.5, "scope": "sample",
                  "on_fail": "hold"}},
    ])
    _target_factor(db, batch_id, "s03", "electrolyte")
    with _session() as session:
        physical = {
            row.well for row in session.query(PhysicalSample)
            .join(Sample, Sample.physical_sample_id == PhysicalSample.id).filter(Sample.batch_id == batch_id)
        }
    assert physical == {f"A{col}" for col in range(1, 9)}, "布局按顺序落到托盘的 8 个位上"
    measured["s03"] = [{"wells": {well: {"ocv": 3.7} for well in sorted(physical)}}]

    _dispatch(operator, batch_id)
    detail = _run(operator, batch_id, executor, rounds=40)
    assert detail["state"] == "done", detail["failure_reason"]
    assert not [row for row in detail["samples"] if row["state"] == "failed"], \
        "设备按实体孔位回报读数，每个样本都要找得到自己的那一个"
    command = next(row for row in _commands(operator, batch_id, "dispatch") if row["step_index"] == 0)
    with _session() as session:
        params = session.get(Command, command["id"]).params
    assert set(params["wells"]) == physical, "下发给设备的孔位是托盘上的实体孔位"


# ---------- N04 「数据复核通过」 ----------


def test_data_gate_requires_complete_approved_valid_results_for_every_sample(
    researcher, operator, db, reset_runtime,
):
    """N04：退回、缺指标、判为无效、有样本没有检测任务，都不算数据复核通过。"""
    from app.models import AnalysisTask, Batch, ResultValue, Sample

    upstream, downstream = _task(researcher), _task(researcher)
    gate = researcher.put(
        f"/api/experiment-tasks/{downstream['id']}/dependencies",
        {"depends_on": [upstream["id"]], "gate": "data_validated"},
    )
    assert gate.status_code == 200, gate.text
    up_batch = _task_batch(operator, upstream["id"])
    with _session() as session:
        session.get(Batch, up_batch).state = "done"
        samples = session.query(Sample).filter(Sample.batch_id == up_batch).order_by(Sample.position).all()
        tasks: dict[str, str] = {}
        for sample in samples:
            task = AnalysisTask(
                org_id="ORG-001", sample_id=sample.id, physical_sample_id=sample.physical_sample_id,
                method="EC", required_metrics=[CAPACITY], state="collected",
            )
            session.add(task)
            session.flush()
            tasks[sample.id] = task.id
            session.add(ResultValue(
                org_id="ORG-001", analysis_task_id=task.id, assignment_id=sample.id,
                physical_sample_id=sample.physical_sample_id, metric_definition_id=CAPACITY, value_num=150.0,
                review_state="rejected", quality="unassessed",
            ))
        session.commit()

    def blocked() -> str:
        rows = researcher.get(f"/api/experiment-tasks/{downstream['id']}").json()["blocked_by"]
        return "；".join(row["label"] for row in rows)

    def upstream_state() -> str:
        return researcher.get(f"/api/experiment-tasks/{upstream['id']}").json()["state"]

    assert upstream_state() == "data_review", "结果全部被退回：数据阶段没有结束"
    assert "退回" in blocked()

    def revise(sample_id: str, review_state: str, quality: str) -> None:
        with _session() as session:
            current = session.query(ResultValue).filter(
                ResultValue.analysis_task_id == tasks[sample_id], ResultValue.superseded_by_id == "",
            ).order_by(ResultValue.result_version.desc()).first()
            newer = ResultValue(
                org_id="ORG-001", analysis_task_id=tasks[sample_id], assignment_id=sample_id,
                metric_definition_id=CAPACITY, value_num=151.0, result_version=current.result_version + 1,
                revises_id=current.id, review_state=review_state, quality=quality,
            )
            session.add(newer)
            session.flush()
            current.superseded_by_id = newer.id
            session.commit()

    ids = list(tasks)
    for sample_id in ids[1:]:
        revise(sample_id, "approved", "valid")
    with _session() as session:
        rejected = session.query(ResultValue).filter(ResultValue.analysis_task_id == tasks[ids[0]]).one()
        session.delete(rejected)
        session.get(AnalysisTask, tasks[ids[0]]).state = "pending"
        session.commit()
    assert upstream_state() == "data_review", "有检测任务还没采齐"
    assert "缺" in blocked()

    with _session() as session:
        session.add(ResultValue(
            org_id="ORG-001", analysis_task_id=tasks[ids[0]], assignment_id=ids[0], metric_definition_id=CAPACITY,
            value_num=12.0, review_state="approved", quality="invalid",
        ))
        session.get(AnalysisTask, tasks[ids[0]]).state = "collected"
        session.commit()
    assert upstream_state() == "reporting", "审核都已完成，进入报告阶段"
    assert "无效" in blocked(), "判为无效的数据不能作为下游开跑的依据"

    revise(ids[0], "approved", "valid")
    assert blocked() == ""

    with _session() as session:
        session.get(AnalysisTask, tasks[ids[-1]]).state = "cancelled"
        session.commit()
    assert "没有检测任务" in blocked(), "在用样本里有一个没有有效检测任务：数据不完整"


# ---------- N05 实体分装的孔位 ----------


def test_split_well_aliases_name_the_same_well(operator, clean_labware, executor, db):
    """N05：A01 与 A1 是同一个孔，不能各放一份子样本。"""
    batch_id = _new_batch(operator)
    plate = _register(operator, "HOTEL-01/S03", type_id="LT-PLATE-96")
    assert operator.post(f"/api/batches/{batch_id}/labware", {"labware_id": plate["id"]}).status_code == 200
    _reshape(db, batch_id, lambda steps: [_split_step("s01"), _long_wait("s02")])
    _dispatch(operator, batch_id)
    detail, split_run = _split_ready(operator, batch_id, executor)

    wells = [f"{row}{col}" for row in "CD" for col in range(1, 9)]
    wells[1] = "c01"
    aliased = operator.post(
        f"/api/step-runs/{split_run['id']}/split", {"placements": _placements(detail, wells), "use_labware": True},
    )
    assert aliased.status_code == 422, aliased.text


def test_split_onto_the_main_plate_respects_live_wells_and_hands_over_the_parent(
    operator, clean_labware, executor, db,
):
    """N05：同一块实体载具的同一孔位只能有一个在途样本；子样留在母样自己的孔里时交接母样的占用。"""
    from app.models import SlotOccupancy

    batch_id = _new_batch(operator)
    plate = _register(operator, "HOTEL-01/S03", type_id="LT-PLATE-96")
    assert operator.post(f"/api/batches/{batch_id}/labware", {"labware_id": plate["id"]}).status_code == 200
    _reshape(db, batch_id, lambda steps: [_split_step("s01"), _long_wait("s02")])
    _dispatch(operator, batch_id)
    detail, split_run = _split_ready(operator, batch_id, executor)

    parents = [row for row in detail["samples"] if row["state"] not in {"failed", "split"}]
    free = iter(f"{row}{col}" for row in "CD" for col in range(1, 9))
    placements = []
    for parent in parents:
        placements.append({"parent_sample_id": parent["id"], "number": 1, "well": parent["well"]})
        placements.append({"parent_sample_id": parent["id"], "number": 2, "well": next(free)})

    clash = [dict(row) for row in placements]
    clash[0]["well"] = parents[1]["well"]
    clash[2]["well"] = parents[0]["well"]
    refused = operator.post(f"/api/step-runs/{split_run['id']}/split", {"placements": clash, "use_labware": True})
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["code"] == "slot_occupied", "别的母样还在那个孔里"

    confirmed = operator.post(
        f"/api/step-runs/{split_run['id']}/split", {"placements": placements, "use_labware": True},
    )
    assert confirmed.status_code == 200, confirmed.text
    with _session() as session:
        live = session.query(SlotOccupancy).filter(
            SlotOccupancy.labware_id == plate["id"], SlotOccupancy.released_at.is_(None),
        ).all()
        positions = [row.labware_well for row in live]
        holders = {row.labware_well: row.assignment_id for row in live}
    assert len(positions) == len(set(positions)) == len(placements), "每个实体孔位只有一个在途样本"
    assert all(holders[parent["well"]] != parent["id"] for parent in parents), "母样的占用交接给留在原孔的子样"


def test_labware_role_main_is_reserved_for_the_main_plate(operator, clean_labware):
    """复现脚本里的遗漏：显式角色 main 会与主载具共用分装容器号，不能用作第二块板的角色。"""
    batch_id = _new_batch(operator)
    plate = _register(operator, "HOTEL-01/S03", type_id="LT-PLATE-96")
    refused = operator.post(f"/api/batches/{batch_id}/labware", {"labware_id": plate["id"], "role": "main"})
    assert refused.status_code == 422, refused.text


# ---------- N06 终止回执并发 ----------


def _stuck_abort(operator, *, second_stop_state: str = "done") -> str:
    """还原评审里并发终止的结局：两台设备各自确认停止，批次却停在「终止中」。"""
    from app.core.clock import now
    from app.models import Batch, Command

    batch_id = _new_batch(operator)
    with _session() as db:
        batch = db.get(Batch, batch_id)
        batch.state = "aborting"
        for index, (station_id, capability) in enumerate([("ST-05", "cap.vacuum_dry"), ("ST-06", "cap.assemble")]):
            action = Command(
                org_id=batch.org_id, batch_id=batch_id, station_id=station_id, capability=capability,
                type="dispatch", state="cancelled", delivery_state="delivered", params={}, step_index=index,
                started_at=now(),
            )
            db.add(action)
            db.flush()
            db.add(Command(
                org_id=batch.org_id, batch_id=batch_id, station_id=station_id, capability=capability,
                type="abort", state="done" if index == 0 else second_stop_state, delivery_state="delivered",
                params={}, step_index=index, started_at=now(), target_command_id=action.id,
            ))
        db.commit()
    return batch_id


def test_concurrent_abort_receipts_finish_the_batch(operator, reset_runtime):
    """N06：两台设备的终止回执在两个会话里同时处理（真实 PostgreSQL），批次也要收尾为已终止。"""
    from app.adapters.base import CommandResult
    from app.core.clock import now
    from app.core.context import system_context
    from app.core.db import SessionLocal
    from app.models import Adapter, AdapterExecution, AuditEvent, Batch, Command
    from app.services.execution_service import ExecutionService

    batch_id = _new_batch(operator)
    by_station: dict[str, dict] = {}
    with SessionLocal() as db:
        assert db.get_bind().dialect.name == "postgresql"
        batch = db.get(Batch, batch_id)
        batch.state = "aborting"
        org_id = batch.org_id
        for index, (station_id, capability) in enumerate([("ST-05", "cap.vacuum_dry"), ("ST-06", "cap.assemble")]):
            action_id, stop_id = str(uuid.uuid4()), str(uuid.uuid4())
            db.add(Command(
                id=action_id, org_id=org_id, batch_id=batch_id, station_id=station_id, capability=capability,
                type="dispatch", state="running", delivery_state="delivered", params={}, step_index=index,
                started_at=now(),
            ))
            db.add(Command(
                id=stop_id, org_id=org_id, batch_id=batch_id, station_id=station_id, capability=capability,
                type="abort", state="running", delivery_state="delivered", params={}, step_index=index,
                started_at=now(), target_command_id=action_id,
            ))
            db.get(Adapter, station_id).current_command_id = action_id
            by_station[station_id] = {"action": action_id, "stop": stop_id}
        db.flush()
        for station_id, ids in by_station.items():
            for command_id in ids.values():
                db.add(AdapterExecution(command_id=command_id, station_id=station_id, state="running"))
        db.commit()

    # 两个会话都做完自己这台设备的更新、走到汇总之前才放行：评审复现的就是这个交错
    before_summary = Barrier(2)

    def receive_stop(station_id: str) -> None:
        with SessionLocal() as db:
            ids = by_station[station_id]
            service = ExecutionService(db, system_context(org_id, "并发终止回执"))
            real_lock = service.batches.lock

            def synchronized_lock(identifier):
                before_summary.wait(timeout=15)
                return real_lock(identifier)

            service.batches.lock = synchronized_lock
            try:
                service.settle(
                    db.get(Batch, batch_id), db.get(Command, ids["stop"]), db.get(AdapterExecution, ids["stop"]),
                    db.get(Adapter, station_id),
                    CommandResult(command_id=ids["stop"], state="done", device_ts=now(), quality="good",
                                  origin="real:concurrent-stop"),
                )
                db.commit()
            except BaseException:
                before_summary.abort()
                db.rollback()
                raise

    with ThreadPoolExecutor(max_workers=2) as workers:
        for future in [workers.submit(receive_stop, station_id) for station_id in by_station]:
            future.result(timeout=40)

    with SessionLocal() as db:
        assert db.get(Batch, batch_id).state == "aborted", "两台设备都确认停止，批次必须收尾"
        finished = db.query(AuditEvent).filter(
            AuditEvent.target == batch_id, AuditEvent.action == "设备确认终止", AuditEvent.after == "已终止",
        ).count()
        assert finished == 1, "只收尾一次"


def test_executor_finishes_an_abort_left_hanging(operator, db, executor, reset_runtime):
    """N06：终止目标都已有结论、批次却停在终止中（并发、进程在收尾前退出）：执行器兜底收尾。"""
    batch_id = _stuck_abort(operator)
    executor()
    detail = _detail(operator, batch_id)
    assert detail["state"] == "aborted"
    assert any(event["action"] == "终止汇总收尾" for event in detail["audit"])


def test_operator_can_reconcile_an_abort_by_hand(operator, db, reset_runtime):
    """N06：界面上的「重新汇总终止」：还有设备没确认就说明在等谁；都确认了就收尾。"""
    from app.models import Command

    batch_id = _stuck_abort(operator, second_stop_state="running")
    waiting = operator.post(f"/api/batches/{batch_id}/abort/reconcile", {})
    assert waiting.status_code == 200, waiting.text
    assert waiting.json()["state"] == "aborting" and waiting.json()["waiting_stations"] == ["ST-06"]

    with _session() as session:
        stop = session.query(Command).filter(
            Command.batch_id == batch_id, Command.type == "abort", Command.state == "running",
        ).one()
        stop.state = "done"
        session.commit()
    finished = operator.post(f"/api/batches/{batch_id}/abort/reconcile", {})
    assert finished.status_code == 200, finished.text
    assert finished.json()["state"] == "aborted" and finished.json()["waiting_stations"] == []


# ---------- N07 按实际进度对齐 ----------


def test_realign_leaves_a_running_parallel_branch_alone(operator, devices, db, executor):
    """N07：序号小的分支迟到开出，序号大的独立分支已在运行：后者的时间窗不能跟着平移。"""
    from app.core.clock import now

    board = devices["use"]("ST-05", "ST-06")
    batch_id = _new_batch(operator)
    _reshape(db, batch_id, _parallel_roots)
    _dispatch(operator, batch_id)
    executor()
    acting = {row["station_id"]: row["id"] for row in _commands(operator, batch_id, "dispatch")}
    assert sorted(acting) == ["ST-05", "ST-06"]

    _shift_plan(batch_id, 120)
    assemble_before = _work(batch_id, 2)
    board["finished"].add(acting["ST-05"])
    executor()
    assert any(row["step_index"] == 1 for row in _commands(operator, batch_id, "dispatch")), "称重开出（已迟到）"
    assert _work(batch_id, 2) == assemble_before, "已在运行的组装不跟着称重平移"
    weigh = _work(batch_id, 1)
    assert abs((weigh[1] - now()).total_seconds()) < 180, "迟到的称重对齐到实际开工时刻"


def test_realign_leaves_an_unrelated_waiting_branch_alone(operator, devices, db, executor):
    """N07 的扩展：只等静置、与称重无关的组装还没开出，也不能被称重的迟到推后。"""
    board = devices["use"]("ST-05", "ST-06")

    def shape(steps):
        dry, weigh, assemble, test = steps
        return [
            {**dry, "after": []},
            {**weigh, "after": [dry["step_id"]]},
            {"step_id": "w9", "name": "静置", "kind": "wait", "dur": 240, "after": [], "wait_for": {"mode": "duration"}},
            {**_without_hard(assemble), "after": ["w9"]},
            {**test, "after": [weigh["step_id"], assemble["step_id"]]},
        ]

    batch_id = _new_batch(operator)
    _reshape(db, batch_id, shape)
    _dispatch(operator, batch_id)
    executor()
    dry = _commands(operator, batch_id, "dispatch")[0]
    _shift_plan(batch_id, 120)
    assemble_before, test_before = _work(batch_id, 3), _work(batch_id, 4)
    board["finished"].add(dry["id"])
    executor()
    assert any(row["step_index"] == 1 for row in _commands(operator, batch_id, "dispatch"))
    assert _work(batch_id, 3) == assemble_before, "组装不依赖称重，时间窗不动"
    assert _work(batch_id, 4)[1] > test_before[1], "测试等称重，跟着顺延"


# ---------- N08 尾段重排的前驱 ----------


def test_tail_reschedule_waits_for_the_real_predecessor(operator, devices, db, executor):
    """N08：测试只等干燥（60 min）；列表里的上一项组装早就做完了，不能拿它当起点。"""
    from app.core.clock import now

    board = devices["use"]("ST-05", "ST-06")

    def shape(steps):
        dry, weigh, assemble, test = steps
        return [
            {**dry, "after": []},
            {**_without_hard(assemble), "after": []},
            {**test, "after": [dry["step_id"]]},
            {**_without_hard(weigh), "after": [test["step_id"]]},
        ]

    batch_id = _new_batch(operator)
    _reshape(db, batch_id, shape)
    _dispatch(operator, batch_id)
    executor()
    acting = {row["station_id"]: row["id"] for row in _commands(operator, batch_id, "dispatch")}
    board["finished"].add(acting["ST-06"])
    executor()
    assert _command(db, acting["ST-06"]).state == "done" and _command(db, acting["ST-05"]).state == "running"

    moved = operator.post(f"/api/batches/{batch_id}/reschedule", {"from_step": 2, "start_from": now().isoformat()})
    assert moved.status_code == 200, moved.text
    assert _work(batch_id, 2)[1] >= _work(batch_id, 0)[2], "测试不能排在干燥结束之前"


# ---------- 未走的分支不算已开出 ----------


def test_untaken_branch_listed_later_does_not_freeze_the_chosen_path(operator, db, executor, reset_runtime):
    """复核补充：没走的分支排在已选路径后面时，它的「未走此路径」记录不能挡住滚动重排与手动重排。"""
    from app.core.clock import now

    def shape(steps):
        dry, weigh, assemble, test = steps
        return [
            dry,
            {"step_id": "b1", "name": "是否直接组装", "kind": "branch", "after": [dry["step_id"]],
             "branch": {"mode": "manual", "cases": [{"key": "assemble", "label": "直接组装"},
                                                   {"key": "weigh", "label": "先称重"}]}},
            {**_without_hard(assemble), "after": ["b1"], "when": {"b1": "assemble"}},
            {**test, "after": [assemble["step_id"]]},
            {**_without_hard(weigh), "after": ["b1"], "when": {"b1": "weigh"}},
        ]

    batch_id = _new_batch(operator)
    _reshape(db, batch_id, shape)
    _dispatch(operator, batch_id)
    detail = _run(operator, batch_id, executor, rounds=8, until=("never",))
    choice = next(row for row in detail["step_runs"] if row["step_id"] == "b1")
    assert choice["state"] == "ready"
    chosen = operator.post(f"/api/step-runs/{choice['id']}/branch-decision", {
        "case": "assemble", "reason": "极片已称过", "row_version": choice["row_version"],
    })
    assert chosen.status_code == 200, chosen.text
    after = _detail(operator, batch_id)
    assert {row["step_index"]: row["state"] for row in after["step_runs"]}.get(4) == "not_taken"
    assert any(event["action"] == "滚动重排" for event in after["audit"]), "已选路径上还没开出的测试要滚动重算"

    moved = operator.post(f"/api/batches/{batch_id}/reschedule", {"from_step": 3, "start_from": now().isoformat()})
    assert moved.status_code == 200, moved.text


# ---------- N09 / N10 任务版本 ----------


def test_decompose_takes_samples_from_the_pinned_version(researcher, qa, operator, reset_runtime):
    """N09：任务锁定 v1（OLD-A/OLD-B）；方案修订中的草稿改成 NEW-C/NEW-D，拆分仍按 v1 的样本。"""
    from app.models import PhysicalSample

    tag = uuid.uuid4().hex[:6].upper()
    old, new = [f"PS-{tag}-OLD-A", f"PS-{tag}-OLD-B"], [f"PS-{tag}-NEW-C", f"PS-{tag}-NEW-D"]
    with _session() as session:
        for sample_id in old + new:
            session.add(PhysicalSample(id=sample_id, org_id="ORG-001", barcode=sample_id))
        session.commit()
    created = researcher.post("/api/plans", {
        "name": f"拆分版本 {tag}", "recipe_id": "R-205", "plan_type": "single_condition",
        "sample_ids": old, "required_metrics": [CAPACITY],
    })
    assert created.status_code == 201, created.text
    plan_id = created.json()["id"]
    _approve(researcher, qa, plan_id)
    task = _plan_task(researcher, plan_id)

    assert researcher.post(f"/api/plans/{plan_id}/revisions").status_code == 201
    plan = researcher.get(f"/api/plans/{plan_id}").json()
    edited = researcher.patch(f"/api/plans/{plan_id}", {"sample_ids": new, "row_version": plan["row_version"]})
    assert edited.status_code == 200, edited.text

    split = researcher.post(f"/api/experiment-tasks/{task['id']}/decompose", {"parts": 2})
    assert split.status_code == 200, split.text
    children = split.json()["children"]
    samples = [researcher.get(f"/api/experiment-tasks/{child['id']}").json()["sample_ids"] for child in children]
    assert samples == [[old[0]], [old[1]]], "子任务沿用锁定版本的样本，不读修订中的草稿"
    batch = operator.post("/api/batches", {"plan_id": plan_id, "task_id": children[0]["id"]})
    assert batch.status_code == 201, batch.text
    rows = operator.get(f"/api/batches/{batch.json()['id']}").json()["samples"]
    assert [row["physical_sample_id"] for row in rows] == [old[0]]


def test_parent_migration_only_moves_descendants_of_the_same_plan(researcher, qa, operator, reset_runtime):
    """N10：方案 B 的任务挂在方案 A 的父任务下：父任务升级到 A v2，子任务保持 B 的版本并照常建批次。"""
    plan_a, plan_b = _approved_plan(researcher, qa), _approved_plan(researcher, qa)
    parent = _plan_task(researcher, plan_a)
    created = researcher.post("/api/experiment-tasks", {"plan_id": plan_b, "parent_id": parent["id"]})
    assert created.status_code == 201, created.text
    child = created.json()

    assert researcher.post(f"/api/plans/{plan_a}/revisions").status_code == 201
    plan = researcher.get(f"/api/plans/{plan_a}").json()
    assert researcher.patch(f"/api/plans/{plan_a}", {"sample_count": 6, "row_version": plan["row_version"]}).status_code == 200
    _approve(researcher, qa, plan_a)

    moved = researcher.post(f"/api/experiment-tasks/{parent['id']}/migrate-version", {"reason": "父任务升级到 v2"})
    assert moved.status_code == 200, moved.text
    assert [row["task_id"] for row in moved.json()["migrated"]] == [parent["id"]]
    assert [row["task_id"] for row in moved.json()["skipped"]] == [child["id"]], "别的方案的后代不迁移，列出来"
    after = researcher.get(f"/api/experiment-tasks/{child['id']}").json()
    assert (after["plan_id"], after["plan_version"]) == (plan_b, 1)
    batch = operator.post("/api/batches", {"plan_id": plan_b, "task_id": child["id"]})
    assert batch.status_code == 201, batch.text


# ---------- N11 协同工位的清洗 ----------


def test_dirty_assist_station_waits_for_cleaning_and_gets_soiled(operator, devices, db, executor):
    """N11：协同工位被上一批弄脏时，主设备干净也要等清洗确认；做过需要清洗的协同动作后协同工位转为待清洗。"""
    from app.models import Alarm, Capability, Station

    board = devices["use"]("ST-05", "AGV-01", "AGV-02")
    with _session() as session:
        capability = session.get(Capability, "cap.transfer")
        original = dict(capability.recovery or {})
        capability.recovery = {**original, "cleanAfter": True}
        session.commit()
    helper = ""
    try:
        batch_id = _new_batch(operator)
        _reshape(db, batch_id, _with_robot)
        _dispatch(operator, batch_id)
        dry = _commands(operator, batch_id, "dispatch")[0]
        helper = dry["assist_station_ids"][0]
        with _session() as session:
            station = session.get(Station, helper)
            station.clean, station.dirty_batch_id = False, "B-PREVIOUS"
            session.commit()
        executor()
        assert _command(db, dry["id"]).state == "sent", "协同工位还没清洗确认，整条动作排队等"
        db.expire_all()
        assert db.query(Alarm).filter(
            Alarm.condition_key == f"station:{helper}:awaiting_clean", Alarm.condition_active.is_(True),
        ).count() == 1

        version = operator.get("/api/stations").json()
        row_version = next(row for row in version if row["id"] == helper)["row_version"]
        confirmed = operator.patch(
            f"/api/stations/{helper}/readiness", {"clean": True, "status": "idle", "row_version": row_version},
        )
        assert confirmed.status_code == 200, confirmed.text
        executor()
        assert _command(db, dry["id"]).state == "running"

        board["finished"].add(dry["id"])
        executor()
        db.expire_all()
        station = db.get(Station, helper)
        assert (station.clean, station.dirty_batch_id) == (False, batch_id), "做过需要清洗的协同动作，协同工位转为待清洗"
    finally:
        with _session() as session:
            session.get(Capability, "cap.transfer").recovery = original
            if helper:
                station = session.get(Station, helper)
                station.clean, station.dirty_batch_id = True, ""
            session.commit()


# ---------- N12 协同资源选型 ----------


def test_assist_selection_checks_the_shared_asset_jointly():
    """N12：主设备与 HELP-A 共用容量 1 的资产，HELP-B 独立：要选 HELP-B，而不是让资产超容量。"""
    from app.domain.capability import StationSpec
    from app.domain.scheduling import Interval, SchedulingContext, peak_load, plan_steps

    start = datetime(2026, 9, 27, 8)
    context = SchedulingContext(
        stations=[
            StationSpec(id="MAIN", limits={"cap.main": {}}),
            StationSpec(id="HELP-A", limits={"cap.help": {}}),
            StationSpec(id="HELP-B", limits={"cap.help": {}}),
        ],
        station_asset={"MAIN": "ASSET", "HELP-A": "ASSET"}, asset_capacity={"ASSET": 1}, clean_min=0,
    )
    planned = plan_steps([{"name": "带协同", "cap": "cap.main", "dur": 10, "assist": ["cap.help"]}], start, context)
    assert {(row.station_id, row.kind) for row in planned} == {("MAIN", "work"), ("HELP-B", "assist")}
    on_asset = [(Interval(row.starts_at, row.ends_at), 1) for row in planned if row.station_id in {"MAIN", "HELP-A"}]
    assert peak_load(on_asset, Interval(start, start + timedelta(minutes=10))) <= 1


def test_dry_run_applies_the_same_asset_check_as_the_final_write(operator, db, reset_runtime, monkeypatch):
    """N12：预览与写入用同一道资产容量检查，不能出现「预览可行、应用失败」。"""
    import app.services.schedule_service as module
    from app.core.clock import now
    from app.core.context import system_context
    from app.domain.scheduling import PlannedAllocation
    from app.models import Asset, Batch, Station

    batch_id = _new_batch(operator)
    with _session() as session:
        asset = Asset(
            org_id="ORG-001", asset_no=f"AS-T{uuid.uuid4().hex[:6]}", name="共享资产（测试）", capacity=1,
            calibration_applicable=False, calibration_exempt_reason="测试资产",
        )
        session.add(asset)
        session.flush()
        original = {sid: session.get(Station, sid).asset_id for sid in ("ST-05", "ST-06")}
        for sid in original:
            session.get(Station, sid).asset_id = asset.id
        session.commit()
    begin = now() + timedelta(hours=1)
    overloaded = [
        PlannedAllocation(0, "ST-05", begin, begin + timedelta(minutes=30), "work"),
        PlannedAllocation(1, "ST-06", begin, begin + timedelta(minutes=30), "work"),
    ]
    monkeypatch.setattr(module, "plan_steps", lambda *args, **kwargs: list(overloaded))
    try:
        preview = module.ScheduleService(db, system_context("ORG-001")).dry_run(db.get(Batch, batch_id))
        assert preview["ok"] is False and "容量" in preview["reason"]
    finally:
        with _session() as session:
            for sid, asset_id in original.items():
                session.get(Station, sid).asset_id = asset_id
            session.commit()
