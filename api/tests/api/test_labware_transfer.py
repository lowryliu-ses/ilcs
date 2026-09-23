"""载具位置与可执行转运。

- 绑定了载具的批次：换工位前先投一条转运指令给承运工位，设备动作等它确认完成才投递；
  板的位置只按转运回执或扫码更新。
- 转运结果未知：等着它的设备动作撤回，现场核查后恢复，不重复搬、不盲目重试。
- 下发前发现板送不到首个工位（位置未知、没空位）：不签名、不下发，原因写清楚。
- 转运途中不能保持；终止发给承运工位，终止后板的位置标为未知。
- 没绑定载具的批次行为与之前一致（其余用例都在覆盖这一点）。
"""
import uuid

import pytest

from tests.conftest import ORG


@pytest.fixture()
def clean_labware(reset_runtime):
    """每个用例从空产线开始：之前用例留在工位 / 板库上的载具一律报废下线。"""
    from app.core.db import SessionLocal
    from app.models import Labware

    def clear():
        with SessionLocal() as db:
            db.query(Labware).update({"location_id": None, "state": "retired"}, synchronize_session=False)
            db.commit()

    clear()
    yield
    clear()


def _register(operator, location_id: str | None = "HOTEL-01/S01", type_id: str = "LT-TRAY-8") -> dict:
    barcode = f"TRAY-{uuid.uuid4().hex[:8]}"
    created = operator.post(
        "/api/labware", {"barcode": barcode, "type_id": type_id, "location_id": location_id},
    )
    assert created.status_code == 201, created.text
    return created.json()


def _batch_with_labware(operator, location_id: str | None = "HOTEL-01/S01") -> tuple[str, dict]:
    created = operator.post("/api/batches", {"plan_id": "EP-205-01"})
    assert created.status_code == 201, created.text
    batch_id = created.json()["id"]
    labware = _register(operator, location_id)
    bound = operator.post(f"/api/batches/{batch_id}/labware", {"labware_id": labware["id"]})
    assert bound.status_code == 200, bound.text
    assert operator.post(f"/api/batches/{batch_id}/schedule", {}).status_code == 200
    return batch_id, labware


def _dispatch(operator, batch_id: str):
    return operator.post(
        f"/api/batches/{batch_id}/dispatch",
        {"manual_review": True, "signature_id": operator.sign("批准执行", target=batch_id)},
    )


def _commands(operator, batch_id: str) -> list[dict]:
    return operator.get(f"/api/batches/{batch_id}").json()["commands"]


def test_device_step_waits_for_transfer_and_location_follows_receipts(operator, clean_labware, executor):
    batch_id, labware = _batch_with_labware(operator)
    dispatched = _dispatch(operator, batch_id)
    assert dispatched.status_code == 200, dispatched.text

    commands = _commands(operator, batch_id)
    transfer = next(c for c in commands if c["type"] == "transfer")
    action = next(c for c in commands if c["type"] == "dispatch")
    assert transfer["station_id"].startswith("AGV-")
    assert action["after_command_id"] == transfer["id"], "设备动作以转运为前置"

    from app.core.db import SessionLocal
    from app.repositories.execution import CommandRepository

    with SessionLocal() as db:
        queued = {c.id for c in CommandRepository(db).pending(50)}
    assert transfer["id"] in queued and action["id"] not in queued, "转运没确认完成，设备动作不进候选"

    for _ in range(20):
        executor()
        if operator.get(f"/api/batches/{batch_id}").json()["state"] == "done":
            break
    detail = operator.get(f"/api/batches/{batch_id}").json()
    assert detail["state"] == "done", detail["failure_reason"]

    moves = operator.get(f"/api/labware/{labware['id']}/moves").json()
    path = [(row["from"], row["to"], row["source"]) for row in reversed(moves)]
    assert path[0] == ("", "HOTEL-01/S01", "manual"), "登记时扫码放置"
    # 干燥与称重都在 ST-05：只搬一次；之后组装 ST-06、测试 ST-07 各搬一次
    assert [row[1] for row in path[1:]] == ["ST-05/N1", "ST-06/N1", "ST-07/N1"]
    assert all(row[2] == "transfer" for row in path[1:])
    assert detail["labware"]["location_id"] == "ST-07/N1"

    with SessionLocal() as db:
        from app.models import Command

        rows = db.query(Command).filter(Command.batch_id == batch_id).order_by(Command.created_at).all()
        for row in rows:
            if row.after_command_id:
                before = db.get(Command, row.after_command_id)
                assert before.state == "done" and before.started_at <= row.started_at


