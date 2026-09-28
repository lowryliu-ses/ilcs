"""工位的实物属性以资产为准：型号、校准只在资产上登记一份，工位台账从资产带出、只读。

- 设备方法按工位关联资产登记的型号匹配；工位上早先登记、与资产不一致的型号只作提示，不参与匹配；
- 关联了资产的工位不能从台账接口改型号，只能清掉旧登记；改资产型号会重校验受影响的流程；
- 校准到期、样品位不再是工位字段；
- 现场监控带清洗状态、行版本与适配器状态，现场操作在那一页做。
"""
import copy
from datetime import datetime

from app.core.context import system_context
from app.domain.capability import out_of_range
from app.models import Asset, Recipe, Station
from app.repositories.resources import StationRepository
from test_schema_guard import scratch_database  # noqa: F401

METHOD = {
    "name": "120℃ 真空干燥（型号匹配）", "capability_id": "cap.vacuum_dry", "instrument_models": ["VAC-WEIGH-12"],
    "program": "VD-120",
    "params": {"temp": {"default": 120, "min": 100, "max": 130, "unit": "℃"},
               "vacuum": {"default": 1, "min": 0.5, "max": 5, "unit": "mbar"}},
    "dur_min": 60,
}


def _row(session, station_id: str) -> dict:
    return next(row for row in session.get("/api/stations").json() if row["id"] == station_id)


def test_station_ledger_reads_model_and_calibration_from_the_asset(admin, reset_runtime):
    row = _row(admin, "ST-07")
    assert row["model"] == "CYCLER-32" and row["model_source"] == "asset" and row["model_conflict"] == ""
    asset = row["asset"]
    assert asset["asset_no"] == "AS-0008" and asset["capacity"] >= row["channels"]
    # 校准结论与到期日从资产的校准记录算出来，工位上不再另存一份
    assert asset["calibration_applicable"] is True and isinstance(asset["calibration_valid"], bool)
    assert "calibration_due" in asset and "unavailable_reasons" in asset
    assert "cal_due" not in row and "positions" not in row

    agv = _row(admin, "AGV-01")
    assert agv["asset"] is None and agv["model"] == "MiR-250" and agv["model_source"] == "station"


def test_method_matches_the_asset_model_and_the_ledger_cannot_fork_it(admin, researcher, qa, db, reset_runtime):
    created = researcher.post("/api/device-methods", METHOD)
    assert created.status_code == 201, created.text
    method = qa.post(
        f"/api/device-methods/{created.json()['id']}/release", {"row_version": created.json()["row_version"]},
    ).json()

    recipe = db.get(Recipe, "R-205")
    original = copy.deepcopy(recipe.steps)
    station = db.get(Station, "ST-05")
    asset = db.get(Asset, station.asset_id)
    asset_id, asset_model = asset.id, asset.model
    try:
        steps = copy.deepcopy(original)
        steps[0] = {**steps[0], "params": {"temp": 110}, "method": {"id": method["id"]}}
        recipe.steps = steps
        # 工位上残留一个和资产不一致的旧型号：匹配照样按资产型号，界面提示核对
        station.model = "LEGACY-MODEL"
        db.commit()

        first = researcher.get("/api/recipes/R-205").json()["validation"][0]
        assert first["ok"] and first["fits"] == ["ST-05"], first
        row = _row(admin, "ST-05")
        assert row["model"] == "VAC-WEIGH-12" and row["model_conflict"] == "LEGACY-MODEL"

        # 台账接口不能把型号改成别的；只能清掉旧登记（「以资产型号为准」）
        forked = admin.patch("/api/stations/ST-05", {"model": "SOMETHING-ELSE", "row_version": row["row_version"]})
        assert forked.status_code == 409 and forked.json()["detail"]["code"] == "model_owned_by_asset"
        cleared = admin.patch("/api/stations/ST-05", {"model": "", "row_version": row["row_version"]})
        assert cleared.status_code == 200, cleared.text
        assert _row(admin, "ST-05")["model_conflict"] == ""

        # 改资产型号：方法不再适用这台工位，引用它的已发布流程重校验后进入需修订
        current = admin.get(f"/api/assets/{asset_id}").json()
        changed = admin.patch(f"/api/assets/{asset_id}", {"model": "VAC-80", "row_version": current["row_version"]})
        assert changed.status_code == 200, changed.text
        assert "R-205" in changed.json()["broken_recipes"]
        first = researcher.get("/api/recipes/R-205").json()["validation"][0]
        assert not first["ok"] and first["fits"] == []
        db.expire_all()
        spec = next(row for row in StationRepository(db, system_context("ORG-001")).specs() if row.id == "ST-05")
        assert spec.model == "VAC-80", "匹配用的是资产型号"
        step = {"cap": "cap.vacuum_dry", "params": {"temp": 110, "vacuum": 1},
                "method": {"id": method["id"], "instrument_models": ["VAC-WEIGH-12"], "program": "VD-120"}}
        assert out_of_range(spec, step) == ["ST-05 型号 VAC-80 不在方法适用型号 VAC-WEIGH-12 内"]
    finally:
        db.expire_all()
        recipe = db.get(Recipe, "R-205")
        recipe.steps = original
        db.commit()
        current = admin.get(f"/api/assets/{asset_id}").json()
        if current["model"] != asset_model:
            restored = admin.patch(f"/api/assets/{asset_id}", {"model": asset_model, "row_version": current["row_version"]})
            assert restored.status_code == 200, restored.text
            assert "R-205" not in restored.json()["broken_recipes"]


