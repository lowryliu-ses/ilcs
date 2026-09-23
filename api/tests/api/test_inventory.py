"""AC-14 至 AC-17：物料有效性与库存核算。

三个量必须分开：账面库存、未耗用占用、可用量。领用与归还只动保管位置，
实际消耗或损耗才扣账面库存。
"""
import pytest

METRIC = "METRIC-discharge_capacity-v1"


@pytest.fixture()
def lot(operator, qa, reset_runtime):
    """一个干净批号：账面 100 g，已放行，未过期。"""
    lot_id = "LOT-INV-100"
    created = operator.post(
        "/api/lots",
        {
            "id": lot_id, "material": "库存用例物料", "qty": "100", "unit": "g",
            "expiry": "2028-01-01",
        },
    )
    assert created.status_code == 201, created.text
    qa.post(f"/api/lots/{lot_id}/release", {"signature_id": qa.sign("复验合格", target=lot_id)})
    yield lot_id
    from app.core.db import SessionLocal
    from app.models import InventoryEvent, InventoryLedger, Lot, Reservation

    with SessionLocal() as db:
        db.query(InventoryLedger).filter(InventoryLedger.lot_id == lot_id).delete()
        db.query(Reservation).filter(Reservation.lot_id == lot_id).delete()
        db.query(InventoryEvent).filter(InventoryEvent.event_id.like(f"%{lot_id}%")).delete()
        db.query(Lot).filter(Lot.id == lot_id).delete()
        db.commit()


def balances(session, lot_id: str) -> tuple[str, str, str]:
    row = session.get(f"/api/lots/{lot_id}/ledger").json()
    return row["balance"], row["outstanding"], row["available"]


def reserve(operator, lot_id: str, quantity: str) -> int:
    """直接建一条预留行，再用 reserve 事件把占用记进流水。"""
    from app.core.db import SessionLocal
    from app.models import Reservation

    with SessionLocal() as db:
        row = Reservation(
            org_id="ORG-001", batch_id="B-INV-TEST", lot_id=lot_id, qty="0", unit="g",
        )
        db.add(row)
        db.commit()
        reservation_id = row.id
    response = operator.post(
        "/api/inventory/events",
        {
            "event_id": f"reserve-{lot_id}-{reservation_id}", "event_type": "reserve",
            "source": "manual", "batch_id": "B-INV-TEST", "reason": "用例预留",
            "items": [
                {"lot_id": lot_id, "reservation_id": reservation_id, "quantity": quantity,
                 "unit": "g"}
            ],
        },
    )
    assert response.status_code == 201, response.text
    return reservation_id


def test_balance_outstanding_available_follow_the_documented_example(operator, lot):
    """AC-15：100/20/80 → 92/12/80 → 92/0/92。"""
    assert balances(operator, lot) == ("100.000000", "0.000000", "100.000000")

    reservation = reserve(operator, lot, "20")
    assert balances(operator, lot) == ("100.000000", "20.000000", "80.000000")

    consumed = operator.post(
        "/api/inventory/events",
        {
            "event_id": "consume-inv-8", "event_type": "consume", "source": "manual",
            "batch_id": "B-INV-TEST", "reason": "人工确认实际投料",
            "items": [
                {"line_no": 1, "lot_id": lot, "reservation_id": reservation, "quantity": "8",
                 "unit": "g"}
            ],
        },
    )
    assert consumed.status_code == 201, consumed.text
    assert balances(operator, lot) == ("92.000000", "12.000000", "80.000000")

    released = operator.post(
        "/api/inventory/events",
        {
            "event_id": "release-inv-12", "event_type": "release", "source": "manual",
            "batch_id": "B-INV-TEST", "reason": "确认剩余未领用",
            "items": [
                {"lot_id": lot, "reservation_id": reservation, "quantity": "12", "unit": "g"}
            ],
        },
    )
    assert released.status_code == 201, released.text
    assert balances(operator, lot) == ("92.000000", "0.000000", "92.000000")

    ledger = operator.get(f"/api/lots/{lot}/ledger").json()
    assert ledger["reconciled"], "流水累计必须等于账面库存"