def test_unknown_transfer_withdraws_action_and_recovers_after_verification(
    operator, clean_labware, executor,
):
    from app.adapters import AdapterUnreachable
    from app.adapters.simulation import SimulationAdapter

    batch_id, labware = _batch_with_labware(operator)
    assert _dispatch(operator, batch_id).status_code == 200

    original = SimulationAdapter.submit

    def flaky(self, request):
        if request.type == "transfer":
            raise AdapterUnreachable("AGV 调度网关超时")
        return original(self, request)

    SimulationAdapter.submit = flaky
    try:
        executor()
    finally:
        SimulationAdapter.submit = original

    detail = operator.get(f"/api/batches/{batch_id}").json()
    assert detail["state"] == "fault"
    transfer = next(c for c in detail["commands"] if c["type"] == "transfer")
    action = next(c for c in detail["commands"] if c["type"] == "dispatch")
    assert (transfer["state"], transfer["delivery_state"]) == ("unknown", "maybe_sent")
    assert (action["state"], action["delivery_state"]) == ("cancelled", "not_sent"), "设备从未收到动作"
    assert detail["labware"]["location_id"] == "HOTEL-01/S01", "结果未知时不改位置"

    blocked = operator.get(f"/api/batches/{batch_id}/recovery-options").json()
    assert blocked["blind_retry_allowed"] is False

    verified = operator.post(
        f"/api/commands/{transfer['id']}/verify",
        {"conclusion": "executed", "note": "AGV 日志显示已放到 ST-05",
         "signature_id": operator.sign("已到现场核实", target=transfer["id"])},
    )
    assert verified.status_code == 200, verified.text
    assert operator.get(f"/api/batches/{batch_id}").json()["labware"]["location_id"] == "ST-05/N1"

    resumed = operator.post(
        f"/api/batches/{batch_id}/recover",
        {"strategy": "resume", "verified": True, "signature_id": operator.sign("已核实实际量与设备状态")},
    )
    assert resumed.status_code == 200, resumed.text
    after = _commands(operator, batch_id)
    assert sum(1 for c in after if c["type"] == "transfer") == 1, "板已在工位上，不重复搬"
    fresh = after[-1]
    assert fresh["type"] == "dispatch" and fresh["after_command_id"] == ""
    for _ in range(20):
        executor()
    assert operator.get(f"/api/batches/{batch_id}").json()["state"] == "done"


def test_partial_transfer_marks_labware_lost_until_rescanned(operator, clean_labware, executor):
    from app.adapters import AdapterUnreachable
    from app.adapters.simulation import SimulationAdapter

    batch_id, labware = _batch_with_labware(operator)
    assert _dispatch(operator, batch_id).status_code == 200
    original = SimulationAdapter.submit
    SimulationAdapter.submit = lambda self, request: (_ for _ in ()).throw(AdapterUnreachable("超时"))
    try:
        executor()
    finally:
        SimulationAdapter.submit = original
    transfer = next(c for c in _commands(operator, batch_id) if c["type"] == "transfer")
    assert operator.post(
        f"/api/commands/{transfer['id']}/verify",
        {"conclusion": "partial", "note": "AGV 停在走廊，托盘在车上",
         "signature_id": operator.sign("已到现场核实", target=transfer["id"])},
    ).status_code == 200
    state = operator.get(f"/api/batches/{batch_id}").json()["labware"]
    assert state["state"] == "lost" and state["location_id"] == ""

    refused = operator.post(
        f"/api/batches/{batch_id}/recover",
        {"strategy": "resume", "verified": True, "signature_id": operator.sign("已核实实际量与设备状态")},
    )
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "transfer_blocked"
    assert "位置未知" in refused.text

    placed = operator.post(
        f"/api/labware/{labware['id']}/move",
        {"barcode": labware["barcode"], "to_location_id": "ST-05/N1", "reason": "人工放回干燥站"},
    )
    assert placed.status_code == 200, placed.text
    resumed = operator.post(
        f"/api/batches/{batch_id}/recover",
        {"strategy": "resume", "verified": True, "signature_id": operator.sign("已核实实际量与设备状态")},
    )
    assert resumed.status_code == 200, resumed.text


