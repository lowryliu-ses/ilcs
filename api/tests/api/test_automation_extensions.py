"""扩展自动化场景与遗留项（核心链路评审第三批）。

- 任务显式迁移到新的批准版本；依赖的放行条件（运行结束 / 数据复核通过 / 报告发布放行）
- 硬时限倒计时的接口与交班摘要
- 放置位原子预占：两个批次并发争同一个空放置位，只有一个拿到
- 实体分装：按实际孔位确认后才推进，可以落到按角色绑定的另一块板上
- 协同资源：主设备与协同工位同一时段一起预约、一起取得
- 多载具并行：用不同板的设备步骤可以同时开出，没绑定的角色不能拿主载具顶替
- 滚动排程：分支没定的下游标为预测；定了路径后按实际路径重算尚未开出的尾段
"""
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from sqlalchemy import text

from test_core_chain_review import (  # noqa: F401  （复用工具与 fixture）
    _approve, _approved_plan, _command, _commands, _detail, _dispatch, _foreign_command, _new_batch, _plan_task,
    _reshape, _session, _task, _task_batch, _wait_beside_dry, _without_hard, scripted,
)
from test_failure_paths import running_batch  # noqa: F401
from test_labware_transfer import clean_labware  # noqa: F401


def _register(operator, location_id: str | None, type_id: str = "LT-TRAY-8") -> dict:
    created = operator.post(
        "/api/labware", {"barcode": f"LW-{uuid.uuid4().hex[:8]}", "type_id": type_id, "location_id": location_id},
    )
    assert created.status_code == 201, created.text
    return created.json()


def _run(operator, batch_id: str, executor, rounds: int = 25, until=("done", "fault", "aborted")) -> dict:
    for _ in range(rounds):
        executor()
        detail = _detail(operator, batch_id)
        if detail["state"] in until:
            return detail
        time.sleep(0.05)
    return _detail(operator, batch_id)


# ---------- 任务版本迁移 ----------


def test_task_migrates_to_the_new_approved_version_explicitly(researcher, qa, operator, reset_runtime):
    plan_id = _approved_plan(researcher, qa, sample_count=4)
    single = _plan_task(researcher, plan_id)
    parent = _plan_task(researcher, plan_id)
    split = researcher.post(f"/api/experiment-tasks/{parent['id']}/decompose", {"parts": 2})
    assert split.status_code == 200, split.text
    first_child, second_child = split.json()["children"]
    started = operator.post("/api/batches", {"plan_id": plan_id, "task_id": first_child["id"]})
    assert started.status_code == 201, started.text

    assert researcher.post(f"/api/plans/{plan_id}/revisions").status_code == 201
    plan = researcher.get(f"/api/plans/{plan_id}").json()
    assert researcher.patch(f"/api/plans/{plan_id}", {"sample_count": 6, "row_version": plan["row_version"]}).status_code == 200
    _approve(researcher, qa, plan_id)
    assert researcher.get(f"/api/experiment-tasks/{single['id']}").json()["latest_plan_version"] == 2

    silent = researcher.post(f"/api/experiment-tasks/{single['id']}/migrate-version", {"reason": ""})
    assert silent.status_code == 422, "迁移必须写原因"
    moved = researcher.post(f"/api/experiment-tasks/{single['id']}/migrate-version", {"reason": "按 v2 的样本数执行"})
    assert moved.status_code == 200, moved.text
    assert moved.json()["plan_version"] == 2
    batch = operator.post("/api/batches", {"plan_id": plan_id, "task_id": single["id"]})
    assert batch.status_code == 201, batch.text
    assert batch.json()["plan_version"] == 2 and batch.json()["sample_count"] == 6

    again = researcher.post(f"/api/experiment-tasks/{single['id']}/migrate-version", {"reason": "再迁一次"})
    assert again.status_code == 409 and again.json()["detail"]["code"] == "task_already_has_batch"

    family = researcher.post(f"/api/experiment-tasks/{parent['id']}/migrate-version", {"reason": "整单改用 v2"})
    assert family.status_code == 200, family.text
    assert [row["task_id"] for row in family.json()["migrated"]] == [parent["id"], second_child["id"]]
    assert [row["task_id"] for row in family.json()["kept"]] == [first_child["id"]], "已建批次的子任务保持原版本"
    current = researcher.post(f"/api/experiment-tasks/{parent['id']}/migrate-version", {"reason": "重复"})
    assert current.status_code == 409 and current.json()["detail"]["code"] == "task_version_current"


