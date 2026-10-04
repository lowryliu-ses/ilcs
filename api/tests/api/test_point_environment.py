"""设备点位 → 环境读数：只读写点位的传感器（温湿度计、压力表）接进来以后，执行器探测在线时按采集周期读登记的点，
记成环境读数，开跑检查与下发前按它核对步骤的环境要求。"""
from datetime import timedelta

STATION = "ST-05"
ENVIRONMENT = {"zone": "联调区", "interval_sec": 30, "points": {"temperature": "temp", "humidity": "rh", "o2_ppm": "o2"}}


def test_environment_is_a_registered_config_item_and_a_light_change():
    from app.adapters.catalog import validate_config
    from app.domain.adapter_rules import busy_blocked_changes

    config = {"host": "127.0.0.1", "port": 50202, "insecure": True, "tasks": False}
    check = validate_config("sila2_v1", {**config, "environment": ENVIRONMENT})
    assert check.ok and not [w for w in check.warnings if "environment" in w], check.warnings
    assert busy_blocked_changes({"config": config}, {"config": {**config, "environment": ENVIRONMENT}}) == [], \
        "改环境采集不改连谁、怎么判结论：不用重新握手"


def test_probing_records_point_readings_as_environment_readings(reset_runtime, monkeypatch, operator):
    from app.core.clock import now
    from app.core.db import SessionLocal
    from app.models import Adapter, EnvironmentReading
    from app.services import execution_service
    from app.services.execution_service import ExecutorLoop

    reads: list[list[str]] = []

    class Sensor:
        def healthcheck(self):
            return {"device_id": "SENSOR-1", "interlock": False, "accepts_commands": True}

        def read_points(self, names=None):
            reads.append(list(names or []))
            return [{"name": "temp", "value": 23.5, "unit": "℃", "error": ""},
                    {"name": "rh", "value": 41, "unit": "%RH", "error": ""},
                    {"name": "o2", "value": None, "unit": "ppm", "error": "读 o2 无结论：TimeoutError"}]

    monkeypatch.setattr(execution_service, "adapter_for", lambda record: Sensor())
    source = f"device:{STATION}"
    with SessionLocal() as db:
        adapter = db.get(Adapter, STATION)
        saved = (adapter.kind, adapter.driver, adapter.config)
        adapter.kind, adapter.driver = "real", "sila2_v1"
        adapter.config = {"host": "127.0.0.1", "port": 50202, "tasks": False, "environment": ENVIRONMENT}
        adapter.connected, adapter.last_heartbeat = False, now() - timedelta(minutes=1)
        db.commit()
        try:
            loop = ExecutorLoop(db)
            loop.probe_devices(STATION)
            db.commit()
            rows = db.query(EnvironmentReading).filter(EnvironmentReading.source == source).all()
            assert {(row.zone, row.metric, row.value, row.unit) for row in rows} == {
                ("联调区", "temperature", 23.5, "℃"), ("联调区", "humidity", 41.0, "%RH")}, "读不到的 o2 不记"
            assert reads == [["o2", "rh", "temp"]]

            adapter.connected = False  # 下一轮还会探测：没到采集周期就不再读、不再记
            db.commit()
            loop.probe_devices(STATION)
            db.commit()
            assert len(reads) == 1
            assert db.query(EnvironmentReading).filter(EnvironmentReading.source == source).count() == 2

            for row in db.query(EnvironmentReading).filter(EnvironmentReading.source == source).all():
                row.measured_at = row.measured_at - timedelta(seconds=31)
            adapter.connected = False
            db.commit()
            loop.probe_devices(STATION)
            db.commit()
            assert len(reads) == 2 and db.query(EnvironmentReading).filter(EnvironmentReading.source == source).count() == 4
        finally:
            adapter.kind, adapter.driver, adapter.config = saved
            db.query(EnvironmentReading).filter(EnvironmentReading.source == source).delete()
            db.commit()

    latest = operator.get("/api/environment/readings")
    assert latest.status_code == 200, latest.text