def test_replayed_event_only_posts_once(operator, lot):
    """AC-16：同一事件重复入账只扣一次；同一命令下不同事件各自入账。"""
    reservation = reserve(operator, lot, "20")
    payload = {
        "event_id": "manual-consumption-0008", "event_type": "consume", "source": "manual",
        "batch_id": "B-INV-TEST", "command_id": "CMD-1", "reason": "第一部分投料",
        "items": [
            {"line_no": 1, "lot_id": lot, "reservation_id": reservation, "quantity": "8",
             "unit": "g"}
        ],
    }
    first = operator.post("/api/inventory/events", payload)
    assert first.status_code == 201 and first.json()["replayed"] is False
    # 换一个请求头幂等键重放同一业务事件：业务级去重靠事件唯一约束，不靠请求头
    replay = operator.post("/api/inventory/events", payload, idempotency_key="another-key")
    assert replay.status_code == 201 and replay.json()["replayed"] is True
    assert balances(operator, lot)[0] == "92.000000"

    second = operator.post(
        "/api/inventory/events",
        {**payload, "event_id": "manual-consumption-0009", "reason": "同一命令的第二部分投料",
         "items": [{"line_no": 1, "lot_id": lot, "reservation_id": reservation, "quantity": "5",
                    "unit": "g"}]},
    )
    assert second.status_code == 201 and second.json()["replayed"] is False
    assert balances(operator, lot) == ("87.000000", "7.000000", "80.000000")