# ---------- 依赖的放行条件 ----------


def test_dependency_gate_distinguishes_run_data_and_release(researcher, operator, reset_runtime):
    from app.models import Batch

    upstream, downstream = _task(researcher), _task(researcher)
    set_gate = researcher.put(
        f"/api/experiment-tasks/{downstream['id']}/dependencies",
        {"depends_on": [upstream["id"]], "gate": "released"},
    )
    assert set_gate.status_code == 200, set_gate.text
    assert set_gate.json()["dependency_gate"] == "released"
    up_batch = _task_batch(operator, upstream["id"])
    with _session() as db:
        db.get(Batch, up_batch).state = "done"
        db.commit()
    assert researcher.get(f"/api/experiment-tasks/{upstream['id']}").json()["state"] == "reporting"

    blocked = researcher.get(f"/api/experiment-tasks/{downstream['id']}").json()["blocked_by"]
    assert blocked and "报告发布放行" in blocked[0]["label"], "运行结束、数据也不用复核，但报告没发布：仍然挡住"

    relaxed = researcher.put(
        f"/api/experiment-tasks/{downstream['id']}/dependencies",
        {"depends_on": [upstream["id"]], "gate": "data_validated"},
    )
    assert relaxed.status_code == 200 and relaxed.json()["blocked_by"] == [], "数据复核通过即可"

    parent = _task(researcher)
    researcher.put(f"/api/experiment-tasks/{parent['id']}/dependencies", {"depends_on": [upstream["id"]], "gate": "released"})
    child = researcher.post(f"/api/experiment-tasks/{parent['id']}/decompose", {"parts": 2}).json()["children"][0]
    inherited = researcher.get(f"/api/experiment-tasks/{child['id']}").json()
    assert inherited["inherited_depends_on"] == [upstream["id"]]
    assert "报告发布放行" in inherited["blocked_by"][0]["label"], "继承来的依赖沿用父任务的放行条件"


# ---------- 硬时限倒计时 ----------


def test_due_windows_are_served_and_included_in_handover(operator, db, reset_runtime):
    from app.core.clock import now
    from app.models import Batch, Checkpoint, Command, StepRun

    batch_id = _new_batch(operator)
    _reshape(db, batch_id, _wait_beside_dry(240))
    batch = db.get(Batch, batch_id)
    steps = batch.recipe_snapshot["steps"]
    batch.state = "running"
    finished = now() - timedelta(minutes=5)
    db.add(StepRun(org_id=batch.org_id, batch_id=batch_id, step_id=steps[1]["step_id"], step_index=1,
                   kind="device", attempt=1, state="completed", step_snapshot=steps[1], started_at=finished,
                   ended_at=finished))
    dry = Command(org_id=batch.org_id, batch_id=batch_id, station_id="ST-05", capability="cap.vacuum_dry",
                  params={}, type="dispatch", state="done", delivery_state="delivered", step_index=1)
    db.add(dry)
    db.flush()
    db.add(Checkpoint(batch_id=batch_id, command_id=dry.id, step_index=1, state="done", payload={}, created_at=finished))
    db.commit()

    rows = [row for row in operator.get("/api/schedule/due-windows").json() if row["batch_id"] == batch_id]
    assert [row["step_index"] for row in rows] == [2]
    handover = operator.get("/api/handover").json()
    assert any(row["batch_id"] == batch_id for row in handover["due_windows"])


# ---------- 放置位原子预占 ----------


