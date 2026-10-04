"""按瓶拆开下发：一次只能处理一瓶的设备（接入配置 wells_per_command: 1，如秤上一个位置的天平），一步两瓶时 ILCS 仍记
一条指令，拆成依次执行的设备指令 <指令号>/1、/2；每瓶做完就按它的实际量入账，全部做完再按瓶汇总写检查点。
哪一瓶失败，整步按失败走，已做完的瓶照样入账；重试跳过已经做完的瓶。"""
from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from app.adapters.base import AdapterContract, AdapterError, CommandResult
from app.adapters.registry import REAL_IMPLEMENTATIONS, reset_cache
from app.core.clock import now
from app.core.context import system_context
from app.core.db import SessionLocal
from app.models import Adapter, Batch, Checkpoint, Command, InventoryEvent, Reservation
from app.services.execution_service import ExecutionService
from test_graph_workflow import _dispatch, _run
from test_step_materials import _lot, _plan_batch


class OneBottleDevice:
    """秤上只有一个位置：一条指令只收一瓶，多瓶明确拒绝（设备没动）。按指令号回报：加的量比目标多 1 mg。"""

    submitted: dict[str, dict] = {}
    fail_on: set[str] = set()
    aborted: list[str] = []
    hang: bool = False  # 回报一直「执行中」：用来在中途终止

    def __init__(self, record):
        self.contract = AdapterContract(kind="real", protocol="test-one-bottle", version="1", supports_hold=False,
                                        supports_abort=True, supports_query=True, supports_dedup=True)

    def healthcheck(self):
        return {"reachable": True, "device_id": "ONE-BOTTLE", "simulator": True}

    def submit(self, request):
        wells = (request.params or {}).get("wells") or {}
        if len(wells) != 1:
            raise AdapterError(f"秤上只有一个位置：一条指令只收一瓶，这条带了 {len(wells)} 瓶")
        type(self).submitted[request.command_id] = {"wells": dict(wells), "material": dict(request.material or {})}
        return CommandResult(command_id=request.command_id, state="accepted", device_ts=now(), origin="real:test-one")

    def query(self, command_id):
        found = type(self).submitted.get(command_id)
        if found is None:
            return None
        (well, values), = found["wells"].items()
        if type(self).hang:
            return CommandResult(command_id=command_id, state="running", device_ts=now(), origin="real:test-one")
        if well in type(self).fail_on:
            return CommandResult(command_id=command_id, state="failed", device_ts=now(), origin="real:test-one",
                                 error="出粉故障：这一瓶加了 0.4 g")
        mass = round(float(values["mass"]) + 0.001, 6)
        name = found["material"].get("name")
        return CommandResult(
            command_id=command_id, state="done", device_ts=now(), origin="real:test-one",
            delivered={"wells": {well: {"mass": mass, "target": values["mass"]}}, "material": name,
                       "materials": [{"material": name, "unit": "g", "quantity": mass}]},
        )

    def hold(self, request):
        raise AdapterError("不支持保持")

    def abort(self, request):
        type(self).aborted.append(request.target_command_id)
        return CommandResult(command_id=request.command_id, state="done", device_ts=now(), origin="real:test-one")


@pytest.fixture()
def one_bottle(monkeypatch):
    OneBottleDevice.submitted, OneBottleDevice.fail_on, OneBottleDevice.aborted = {}, set(), []
    OneBottleDevice.hang = False
    monkeypatch.setitem(REAL_IMPLEMENTATIONS, "test_one_bottle", OneBottleDevice)
    saved: dict[str, dict] = {}

    def attach(batch_id: str) -> list[str]:
        """这一批的设备步骤所在工位都接上「一次一瓶」的设备，配 wells_per_command: 1。"""
        with SessionLocal() as db:
            stations = sorted({row.station_id for row in db.query(Command).filter(Command.batch_id == batch_id)})
            for station_id in stations:
                adapter = db.get(Adapter, station_id)
                saved.setdefault(station_id, {key: getattr(adapter, key) for key in (
                    "kind", "driver", "protocol", "config", "supports_hold", "supports_query", "supports_dedup")})
                adapter.kind, adapter.driver, adapter.protocol = "real", "test_one_bottle", "test-one-bottle"
                adapter.config = {"wells_per_command": 1}
                adapter.supports_hold, adapter.supports_query, adapter.supports_dedup = False, True, True
                adapter.config_version += 1
            db.commit()
        reset_cache()
        return stations

    yield attach
    with SessionLocal() as db:
        for station_id, values in saved.items():
            adapter = db.get(Adapter, station_id)
            for key, value in values.items():
                setattr(adapter, key, value)
            adapter.current_command_id = ""
        db.commit()
    reset_cache()


def _batch(researcher, qa, operator, tag: str) -> tuple[str, str]:
    solvent, salt = f"逐瓶溶剂-{tag}", f"逐瓶锂盐-{tag}"
    _lot(operator, qa, solvent, qty="100")
    _lot(operator, qa, salt, qty="100")
    return _plan_batch(researcher, qa, operator, solvent, salt), solvent


