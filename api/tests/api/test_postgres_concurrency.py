"""PostgreSQL 专属并发验收。

这些用例刻意让两个真实连接在锁边界相遇，
验证正式环境依赖的 advisory lock、行锁和 SKIP LOCKED。
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import hashlib
from threading import Barrier
import time
import uuid

from sqlalchemy import text

from conftest import ORG


def _lock_id(*parts: str) -> int:
    scope = "\x1f".join(parts).encode()
    return int.from_bytes(hashlib.sha256(scope).digest()[:8], "big", signed=True)


def test_same_idempotency_key_is_serialized_across_real_connections(operator):
    """同键请求不能在领域服务内部 commit 后穿透并重复登记。"""
    from app.core.db import SessionLocal, engine
    from app.main import app
    from app.models import IdempotencyKey, PhysicalSample
    from fastapi.testclient import TestClient

    suffix = uuid.uuid4().hex[:10]
    sample_id = f"PS-PG-IDEM-{suffix}"
    key = f"pg-idem-{suffix}"
    action = "POST /api/samples"
    lock_id = _lock_id(ORG, operator.user["id"], action, key)
    payload = {
        "id": sample_id,
        "barcode": f"BC-PG-IDEM-{suffix}",
        "sample_type": "并发验收样本",
    }
    headers = {**operator.headers, "Idempotency-Key": key}

    def send_request():
        # TestClient 本身不承诺跨线程复用；每个请求使用独立客户端，确保测试中的
        # 并发来自两个 HTTP/DB 会话，而不是测试客户端内部调度。
        with TestClient(app) as concurrent_client:
            return concurrent_client.post("/api/samples", json=payload, headers=headers)

    with engine.connect() as blocker:
        blocker.execute(text("SELECT pg_advisory_lock(:lock_id)"), {"lock_id": lock_id})
        pool = ThreadPoolExecutor(max_workers=2)
        try:
            futures = [
                pool.submit(send_request)
                for _ in range(2)
            ]
            try:
                time.sleep(0.25)
                completed = [future for future in futures if future.done()]
                details = []
                for future in completed:
                    error = future.exception()
                    details.append(
                        repr(error) if error else (future.result().status_code, future.result().text)
                    )
                assert not completed, f"请求没有等待对应幂等 advisory lock：{details}"
            finally:
                blocker.execute(
                    text("SELECT pg_advisory_unlock(:lock_id)"), {"lock_id": lock_id}
                )
                blocker.commit()
            responses = [future.result(timeout=10) for future in futures]
        finally:
            pool.shutdown(wait=True)

    assert [response.status_code for response in responses] == [201, 201]
    assert responses[0].json() == responses[1].json()
    with SessionLocal() as verification:
        assert verification.query(PhysicalSample).filter_by(id=sample_id).count() == 1
        assert verification.query(IdempotencyKey).filter_by(key=key).count() == 1


def test_booking_conflict_rechecks_after_waiting_for_asset_row_lock(admin, operator):
    """被锁等待的预约醒来后必须看到先提交的占用并返回冲突。"""
    from app.core.clock import now
    from app.core.db import SessionLocal
    from app.models import Asset, ResourceBooking

    suffix = uuid.uuid4().hex[:10]
    created = admin.post(
        "/api/assets",
        {"asset_no": f"AS-PG-LOCK-{suffix}", "name": "PG 并发锁验收", "capacity": 1},
    )
    assert created.status_code == 201, created.text
    asset_id = created.json()["id"]
    starts_at = now() + timedelta(days=3)
    ends_at = starts_at + timedelta(hours=2)
    payload = {
        "asset_id": asset_id,
        "kind": "manual",
        "starts_at": starts_at.isoformat(),
        "ends_at": ends_at.isoformat(),
        "reason": "并发预约",
    }

    with SessionLocal() as winner:
        asset = (
            winner.query(Asset).filter(Asset.id == asset_id).with_for_update().one()
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                operator.client.post,
                "/api/resource-bookings",
                json=payload,
                headers=operator.headers,
            )
            try:
                time.sleep(0.25)
                assert not future.done(), "预约冲突检查没有等待资产行锁"
                winner.add(
                    ResourceBooking(
                        org_id=asset.org_id,
                        asset_id=asset.id,
                        kind="maintenance",
                        starts_at=starts_at,
                        ends_at=ends_at,
                        reason="先提交的维护占用",
                        state="confirmed",
                        created_by=admin.user["id"],
                    )
                )
                winner.commit()
            finally:
                if winner.in_transaction():
                    winner.rollback()
            response = future.result(timeout=10)

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "booking_conflict"


def test_workflow_claim_uses_skip_locked_between_workers():
    """一个推进器锁住的事件不会阻塞或重复出现在另一个推进器的领取批次。"""
    from app.core.clock import now
    from app.core.context import system_context
    from app.core.db import SessionLocal
    from app.models import WorkflowEvent
    from app.repositories.workflow import WorkflowEventRepository

    event_key = f"pg-claim-{uuid.uuid4().hex}"
    with SessionLocal() as setup:
        event = WorkflowEvent(
            org_id=ORG,
            batch_id="",
            step_run_id="",
            event_type="concurrency_probe",
            event_key=event_key,
            payload={},
            available_at=now(),
        )
        setup.add(event)
        setup.commit()
        event_id = event.id

    with SessionLocal() as first:
        repository = WorkflowEventRepository(first, system_context(ORG, "推进器 A"))
        locked = repository.claim_batch(limit=100)
        assert any(row.id == event_id for row in locked)

        def claim_elsewhere() -> list[str]:
            with SessionLocal() as second:
                rows = WorkflowEventRepository(
                    second, system_context(ORG, "推进器 B")
                ).claim_batch(limit=100)
                return [row.id for row in rows]

        with ThreadPoolExecutor(max_workers=1) as pool:
            claimed_elsewhere = pool.submit(claim_elsewhere).result(timeout=5)
        assert event_id not in claimed_elsewhere
        event = first.get(WorkflowEvent, event_id)
        event.state = "rejected"
        event.error = "并发锁验收完成"
        first.commit()


def test_concurrent_batch_reservations_cannot_overbook_one_lot(operator):
    """两个事务同时预留 7/10，最终只能成功一个且可用量不能为负。"""
    from app.core.db import SessionLocal, dec
    from app.core.errors import StateConflict
    from app.models import (
        InventoryEvent, InventoryLedger, Lot, Material, Reservation, User,
    )
    from app.services.identity_service import IdentityService
    from app.services.inventory_service import InventoryService

    suffix = uuid.uuid4().hex[:10]
    material_name = f"PG 并发物料 {suffix}"
    lot_id = f"LOT-PG-{suffix}"
    with SessionLocal() as setup:
        material = Material(
            org_id=ORG,
            code=f"MAT-PG-{suffix}",
            name=material_name,
            base_unit="g",
            state="active",
        )
        setup.add(material)
        setup.flush()
        setup.add(
            Lot(
                id=lot_id,
                org_id=ORG,
                material_id=material.id,
                material=material.name,
                qty="10",
                opening_balance="10",
                unit="g",
                release="已放行",
                expiry="2099-12-31",
                state="active",
            )
        )
        setup.commit()

    barrier = Barrier(2)

    def reserve(batch_id: str) -> str:
        with SessionLocal() as session:
            user = session.get(User, operator.user["id"])
            ctx = IdentityService(session).context_for(user)
            barrier.wait(timeout=5)
            try:
                InventoryService(session, ctx).reserve_for_batch(
                    batch_id,
                    [{"material": material_name, "qty": "7", "unit": "g"}],
                    user,
                )
                session.commit()
                return "reserved"
            except StateConflict as exc:
                session.rollback()
                assert exc.code == "material_insufficient"
                return "insufficient"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(reserve, (f"B-PG-A-{suffix}", f"B-PG-B-{suffix}")))

    assert sorted(outcomes) == ["insufficient", "reserved"]
    with SessionLocal() as verification:
        try:
            reservations = (
                verification.query(Reservation).filter(Reservation.lot_id == lot_id).all()
            )
            assert len(reservations) == 1
            assert sum((dec(row.qty) for row in reservations), dec(0)) == dec(7)
        finally:
            # 这是锁时序探针，不是库存业务样例；移除直建的期初批号，避免污染后续
            # “每个批号都有期初流水”的迁移核对。
            event_ids = [
                row.id for row in verification.query(InventoryEvent).filter(
                    InventoryEvent.event_id.in_(
                        [f"reserve-B-PG-A-{suffix}", f"reserve-B-PG-B-{suffix}"]
                    )
                ).all()
            ]
            if event_ids:
                verification.query(InventoryLedger).filter(
                    InventoryLedger.event_row_id.in_(event_ids)
                ).delete(synchronize_session=False)
                verification.query(InventoryEvent).filter(
                    InventoryEvent.id.in_(event_ids)
                ).delete(synchronize_session=False)
            verification.query(Reservation).filter(Reservation.lot_id == lot_id).delete(
                synchronize_session=False
            )
            verification.query(Lot).filter(Lot.id == lot_id).delete(
                synchronize_session=False
            )
            verification.query(Material).filter(Material.code == f"MAT-PG-{suffix}").delete(
                synchronize_session=False
            )
            verification.commit()


def test_concurrent_scheduling_serializes_on_station_timeline(operator, reset_runtime):
    """两个排程请求读到同一份时间线会把同一工位排两次：后到者必须等锁并在锁内重读。"""
    from app.core.context import system_context
    from app.core.db import SessionLocal, serialize
    from app.models import Batch, User
    from app.services.schedule_service import SCHEDULE_LOCK, ScheduleService

    first = operator.post("/api/batches", {"plan_id": "EP-205-01"}).json()["id"]
    second = operator.post("/api/batches", {"plan_id": "EP-205-01"}).json()["id"]
    headers = {**operator.headers, "Idempotency-Key": f"pg-schedule-{uuid.uuid4().hex[:10]}"}

    with SessionLocal() as winner:
        serialize(winner, SCHEDULE_LOCK)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                operator.client.post, f"/api/batches/{second}/schedule", json={}, headers=headers,
            )
            try:
                time.sleep(0.3)
                assert not future.done(), "排程没有等待工位时间线锁"
                ScheduleService(winner, system_context(ORG)).schedule(
                    winner.get(Batch, first), None, None, winner.get(User, operator.user["id"]),
                )
                winner.commit()
            finally:
                if winner.in_transaction():
                    winner.rollback()
            response = future.result(timeout=10)

    assert response.status_code == 200, response.text
    with SessionLocal() as db:
        overlaps = [
            row for row in ScheduleService(db, system_context(ORG)).conflicts()
            if {row["a"]["batch_id"], row["b"]["batch_id"]} == {first, second}
        ]
    assert overlaps == [], "后到的排程必须看到先提交的占用"