def test_two_batches_racing_for_one_free_nest_get_one_winner(operator, clean_labware, db):  # noqa: F811
    """两个批次同时要把板送到 ST-05 唯一的放置位：锁把两次「查空位 → 写转运」串起来，只有一个拿到。"""
    from app.core.context import system_context
    from app.core.db import ADVISORY_NAMESPACE, SessionLocal, engine
    from app.models import Batch
    from app.services.transfer_service import TransferService

    batches = []
    for slot in ("HOTEL-01/S01", "HOTEL-01/S02"):
        batch_id = _new_batch(operator)
        labware = _register(operator, slot)
        assert operator.post(f"/api/batches/{batch_id}/labware", {"labware_id": labware["id"]}).status_code == 200
        batches.append(batch_id)

    def prepare(batch_id: str):
        with SessionLocal() as session:
            service = TransferService(session, system_context("ORG-001"))
            command, blocker = service.prepare(session.get(Batch, batch_id), 0, "ST-05", "")
            session.commit()
            return (command.params["to"]["location_id"] if command else None), blocker

    with engine.connect() as blocker:
        blocker.execute(
            text("SELECT pg_advisory_lock(:ns, hashtext('labware:locations'))"), {"ns": ADVISORY_NAMESPACE},
        )
        pool = ThreadPoolExecutor(max_workers=2)
        try:
            futures = [pool.submit(prepare, batch_id) for batch_id in batches]
            time.sleep(0.3)
            assert not any(future.done() for future in futures), "放置位分配没有等锁"
            blocker.execute(
                text("SELECT pg_advisory_unlock(:ns, hashtext('labware:locations'))"), {"ns": ADVISORY_NAMESPACE},
            )
            blocker.commit()
            results = [future.result(timeout=20) for future in futures]
        finally:
            pool.shutdown(wait=True)
    destinations = [destination for destination, _ in results]
    assert sorted(destinations, key=lambda value: value or "") == [None, "ST-05/N1"], results
    loser = next(reason for destination, reason in results if destination is None)
    assert "放置位都被占用" in loser


# ---------- 实体分装 ----------


def _append_split(db, batch_id: str, **split) -> None:
    from sqlalchemy.orm.attributes import flag_modified

    from app.models import Batch

    batch = db.get(Batch, batch_id)
    snapshot = dict(batch.recipe_snapshot)
    snapshot["steps"] = [*snapshot["steps"], {
        "step_id": "s05", "kind": "split", "name": "分装扣电",
        "split": {"count": 2, "child_type": "扣电", "mode": "physical", **split},
    }]
    batch.recipe_snapshot = snapshot
    flag_modified(batch, "recipe_snapshot")
    db.commit()


def _placements(detail: dict, wells=None) -> list[dict]:
    parents = [row for row in detail["samples"] if row["state"] not in {"failed", "split"}]
    rows = []
    for position, (sample, number) in enumerate((sample, number) for sample in parents for number in (1, 2)):
        well = wells[position] if wells else f"{sample['well']}-{number}"
        rows.append({"parent_sample_id": sample["id"], "number": number, "well": well})
    return rows


def test_physical_split_waits_for_confirmed_placements(operator, running_batch, executor, db):
    from app.models import PhysicalSample, Sample

    _append_split(db, running_batch)
    detail = _run(operator, running_batch, executor, until=("done", "fault"))
    split_run = next(row for row in detail["step_runs"] if row["kind"] == "split")
    assert detail["state"] == "running" and split_run["state"] == "ready", "实体分装要等孔位确认，不自己完成"

    partial = operator.post(f"/api/step-runs/{split_run['id']}/split", {"placements": _placements(detail)[:-1]})
    assert partial.status_code == 422 and partial.json()["detail"]["code"] == "split_placements_incomplete"
    duplicate = _placements(detail)
    duplicate[1]["well"] = duplicate[0]["well"]
    assert operator.post(f"/api/step-runs/{split_run['id']}/split", {"placements": duplicate}).status_code == 422

    placements = _placements(detail)
    confirmed = operator.post(f"/api/step-runs/{split_run['id']}/split", {"placements": placements, "note": "分装机回报"})
    assert confirmed.status_code == 200, confirmed.text
    assert _detail(operator, running_batch)["state"] == "done"
    db.expire_all()
    child = db.query(Sample).filter(Sample.batch_id == running_batch, Sample.well == placements[0]["well"]).one()
    assert db.get(PhysicalSample, child.physical_sample_id).parent_id is not None, "子样本谱系指向母样"