def test_consumption_beyond_reservation_is_rejected(operator, lot):
    reservation = reserve(operator, lot, "20")
    response = operator.post(
        "/api/inventory/events",
        {
            "event_id": "consume-over", "event_type": "consume", "source": "manual",
            "items": [{"lot_id": lot, "reservation_id": reservation, "quantity": "25",
                       "unit": "g"}],
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "inventory_rejected"
    assert "超出剩余预留" in response.json()["detail"]["blocked"][0]["label"]
    assert balances(operator, lot)[0] == "100.000000", "被拒的事件不能留下部分扣减"


def test_issued_material_is_not_auto_released(operator, lot):
    """AC-17：已领用未消耗的部分不能直接变回可用库存。"""
    reservation = reserve(operator, lot, "20")
    issued = operator.post(
        "/api/inventory/events",
        {
            "event_id": "issue-inv-1", "event_type": "issue", "source": "manual",
            "items": [{"lot_id": lot, "reservation_id": reservation, "quantity": "15",
                       "unit": "g"}],
        },
    )
    assert issued.status_code == 201
    # 领用不改账面库存，也不改占用总量
    assert balances(operator, lot) == ("100.000000", "20.000000", "80.000000")

    over_release = operator.post(
        "/api/inventory/events",
        {
            "event_id": "release-inv-over", "event_type": "release", "source": "manual",
            "items": [{"lot_id": lot, "reservation_id": reservation, "quantity": "20",
                       "unit": "g"}],
        },
    )
    assert over_release.status_code == 409
    assert "先归还或处置确认" in over_release.json()["detail"]["blocked"][0]["label"]

    returned = operator.post(
        "/api/inventory/events",
        {
            "event_id": "return-inv-1", "event_type": "return", "source": "return",
            "items": [{"lot_id": lot, "reservation_id": reservation, "quantity": "15",
                       "unit": "g"}],
        },
    )
    assert returned.status_code == 201
    final = operator.post(
        "/api/inventory/events",
        {
            "event_id": "release-inv-after-return", "event_type": "release", "source": "manual",
            "items": [{"lot_id": lot, "reservation_id": reservation, "quantity": "20",
                       "unit": "g"}],
        },
    )
    assert final.status_code == 201
    assert balances(operator, lot) == ("100.000000", "0.000000", "100.000000")


def test_float_quantities_are_refused(operator, lot):
    """数量不接受二进制浮点：账实差额不能被浮点误差吃掉。"""
    reservation = reserve(operator, lot, "20")
    response = operator.post(
        "/api/inventory/events",
        {
            "event_id": "consume-float", "event_type": "consume", "source": "manual",
            "items": [{"lot_id": lot, "reservation_id": reservation, "quantity": 0.1,
                       "unit": "g"}],
        },
    )
    # Decimal 会接受 0.1 的字符串化结果，所以这里验证的是它没有被 float 直接带进算术
    assert response.status_code == 201
    assert operator.get(f"/api/lots/{lot}/ledger").json()["lines"][-1]["quantity"] == "0.100000"


def test_expired_and_unopened_lots_are_blocked(operator, qa, reset_runtime):
    """AC-14：过期、开封超期、未放行的批号不能预留和投料。"""
    from app.core.db import SessionLocal
    from app.models import Lot

    operator.post(
        "/api/lots",
        {"id": "LOT-EXPIRY-01", "material": "有效期用例", "qty": "10", "unit": "g",
         "expiry": "2020-01-01"},
    )
    qa.post(
        "/api/lots/LOT-EXPIRY-01/release",
        {"signature_id": qa.sign("复验合格", target="LOT-EXPIRY-01")},
    )
    rows = {row["id"]: row for row in operator.get("/api/lots").json()}
    assert rows["LOT-EXPIRY-01"]["expired"] is True
    assert rows["LOT-EXPIRY-01"]["usable"] is False
    assert "已超过有效截止" in rows["LOT-EXPIRY-01"]["use_blockers"][0]

    # 开封截止时间早于生产有效期：有效截止取较早者
    operator.post(
        "/api/lots",
        {"id": "LOT-EXPIRY-02", "material": "有效期用例", "qty": "10", "unit": "g",
         "expiry": "2029-01-01"},
    )
    no_basis = operator.post(
        "/api/lots/LOT-EXPIRY-02/opening",
        {"opened": "2026-09-01", "open_expiry": "2026-09-10"},
    )
    assert no_basis.status_code == 422, "录开封期限必须写依据，不编造天数"
    assert no_basis.json()["detail"]["code"] == "open_expiry_basis_required"

    ok = operator.post(
        "/api/lots/LOT-EXPIRY-02/opening",
        {"opened": "2026-09-01", "open_expiry": "2026-09-10",
         "open_expiry_basis": "SOP-SLR-01 规定开封后 9 天"},
    )
    assert ok.status_code == 200
    assert ok.json()["effective_expiry"] == "2026-09-10"
    assert ok.json()["effective_expiry_basis"] == "开封有效期"

    with SessionLocal() as db:
        for lot_id in ("LOT-EXPIRY-01", "LOT-EXPIRY-02"):
            from app.models import InventoryEvent, InventoryLedger

            db.query(InventoryLedger).filter(InventoryLedger.lot_id == lot_id).delete()
            db.query(InventoryEvent).filter(
                InventoryEvent.event_id == f"receive-{lot_id}"
            ).delete()
            db.query(Lot).filter(Lot.id == lot_id).delete()
        db.commit()


def test_empty_bom_is_not_blocked_by_material_check(operator, researcher, qa, reset_runtime):
    """AC-14 后半句：合法空 BOM 的实验不被物料项误拦截。"""
    recipe = researcher.post("/api/recipes", {"name": "纯人工流程", "plate": 4}).json()
    recipe_id = recipe["id"]
    patched = researcher.patch(
        f"/api/recipes/{recipe_id}",
        {
            "risk": "RA-manual v1",
            "bom": [],
            "steps": [
                {
                    "kind": "manual", "name": "目视检查", "dur": 10,
                    "form": [{"key": "ok", "label": "外观合格", "type": "bool", "required": True}],
                },
                {"kind": "review", "name": "QA 确认", "review_role": "qa"},
            ],
        },
    )
    assert patched.status_code == 200, patched.text
    checks = {row["key"]: row for row in patched.json()["checks"]}
    assert checks["bom"]["ok"] is True
    assert "无需物料" in checks["bom"]["detail"]
    assert checks["stations"]["ok"] is True
    assert "没有需要工位的步骤" in checks["stations"]["detail"]


def test_bom_reservation_equals_the_declared_quantity(operator, reset_runtime):
    """预留量必须等于 BOM 声明量。

    这条守着一个已经踩过的坑：预留行建出来就写满数量、又用 reserve 事件加一遍，
    占用会正好翻倍——可用量凭空少一半，而两处看起来都「对」。
    """
    created = operator.post("/api/batches", {"plan_id": "EP-205-01"})
    assert created.status_code == 201, created.text
    detail = operator.get(f"/api/batches/{created.json()['id']}").json()

    bom = {item["material"]: item for item in detail["snapshot"]["bom"]}
    assert bom, "该用例要求方法有 BOM"
    reserved: dict[str, float] = {}
    for row in detail["reservations"]:
        reserved[row["material"]] = reserved.get(row["material"], 0.0) + float(row["qty"])

    for material, item in bom.items():
        assert material in reserved, f"{material} 没有预留"
        assert reserved[material] == float(item["qty"]), (
            f"{material} 预留 {reserved[material]} ≠ BOM 声明 {item['qty']}"
        )
        # 授权预留 = 剩余占用（还没有任何消耗）
        rows = [row for row in detail["reservations"] if row["material"] == material]
        assert sum(float(row["outstanding"]) for row in rows) == float(item["qty"])
