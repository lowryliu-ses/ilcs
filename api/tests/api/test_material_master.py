"""物料主数据的维护：改类别与换算、有批号时锁名称与单位、同名同单位不重复、停用与恢复。"""
import uuid


def _material(actor, **extra):
    code = f"MM-{uuid.uuid4().hex[:8]}"
    body = {"code": code, "name": f"测试物料 {code}", "base_unit": "g", "category": "溶剂", **extra}
    response = actor.post("/api/materials", body)
    assert response.status_code == 201, response.text
    return response.json()


def _receive(actor, material, **extra):
    lot_id = f"LOT-{uuid.uuid4().hex[:8]}"
    body = {"id": lot_id, "material": material["name"], "material_id": material["id"], "qty": "100",
            "unit": material["base_unit"], "expiry": "2030-12-31", **extra}
    return actor.post("/api/lots", body)


def test_edit_category_conversions_and_version(operator, researcher):
    material = _material(operator)
    assert material["row_version"] == 1 and material["locked_fields"] == []

    changed = operator.patch(f"/api/materials/{material['id']}", {
        "category": "预热溶剂", "cas": "96-49-1", "conversions": {"ml": "1.32", "kg": 1000},
        "row_version": material["row_version"],
    })
    assert changed.status_code == 200, changed.text
    body = changed.json()
    assert body["category"] == "预热溶剂" and body["cas"] == "96-49-1"
    # 单位按规范写法存：ml → mL；系数是十进制文字
    assert body["conversions"] == {"mL": "1.32", "kg": "1000"}
    assert body["row_version"] == 2 and body["updated_at"]

    # 拿旧版本号提交：别人改过了，409 不静默覆盖
    stale = operator.patch(f"/api/materials/{material['id']}", {"category": "溶剂", "row_version": 1})
    assert stale.status_code == 409

    # 没有物料维护权限的角色不能改
    assert researcher.patch(f"/api/materials/{material['id']}", {"category": "溶剂"}).status_code == 403

    audit = operator.get(f"/api/audit?target={material['id']}").json()
    assert any(row["action"] == "修改物料主数据" and "预热溶剂" in row["after"] for row in audit), audit


def test_conversions_are_validated(operator):
    material = _material(operator)
    for conversions, word in (
        ({"mL": "0"}, "正数"), ({"mL": "-1"}, "正数"), ({"mL": "abc"}, "正数"), ({"g": "1"}, "就是基础单位"),
        ({"": "1"}, "不能为空"),
    ):
        response = operator.patch(f"/api/materials/{material['id']}", {"conversions": conversions})
        assert response.status_code == 422, (conversions, response.text)
        assert word in response.json()["detail"]["message"], response.text


def test_name_and_unit_lock_once_lots_exist(operator):
    material = _material(operator)
    # 还没有批号：名称、基础单位都能改
    renamed = operator.patch(f"/api/materials/{material['id']}", {"name": material["name"] + " 改", "base_unit": "kg"})
    assert renamed.status_code == 200, renamed.text
    material = renamed.json()
    assert material["base_unit"] == "kg"

    assert _receive(operator, material).status_code == 201
    listed = next(row for row in operator.get("/api/materials").json() if row["id"] == material["id"])
    assert listed["lot_count"] == 1 and listed["locked_fields"] == ["name", "base_unit"]

    for change in ({"name": "别的名字"}, {"base_unit": "g"}):
        response = operator.patch(f"/api/materials/{material['id']}", change)
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "material_locked"
    # 原样提交名称不算改名；类别照样能改
    same = operator.patch(f"/api/materials/{material['id']}", {"name": material["name"], "category": "锂盐"})
    assert same.status_code == 200 and same.json()["category"] == "锂盐"


def test_same_name_and_unit_is_rejected(operator):
    first = _material(operator)
    duplicate = operator.post("/api/materials", {"code": f"MM-{uuid.uuid4().hex[:8]}", "name": first["name"],
                                                 "base_unit": "g"})
    assert duplicate.status_code == 409 and duplicate.json()["detail"]["code"] == "material_duplicate"
    # 同名不同单位是两种登记，可以
    other_unit = operator.post("/api/materials", {"code": f"MM-{uuid.uuid4().hex[:8]}", "name": first["name"],
                                                  "base_unit": "mL"})
    assert other_unit.status_code == 201, other_unit.text

    second = _material(operator)
    clash = operator.patch(f"/api/materials/{second['id']}", {"name": first["name"]})
    assert clash.status_code == 409 and clash.json()["detail"]["code"] == "material_duplicate"


def test_retired_material_takes_no_new_lots_until_restored(operator):
    material = _material(operator)
    assert _receive(operator, material).status_code == 201

    retired = operator.post(f"/api/materials/{material['id']}/retire", {"row_version": material["row_version"]})
    assert retired.status_code == 200, retired.text
    assert retired.json()["state"] == "retired"
    assert operator.post(f"/api/materials/{material['id']}/retire", {}).status_code == 409

    # 指定主数据入库、只写名称与单位入库，都拒绝
    by_id = _receive(operator, material)
    assert by_id.status_code == 409 and by_id.json()["detail"]["code"] == "material_retired"
    by_name = _receive(operator, {**material, "id": ""})
    assert by_name.status_code == 409 and by_name.json()["detail"]["code"] == "material_retired"

    restored = operator.post(f"/api/materials/{material['id']}/restore", {"row_version": retired.json()["row_version"]})
    assert restored.status_code == 200 and restored.json()["state"] == "active"
    assert _receive(operator, material).status_code == 201


def test_restore_refuses_when_an_active_twin_exists(operator):
    material = _material(operator)
    retired = operator.post(f"/api/materials/{material['id']}/retire", {}).json()
    # 停用期间登记了一条同名同单位的新物料：恢复旧的会让入库分不清入到哪条
    twin = operator.post("/api/materials", {"code": f"MM-{uuid.uuid4().hex[:8]}", "name": material["name"],
                                            "base_unit": "g"})
    assert twin.status_code == 201, twin.text
    blocked = operator.post(f"/api/materials/{material['id']}/restore", {"row_version": retired["row_version"]})
    assert blocked.status_code == 409 and blocked.json()["detail"]["code"] == "material_duplicate"
    # 按名称与单位入库，入到在用的那条
    lot = _receive(operator, {**material, "id": ""})
    assert lot.status_code == 201, lot.text
    assert lot.json()["material_id"] == twin.json()["id"]