def test_linking_moves_the_model_onto_the_asset(admin, reset_runtime):
    for station_id, model in (("ST-LNK-1", "SONIC-2"), ("ST-LNK-2", "SONIC-3")):
        response = admin.post("/api/stations", {
            "id": station_id, "name": f"关联测试 {station_id}", "model": model, "limits": {},
            "signature_id": admin.sign("工程变更批准", target=station_id),
        })
        assert response.status_code == 201, response.text
    blank = admin.post("/api/assets", {"asset_no": "AS-LINK-1", "name": "未登记型号的资产"}).json()
    other = admin.post("/api/assets", {"asset_no": "AS-LINK-2", "name": "另一型号的资产", "model": "SONIC-9"}).json()
    try:
        # 资产没登记型号：按工位原来登记的补上，工位上不再另存
        linked = admin.post(f"/api/assets/{blank['id']}/stations", {"station_id": "ST-LNK-1"})
        assert linked.status_code == 200, linked.text
        assert linked.json()["model"] == "SONIC-2" and linked.json()["warning"] == ""
        row = _row(admin, "ST-LNK-1")
        assert row["model"] == "SONIC-2" and row["model_source"] == "asset" and row["model_conflict"] == ""

        # 两边不一致：不替人判断，按资产型号匹配，提示核对
        linked = admin.post(f"/api/assets/{other['id']}/stations", {"station_id": "ST-LNK-2"})
        assert linked.status_code == 200, linked.text
        assert "不一致" in linked.json()["warning"]
        row = _row(admin, "ST-LNK-2")
        assert row["model"] == "SONIC-9" and row["model_conflict"] == "SONIC-3"

        # 资产改成工位上的旧型号：旧登记与资产一致了，顺手清掉
        current = admin.get(f"/api/assets/{other['id']}").json()
        assert admin.patch(
            f"/api/assets/{other['id']}", {"model": "SONIC-3", "row_version": current["row_version"]},
        ).status_code == 200
        row = _row(admin, "ST-LNK-2")
        assert row["model"] == "SONIC-3" and row["model_conflict"] == ""

        # 从台账接口取消关联：型号抄回工位，工位不会因此没了型号；再关联回去又归资产
        assert admin.patch("/api/stations/ST-LNK-2", {"asset_id": "", "row_version": row["row_version"]}).status_code == 200
        row = _row(admin, "ST-LNK-2")
        assert row["model"] == "SONIC-3" and row["model_source"] == "station" and row["asset"] is None
        assert admin.patch(
            "/api/stations/ST-LNK-2", {"asset_id": other["id"], "row_version": row["row_version"]},
        ).status_code == 200
        row = _row(admin, "ST-LNK-2")
        assert row["model"] == "SONIC-3" and row["model_source"] == "asset" and row["model_conflict"] == ""
    finally:
        for station_id in ("ST-LNK-1", "ST-LNK-2"):
            admin.post(f"/api/stations/{station_id}/retire", {"retired": True})


def test_floor_carries_what_the_operator_acts_on(operator, reset_runtime):
    def card(station_id: str) -> dict:
        return next(row for row in operator.get("/api/floor").json()["stations"] if row["id"] == station_id)

    row = card("ST-05")
    assert row["mine"] is True and row["clean"] is True and row["row_version"] > 0
    assert row["adapter"]["status"] in {"online", "degraded", "stale", "offline", "disabled"}

    marked = operator.patch(
        "/api/stations/ST-05/readiness", {"clean": False, "status": "idle", "row_version": row["row_version"]},
    )
    assert marked.status_code == 200, marked.text
    row = card("ST-05")
    assert row["clean"] is False
    confirmed = operator.patch(
        "/api/stations/ST-05/readiness", {"clean": True, "status": "idle", "row_version": row["row_version"]},
    )
    assert confirmed.status_code == 200, confirmed.text
    assert card("ST-05")["clean"] is True


