"""AC-18 至 AC-20：样本登记、分样、孔位占用与交接。"""
import pytest


def test_sample_can_be_registered_without_a_batch_or_result(operator, reset_runtime):
    """AC-18 前半句：登记样本不要求已有批次，也不要求已有检测结果。"""
    created = operator.post(
        "/api/samples",
        {
            "id": "PS-STANDALONE-1", "barcode": "BC-STANDALONE-1", "source": "外部来料",
            "sample_type": "极片", "quantity": "10", "unit": "g",
            "storage_condition": "干燥柜", "current_location": "收样间",
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["lifecycle_state"] == "registered"
    assert created.json()["assignment_count"] == 0

    detail = operator.get("/api/samples/PS-STANDALONE-1").json()
    assert detail["analysis_tasks"] == []
    assert detail["assignments"] == []


def test_split_checks_parent_quantity_and_records_loss(operator, reset_runtime):
    """AC-18 后半句：父子数量可核对；超量分样被拒。"""
    operator.post(
        "/api/samples",
        {"id": "PS-SPLIT-1", "barcode": "BC-SPLIT-1", "quantity": "10", "unit": "g",
         "sample_type": "极片"},
    )
    over = operator.post(
        "/api/samples/PS-SPLIT-1/split",
        {"event_key": "split-over", "children": [{"quantity": "6"}, {"quantity": "6"}]},
    )
    assert over.status_code == 409
    assert over.json()["detail"]["code"] == "split_over_quantity"

    no_reason = operator.post(
        "/api/samples/PS-SPLIT-1/split",
        {"event_key": "split-no-reason", "children": [{"quantity": "4"}], "loss": "1"},
    )
    assert no_reason.status_code == 422
    assert no_reason.json()["detail"]["code"] == "loss_reason_required"

    ok = operator.post(
        "/api/samples/PS-SPLIT-1/split",
        {
            "event_key": "split-ok",
            "children": [{"quantity": "4"}, {"quantity": "3"}],
            "loss": "0.5", "loss_reason": "转移残留",
        },
    )
    assert ok.status_code == 201, ok.text
    assert ok.json()["parent"]["quantity"] == "2.500000"
    assert [row["quantity"] for row in ok.json()["children"]] == ["4.000000", "3.000000"]

    detail = operator.get("/api/samples/PS-SPLIT-1").json()
    assert len(detail["children"]) == 2
    split_transfer = next(row for row in detail["transfers"] if row["kind"] == "split")
    assert "损耗 0.500000" in split_transfer["note"]

    child = operator.get(f"/api/samples/{ok.json()['children'][0]['id']}").json()
    assert child["lineage"][0]["id"] == "PS-SPLIT-1", "子样可追溯到母样"


def test_repeated_receive_scan_does_not_write_twice(operator, reset_runtime):
    """AC-19 后半句：重复扫码不多写交接；真实的新交接可以记录。"""
    operator.post("/api/samples", {"id": "PS-SCAN-1", "barcode": "BC-SCAN-1", "sample_type": "极片"})
    payload = {"event_key": "scan-1", "to_location": "备料间", "to_party": "操作员"}
    first = operator.post("/api/samples/PS-SCAN-1/receive", payload)
    assert first.status_code == 200 and first.json()["replayed"] is False
    replay = operator.post("/api/samples/PS-SCAN-1/receive", payload)
    assert replay.status_code == 200 and replay.json()["replayed"] is True

    detail = operator.get("/api/samples/PS-SCAN-1").json()
    assert len([row for row in detail["transfers"] if row["kind"] == "receive"]) == 1
    assert detail["lifecycle_state"] == "received"

    moved = operator.post(
        "/api/samples/PS-SCAN-1/transfers",
        {"event_key": "scan-2", "kind": "handover", "to_location": "涂布间",
         "to_party": "涂布操作员"},
    )
    assert moved.status_code == 200 and moved.json()["replayed"] is False
    after = operator.get("/api/samples/PS-SCAN-1").json()
    assert len(after["transfers"]) == 2
    assert after["current_location"] == "涂布间"
    # 位置历史保留时间线，不是覆盖一个字段
    assert [row["to_location"] for row in after["transfers"]] == ["备料间", "涂布间"]


def test_two_samples_cannot_occupy_the_same_live_slot(operator, reset_runtime):
    """AC-19 前半句：一个在途孔位同时只能分配给一个样本。"""
    from app.core.context import AccessContext
    from app.core.db import SessionLocal
    from app.services.sample_service import SampleService

    operator.post("/api/samples", {"id": "PS-SLOT-A", "barcode": "BC-SLOT-A"})
    operator.post("/api/samples", {"id": "PS-SLOT-B", "barcode": "BC-SLOT-B"})
    ctx = AccessContext(org_id="ORG-001", subject_id="test", role="operator")

    with SessionLocal() as db:
        service = SampleService(db, ctx)
        service.occupy_slot("PL-TEST", "A1", "PS-SLOT-A")
        db.commit()

    with SessionLocal() as db:
        service = SampleService(db, ctx)
        with pytest.raises(Exception) as caught:
            service.occupy_slot("PL-TEST", "A1", "PS-SLOT-B")
        assert "已被在途样本" in str(caught.value)

    with SessionLocal() as db:
        service = SampleService(db, ctx)
        assert service.release_slots("PL-TEST") == 1
        service.occupy_slot("PL-TEST", "A1", "PS-SLOT-B")
        db.commit()


def test_deleting_a_batch_keeps_independently_registered_samples(
    operator, researcher, qa, reset_runtime
):
    """AC-20：删除未执行批次时，独立登记或已流转的样本不能被级联删除。"""
    sample_ids = ["PS-KEEP-1", "PS-KEEP-2"]
    for sample_id in sample_ids:
        assert operator.post(
            "/api/samples",
            {"id": sample_id, "barcode": f"BC-{sample_id}", "sample_type": "极片",
             "quantity": "5", "unit": "g"},
        ).status_code == 201
    # 其中一个已经有流转历史
    assert operator.post(
        f"/api/samples/{sample_ids[0]}/receive",
        {"event_key": f"recv-{sample_ids[0]}", "to_location": "备料间"},
    ).status_code == 200

    plan = researcher.post(
        "/api/plans",
        {
            "name": "样本删除用例", "recipe_id": "R-205", "plan_type": "commissioned_test",
            "sample_ids": sample_ids, "required_metrics": ["METRIC-discharge_capacity-v1"],
        },
    ).json()
    plan_id = plan["id"]
    assert researcher.post(f"/api/plans/{plan_id}/lock").status_code == 200
    assert researcher.post(f"/api/plans/{plan_id}/submit").status_code == 200
    current = researcher.get(f"/api/plans/{plan_id}").json()
    assert qa.post(
        f"/api/plans/{plan_id}/decision",
        {"conclusion": "approved",
         "signature_id": qa.sign("批准方案", target=plan_id,
                                 object_version=current["row_version"])},
    ).status_code == 200

    batch = operator.post("/api/batches", {"plan_id": plan_id})
    assert batch.status_code == 201, batch.text
    batch_id = batch.json()["id"]
    assert operator.get(f"/api/batches/{batch_id}").json()["sample_count"] == 2

    deleted = operator.delete(f"/api/batches/{batch_id}")
    assert deleted.status_code == 200, deleted.text
    assert set(deleted.json()["kept_samples"]) == set(sample_ids), (
        "方案引用的已登记样本不能被批次删除带走"
    )
    for sample_id in sample_ids:
        assert operator.get(f"/api/samples/{sample_id}").status_code == 200