def test_physical_split_lands_on_a_second_plate(operator, clean_labware, executor, db):  # noqa: F811
    from app.domain.labware import container_of
    from app.models import PhysicalSample, SlotOccupancy

    batch_id = _new_batch(operator)
    plate = _register(operator, "HOTEL-01/S03", type_id="LT-PLATE-96")
    bound = operator.post(f"/api/batches/{batch_id}/labware", {"labware_id": plate["id"], "role": "B"})
    assert bound.status_code == 200 and bound.json()["role"] == "B"
    _append_split(db, batch_id)
    _dispatch(operator, batch_id)
    detail = _run(operator, batch_id, executor, until=("done", "fault"))
    split_run = next(row for row in detail["step_runs"] if row["kind"] == "split")
    assert split_run["state"] == "ready", detail["failure_reason"]
    assert [row["role"] for row in detail["labware_all"]] == ["B"]

    wells = [f"{chr(65 + row)}{col}" for row in range(8) for col in range(1, 13)]
    outside = operator.post(f"/api/step-runs/{split_run['id']}/split", {
        "placements": _placements(detail, ["Z9", *wells[1:]]), "labware_role": "B",
    })
    assert outside.status_code == 422, "孔位要在这块板上"
    confirmed = operator.post(f"/api/step-runs/{split_run['id']}/split", {
        "placements": _placements(detail, wells), "labware_role": "B",
    })
    assert confirmed.status_code == 200, confirmed.text
    db.expire_all()
    slots = db.query(SlotOccupancy).filter(SlotOccupancy.container_id == f"{container_of(batch_id)}:B").all()
    assert len(slots) == len(_placements(detail)) and {row.labware_id for row in slots} == {plate["id"]}, \
        "每一份子样本占用第二块板上的一个孔位"
    child = db.get(PhysicalSample, slots[0].physical_sample_id)
    assert child.current_location.startswith(plate["barcode"]), "子样本的位置指向第二块板（批次结束后留作文本位置）"


# ---------- 协同资源 ----------


def _with_robot(steps):
    dry, weigh, assemble, test = steps
    return [{**dry, "assist": ["cap.transfer"]}, weigh, assemble, test]


def test_assist_resource_is_booked_and_acquired_with_the_device(operator, scripted, db, executor):
    from app.models import Command

    scripted["use"]("ST-05", "AGV-01", "AGV-02")
    # AGV-01 上已经有别的批次的动作在跑（排程时间线上没有它的时间窗，只有执行器看得见）
    _, other = _foreign_command(operator, "AGV-01", "cap.transfer")
    executor()
    assert _command(db, other).state == "running"

    batch_id = _new_batch(operator)
    _reshape(db, batch_id, _with_robot)
    _dispatch(operator, batch_id)
    allocations = _detail(operator, batch_id)["allocations"]
    work = next(row for row in allocations if row["step_index"] == 0 and row["kind"] == "work")
    assist = next(row for row in allocations if row["step_index"] == 0 and row["kind"] == "assist")
    assert (assist["starts_at"], assist["ends_at"]) == (work["starts_at"], work["ends_at"]), "协同资源与主设备同起同止"
    helper = assist["station_id"]
    assert helper == "AGV-01"

    executor()
    dry = _commands(operator, batch_id, "dispatch")[0]
    assert dry["assist_station_ids"] == [helper]
    assert dry["state"] == "sent", "协同资源被占着，主设备的动作也不投递：不会只占到一半就开工"

    with _session() as session:
        session.get(Command, other).state = "cancelled"
        session.commit()
    executor()
    started = _command(db, dry["id"])
    assert started.state == "running" and started.assist_station_ids == [helper]


def test_assist_capability_must_be_registered():
    from app.domain.recipe_rules import validate_steps

    steps = [{"step_id": "s01", "name": "干燥", "cap": "cap.vacuum_dry", "params": {"temp": 120, "vacuum": 1},
              "dur": 10, "assist": ["cap.nope"]}]
    capabilities = {"cap.vacuum_dry": {"name": "干燥", "params": {"temp": "箱温", "vacuum": "真空度"}}}
    issues = validate_steps(steps, [], capabilities)[0]["issues"]
    assert any("协同资源的能力 cap.nope 未登记" in issue for issue in issues)


# ---------- 多载具并行 ----------


def _two_plates(steps):
    dry, weigh, assemble, test = steps
    return [
        {**dry, "after": []},
        {**weigh, "after": [dry["step_id"]]},
        {**_without_hard(assemble), "after": [], "labware": "B"},
        {**test, "after": [weigh["step_id"], assemble["step_id"]]},
    ]