def test_two_bottles_go_out_one_by_one_and_each_is_booked_by_its_actual_amount(
    researcher, qa, operator, reset_runtime, executor, one_bottle,
):
    batch_id, solvent = _batch(researcher, qa, operator, uuid4().hex[:6])
    _dispatch(operator, batch_id)
    one_bottle(batch_id)
    detail = _run(operator, batch_id, executor)
    assert detail["state"] == "done", detail["failure_reason"]
    listed = [row for row in detail["commands"] if row["type"] == "dispatch"]
    assert all([run["state"] for run in row["runs"]] == ["done", "done"] for row in listed), "批次详情里看得到逐瓶进度"

    with SessionLocal() as db:
        commands = db.query(Command).filter(Command.batch_id == batch_id, Command.type == "dispatch").order_by(
            Command.step_index).all()
        assert len(commands) == 2
        for command in commands:
            wells = list(command.params["wells"])
            assert len(wells) == 2
            assert [run["id"] for run in command.runs] == [f"{command.id}/1", f"{command.id}/2"]
            assert [run["wells"] for run in command.runs] == [[well] for well in wells]
            assert all(run["state"] == "done" for run in command.runs)
            sent = [OneBottleDevice.submitted[run["id"]]["wells"] for run in command.runs]
            assert [list(item) for item in sent] == [[well] for well in wells], "每条设备指令只带一瓶"
            checkpoint = db.get(Checkpoint, command.checkpoint_id)
            delivered = checkpoint.payload["delivered"]
            assert set(delivered["wells"]) == set(wells), "检查点按瓶汇总"
            assert [run["id"] for run in delivered["runs"]] == [run["id"] for run in command.runs]
            assert db.query(InventoryEvent).filter(InventoryEvent.event_id == f"{command.id}#1").count() == 0, (
                "汇总回执不再入一遍账"
            )
            for run in command.runs:
                assert db.query(InventoryEvent).filter(InventoryEvent.event_id == f"{run['id']}#1").count() == 1

        first = commands[0]
        reservation = next(row for row in db.query(Reservation).filter(Reservation.batch_id == batch_id).all()
                           if row.consumed_qty and Decimal(row.consumed_qty) > Decimal("5"))
        assert Decimal(reservation.consumed_qty) == Decimal("5.752"), "两瓶各按天平称出来的量入账（2.501 + 3.251）"
        assert db.query(InventoryEvent).filter(InventoryEvent.event_id == f"{first.id}/2#1:topup").count() == 1, (
            "第二瓶比剩余预留多 1 mg：在偏差阈值内，自动追加预留"
        )


def test_a_failed_bottle_stops_the_rest_books_the_finished_ones_and_retry_skips_them(
    researcher, qa, operator, reset_runtime, executor, one_bottle,
):
    batch_id, solvent = _batch(researcher, qa, operator, uuid4().hex[:6])
    _dispatch(operator, batch_id)
    one_bottle(batch_id)
    with SessionLocal() as db:
        first = db.query(Command).filter(Command.batch_id == batch_id, Command.type == "dispatch").order_by(
            Command.step_index).first()
        wells = list(first.params["wells"])
        command_id = first.id
    OneBottleDevice.fail_on = {wells[1]}
    detail = _run(operator, batch_id, executor)
    assert detail["state"] == "fault"

    with SessionLocal() as db:
        command = db.get(Command, command_id)
        assert [run["state"] for run in command.runs] == ["done", "failed"]
        assert command.outcome == "failed"
        assert "第 2/2 条" in command.error and f"已做完 {wells[0]}" in command.error and "出粉故障" in command.error
        assert db.query(InventoryEvent).filter(InventoryEvent.event_id == f"{command_id}/1#1").count() == 1, (
            "做完的那一瓶照样按实际量入账"
        )
        assert db.query(InventoryEvent).filter(InventoryEvent.event_id.like(f"{command_id}/2#%")).count() == 0

        # 人工确认后重试：新指令接续这一条，已经做完的瓶照搬回执，不再下发
        batch = db.get(Batch, batch_id)
        retry = Command(batch_id=batch_id, station_id=command.station_id, capability=command.capability,
                        params=command.params, method=command.method, type="retry", step_index=command.step_index,
                        step_run_id=command.step_run_id, target_command_id=command.id, org_id=command.org_id)
        db.add(retry)
        db.flush()
        service = ExecutionService(db, system_context(batch.org_id, "测试"))
        record = db.get(Adapter, command.station_id)
        assert service._plan_runs(batch, retry, record, service._base_request(batch, retry))
        assert [(run["id"], run["wells"], run["state"]) for run in retry.runs] == [
            (f"{command_id}/1", [wells[0]], "done"), (f"{retry.id}/2", [wells[1]], "pending"),
        ]
        assert retry.runs[0]["carried_from"] == command_id
        db.rollback()


def test_abort_stops_the_bottle_on_the_device_and_the_rest_are_never_sent(
    researcher, qa, operator, reset_runtime, executor, one_bottle,
):
    batch_id, solvent = _batch(researcher, qa, operator, uuid4().hex[:6])
    _dispatch(operator, batch_id)
    one_bottle(batch_id)
    OneBottleDevice.hang = True
    executor()
    executor()
    with SessionLocal() as db:
        command = db.query(Command).filter(Command.batch_id == batch_id, Command.type == "dispatch").order_by(
            Command.step_index).first()
        command_id = command.id
        assert [run["state"] for run in command.runs] == ["running", "pending"]
    aborted = operator.post(f"/api/batches/{batch_id}/abort", {
        "reason": "第一瓶加料异常，安全终止", "signature_id": operator.sign("安全终止", target=batch_id),
    })
    assert aborted.status_code == 200, aborted.text
    for _ in range(5):
        executor()
    assert OneBottleDevice.aborted == [f"{command_id}/1"], "终止发给正在做的那一瓶"
    assert f"{command_id}/2" not in OneBottleDevice.submitted, "后面的瓶不再下发"
    detail = operator.get(f"/api/batches/{batch_id}").json()
    assert detail["state"] == "aborted", detail["state"]
