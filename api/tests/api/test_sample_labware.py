"""样本 ↔ 载具真实关联、结构化位置与标签二维码。"""
import uuid

from test_labware_transfer import _register, clean_labware  # noqa: F401  （夹具复用）


def _batch(operator):
    created = operator.post("/api/batches", {"plan_id": "EP-205-01"})
    assert created.status_code == 201, created.text
    return created.json()["id"]


def test_binding_labware_points_samples_to_physical_wells(operator, clean_labware):  # noqa: F811
    batch_id = _batch(operator)
    labware = _register(operator, "HOTEL-01/S01")
    bound = operator.post(f"/api/batches/{batch_id}/labware", {"labware_id": labware["id"]})
    assert bound.status_code == 200, bound.text

    on_tray = operator.get(f"/api/labware/{labware['barcode']}/samples").json()
    wells = [row["well"] for row in on_tray["samples"]]
    assert wells == [f"A{n}" for n in range(1, 9)], "2×4 的布局按顺序放进 1×8 托盘"
    sample = operator.get(f"/api/samples/{on_tray['samples'][0]['id']}").json()
    location = sample["location"]
    assert location["kind"] == "labware" and location["labware"]["barcode"] == labware["barcode"]
    assert location["well"] == "A1" and location["place"]["id"] == "HOTEL-01/S01"
    assert labware["barcode"] in location["text"]

    moved = operator.post(
        f"/api/samples/{sample['id']}/transfers",
        {"event_key": f"mv-{uuid.uuid4().hex[:6]}", "kind": "store", "to_location_id": "HOTEL-01/S02"},
    )
    assert moved.status_code == 409 and moved.json()["detail"]["code"] == "sample_on_labware"

    assert operator.delete(f"/api/batches/{batch_id}/labware").status_code == 200
    after = operator.get(f"/api/samples/{sample['id']}").json()["location"]
    assert after["kind"] != "labware"
    assert operator.get(f"/api/labware/{labware['id']}/samples").json()["samples"] == []


def test_store_to_a_registered_location_is_structured(operator, admin):
    sample_id = f"PS-LOC-{uuid.uuid4().hex[:6]}"
    barcode = f"BC-{uuid.uuid4().hex[:8]}"
    created = operator.post("/api/samples", {"id": sample_id, "barcode": barcode, "source": "位置用例",
                                             "sample_type": "极片", "quantity": "1", "unit": "g"})
    assert created.status_code == 201, created.text
    shelf = f"FRIDGE-{uuid.uuid4().hex[:4]}"
    assert admin.post("/api/locations", {"id": shelf, "name": "冷藏柜 2 层", "kind": "storage"}).status_code == 201
    stored = operator.post(
        f"/api/samples/{barcode}/transfers",
        {"event_key": f"st-{uuid.uuid4().hex[:6]}", "kind": "store", "to_location_id": shelf},
    )
    assert stored.status_code == 200, stored.text
    location = stored.json()["location"]
    assert location["kind"] == "location" and location["place"]["name"] == "冷藏柜 2 层"
    assert stored.json()["current_location"] == "冷藏柜 2 层"

    qr = operator.get(f"/api/samples/{barcode}/qr").json()
    assert qr["content"] == barcode and qr["svg"].lstrip().startswith("<svg")
    assert operator.get(f"/api/samples/{barcode}").json()["id"] == sample_id, "扫码（条码）直接查到样本"


def test_ending_the_batch_takes_samples_off_the_labware(operator, clean_labware):  # noqa: F811
    batch_id = _batch(operator)
    labware = _register(operator, "HOTEL-01/S01")
    assert operator.post(f"/api/batches/{batch_id}/labware", {"labware_id": labware["id"]}).status_code == 200
    sample_id = operator.get(f"/api/labware/{labware['id']}/samples").json()["samples"][0]["id"]
    aborted = operator.post(f"/api/batches/{batch_id}/abort", {"reason": "用例结束", "signature_id": operator.sign("终止批次", target=batch_id)})
    assert aborted.status_code == 200, aborted.text
    location = operator.get(f"/api/samples/{sample_id}").json()["location"]
    assert location["kind"] == "text" and labware["barcode"] in location["text"], "解开后保留最后所在的载具孔位"