def test_dispatch_is_refused_before_signing_when_labware_cannot_be_delivered(operator, clean_labware):
    batch_id, _ = _batch_with_labware(operator, location_id=None)
    signature = operator.sign("批准执行", target=batch_id)
    refused = operator.post(
        f"/api/batches/{batch_id}/dispatch", {"manual_review": True, "signature_id": signature},
    )
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "transfer_blocked"
    assert "位置未知" in refused.text
    assert operator.get(f"/api/batches/{batch_id}").json()["state"] == "scheduled"


def test_full_destination_blocks_dispatch_with_reason(operator, clean_labware):
    occupant = _register(operator, "ST-05/N1")
    batch_id, _ = _batch_with_labware(operator)
    refused = _dispatch(operator, batch_id)
    assert refused.status_code == 409, refused.text
    assert "ST-05 的放置位都被占用" in refused.text
    assert occupant["location_id"] == "ST-05/N1"


def test_transfer_in_flight_cannot_be_held_and_abort_targets_the_carrier(
    operator, clean_labware, executor, monkeypatch,
):
    from app.adapters.base import CommandResult
    from app.adapters.simulation import SimulationAdapter
    from app.core.clock import now

    original = SimulationAdapter.submit

    def slow_transfer(self, request):
        if request.type == "transfer":
            result = CommandResult(command_id=request.command_id, state="accepted", device_ts=now(),
                                   origin="simulation")
            self._ledger[request.command_id] = CommandResult(
                command_id=request.command_id, state="running", device_ts=now(), origin="simulation",
            )
            return result
        return original(self, request)

    monkeypatch.setattr(SimulationAdapter, "submit", slow_transfer)
    batch_id, labware = _batch_with_labware(operator)
    assert _dispatch(operator, batch_id).status_code == 200
    executor()
    transfer = next(c for c in _commands(operator, batch_id) if c["type"] == "transfer")
    assert transfer["state"] == "running"

    held = operator.post(f"/api/batches/{batch_id}/hold", {"reason": "想暂停"})
    assert held.status_code == 409 and held.json()["detail"]["code"] == "transfer_in_progress"

    aborted = operator.post(
        f"/api/batches/{batch_id}/abort",
        {"reason": "现场异常", "signature_id": operator.sign("安全终止", target=batch_id)},
    )
    assert aborted.status_code == 200 and aborted.json()["state"] == "aborting"
    abort = next(c for c in _commands(operator, batch_id) if c["type"] == "abort")
    assert abort["station_id"] == transfer["station_id"], "要停的是正在搬的承运工位"

    executor()
    detail = operator.get(f"/api/batches/{batch_id}").json()
    assert detail["state"] == "aborted"
    assert detail["labware"]["state"] == "lost", "搬到一半终止：板的位置不可信"


def test_manual_move_checks_barcode_and_occupancy(operator, clean_labware):
    first = _register(operator, "HOTEL-01/S01")
    second = _register(operator, "HOTEL-01/S02")
    wrong = operator.post(
        f"/api/labware/{second['id']}/move", {"barcode": first["barcode"], "to_location_id": "HOTEL-01/S03"},
    )
    assert wrong.status_code == 422 and wrong.json()["detail"]["code"] == "barcode_mismatch"
    taken = operator.post(
        f"/api/labware/{second['id']}/move", {"barcode": second["barcode"], "to_location_id": "HOTEL-01/S01"},
    )
    assert taken.status_code == 409 and first["barcode"] in taken.text


def test_floor_shows_stations_slots_and_hides_other_organizations_barcodes(operator, clean_labware, db):
    from app.models import Labware

    mine = _register(operator, "HOTEL-01/S01")
    db.add(Labware(org_id="ORG-002", barcode="FOREIGN-1", type_id="LT-TRAY-8", location_id="HOTEL-01/S02"))
    db.commit()
    floor = operator.get("/api/floor").json()
    assert floor["tracking"] is True
    assert any(station["id"] == "ST-05" and station["nests"] for station in floor["stations"])
    hotel = next(group for group in floor["storage"] if group["group"] == "HOTEL-01")
    slots = {slot["id"]: slot for slot in hotel["slots"]}
    assert slots["HOTEL-01/S01"]["labware"]["barcode"] == mine["barcode"]
    assert slots["HOTEL-01/S02"]["labware"]["barcode"] == "已占用"
    assert "FOREIGN-1" not in str(floor)
    assert ORG  # 组织上下文来自登录，不来自请求