def _alembic(scratch_database: str, *args: str) -> None:
    import os
    import subprocess

    from conftest import API_DIR

    result = subprocess.run(
        [str(API_DIR / ".venv" / "bin" / "alembic"), *args],
        cwd=API_DIR, capture_output=True, text=True,
        env={**os.environ, "ILCS_DATABASE_URL": scratch_database},
    )
    assert result.returncode == 0, result.stderr


def _insert(connection, metadata, table_name: str, **values) -> None:
    """按当时的表结构插一行：没给的非空列按类型填空值（旧结构里这些列没有服务端缺省）。"""
    from datetime import datetime

    import sqlalchemy as sa

    table = metadata.tables[table_name]
    row = dict(values)
    for column in table.columns:
        if column.name in row or column.nullable or column.server_default is not None:
            continue
        kind = column.type
        row[column.name] = (
            False if isinstance(kind, sa.Boolean)
            else 1 if isinstance(kind, sa.Integer)
            else 0.0 if isinstance(kind, sa.Float)
            else datetime(2026, 9, 1) if isinstance(kind, sa.DateTime)
            else {} if isinstance(kind, sa.JSON)
            else ""
        )
    connection.execute(table.insert().values(row))


def test_upgrade_keeps_one_model_on_the_asset_and_drops_the_station_copies(scratch_database):
    """0036：资产型号为空用工位的回填、一致的工位型号清空、不一致的两边都不动；删校准到期与样品位。可回退。"""
    from sqlalchemy import MetaData, create_engine, inspect, text

    _alembic(scratch_database, "upgrade", "0035_sop_step_keys")
    engine = create_engine(scratch_database)
    try:
        metadata = MetaData()
        metadata.reflect(bind=engine, only=["organizations", "assets", "stations", "calibration_records"])
        with engine.begin() as connection:
            _insert(connection, metadata, "organizations", id="ORG-M", name="迁移测试")
            for asset_id, model in (("A-BLANK", ""), ("A-SAME", "SAME-1"), ("A-DIFF", "ASSET-M")):
                _insert(connection, metadata, "assets", id=asset_id, org_id="ORG-M", asset_no=asset_id, name=asset_id,
                        model=model, capacity=1, row_version=1)
            _insert(connection, metadata, "calibration_records", id="CAL-1", org_id="ORG-M", asset_id="A-SAME",
                    result="pass", capability_scope=[], expires_at=datetime(2027, 5, 1))
            for station_id, asset_id, model in (
                ("S-BLANK", "A-BLANK", "STATION-1"), ("S-SAME", "A-SAME", "SAME-1"),
                ("S-DIFF", "A-DIFF", "STATION-3"), ("S-AGV", "", "AGV-M"),
            ):
                _insert(connection, metadata, "stations", id=station_id, org_id="ORG-M", asset_id=asset_id, island=1,
                        name=station_id, model=model, status="idle", cal_due="2026-12-31", positions=24,
                        clean=True, limits={}, retired=False, channels=1, row_version=1)

        _alembic(scratch_database, "upgrade", "head")
        with engine.connect() as connection:
            assets = dict(connection.execute(text("SELECT id, model FROM assets")).all())
            stations = dict(connection.execute(text("SELECT id, model FROM stations")).all())
        assert assets == {"A-BLANK": "STATION-1", "A-SAME": "SAME-1", "A-DIFF": "ASSET-M"}
        assert stations == {"S-BLANK": "", "S-SAME": "", "S-DIFF": "STATION-3", "S-AGV": "AGV-M"}
        columns = {column["name"] for column in inspect(engine).get_columns("stations")}
        assert not {"cal_due", "positions"} & columns

        # 回退：旧代码按工位型号匹配方法，型号从资产抄回；校准到期按资产最近一条合格校准回填
        _alembic(scratch_database, "downgrade", "0035_sop_step_keys")
        with engine.connect() as connection:
            rows = {row.id: row for row in connection.execute(text("SELECT id, model, cal_due, positions FROM stations"))}
        assert {key: row.model for key, row in rows.items()} == {
            "S-BLANK": "STATION-1", "S-SAME": "SAME-1", "S-DIFF": "STATION-3", "S-AGV": "AGV-M",
        }
        assert rows["S-SAME"].cal_due == "2027-05-01" and rows["S-BLANK"].cal_due == ""
        assert all(row.positions == 1 for row in rows.values())
    finally:
        engine.dispose()