def test_steps_on_different_plates_run_in_parallel(operator, clean_labware, db, executor):  # noqa: F811
    batch_id = _new_batch(operator)
    _reshape(db, batch_id, _two_plates)
    main = _register(operator, "HOTEL-01/S01")
    second = _register(operator, "HOTEL-01/S02")
    assert operator.post(f"/api/batches/{batch_id}/labware", {"labware_id": main["id"]}).status_code == 200
    assert operator.post(f"/api/batches/{batch_id}/labware", {"labware_id": second["id"], "role": "B"}).status_code == 200
    _dispatch(operator, batch_id)

    detail = _detail(operator, batch_id)
    roots = {row["step_index"]: row["state"] for row in detail["step_runs"]}
    assert roots == {0: "ready", 2: "ready"}, "两块板各自的起点设备步骤同时开出，谁也不等谁的板"
    transfers = {row["labware_id"] for row in detail["commands"] if row["type"] == "transfer"}
    assert transfers == {main["id"], second["id"]}, "两块板各有自己的转运"
    work = {row["step_index"]: row for row in detail["allocations"] if row["kind"] == "work"}
    assert work[2]["starts_at"] < work[0]["ends_at"], "排程也允许不同板上的步骤并行"

    done = _run(operator, batch_id, executor, rounds=40)
    assert done["state"] == "done", done["failure_reason"]


def test_step_cannot_borrow_the_main_plate_for_an_unbound_role(operator, clean_labware, db, executor):  # noqa: F811
    batch_id = _new_batch(operator)
    _reshape(db, batch_id, lambda steps: [{**steps[0], "labware": "C"}, *steps[1:]])
    main = _register(operator, "HOTEL-01/S01")
    assert operator.post(f"/api/batches/{batch_id}/labware", {"labware_id": main["id"]}).status_code == 200
    assert operator.post(f"/api/batches/{batch_id}/schedule", {}).status_code == 200
    refused = operator.post(
        f"/api/batches/{batch_id}/dispatch",
        {"manual_review": True, "signature_id": operator.sign("批准执行", target=batch_id)},
    )
    assert refused.status_code == 409, refused.text
    assert "角色「C」" in refused.text


# ---------- 滚动排程与承诺 / 预测窗口 ----------


def test_branch_downstream_is_forecast_until_decided_then_rolled(operator, db, executor, reset_runtime):
    def shape(steps):
        dry, weigh, assemble, test = steps
        return [
            dry,
            {"step_id": "b1", "name": "是否需要称重", "kind": "branch", "after": [dry["step_id"]],
             "branch": {"mode": "manual", "cases": [{"key": "weigh", "label": "称重"}, {"key": "skip", "label": "直接组装"}]}},
            {**weigh, "after": ["b1"], "when": {"b1": "weigh"}},
            {**_without_hard(assemble), "after": ["b1", weigh["step_id"]], "when": {"b1": "skip"}},
            {**test, "after": [assemble["step_id"]]},
        ]

    batch_id = _new_batch(operator)
    _reshape(db, batch_id, shape)
    _dispatch(operator, batch_id)
    detail = _run(operator, batch_id, executor, rounds=8, until=("never",))
    choice = next(row for row in detail["step_runs"] if row["step_id"] == "b1")
    assert choice["state"] == "ready"
    marks = {row["step_index"]: row for row in detail["allocations"] if row["kind"] == "work"}
    assert marks[2]["forecast"] and "分支" in marks[2]["forecast_reason"], "分支没定，下游只是预测"
    assert marks[4]["forecast"]
    board = operator.get("/api/schedule/board").json()
    items = [item for lane in board["stations"] for item in lane["items"] if item["batch_id"] == batch_id]
    assert any(item["forecast"] for item in items)

    chosen = operator.post(f"/api/step-runs/{choice['id']}/branch-decision", {
        "case": "skip", "reason": "极片上一批次已称过", "row_version": choice["row_version"],
    })
    assert chosen.status_code == 200, chosen.text
    after = _detail(operator, batch_id)
    assert not [row for row in after["allocations"] if row["step_index"] == 2], "没走的分支时间窗归还"
    assert any(event["action"] == "滚动重排" for event in after["audit"]), "定了路径后按实际路径重算尾段"
    tail = [row for row in after["allocations"] if row["step_index"] == 4 and row["kind"] == "work"]
    assert tail, "尾段重新预约"
    assert not tail[0]["forecast"] or "远期" in tail[0]["forecast_reason"], "路径定了，不再因为分支而只是预测"
