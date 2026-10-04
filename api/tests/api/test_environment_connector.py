"""环境读数连接器：模拟手套箱（Modbus TCP）→ 连接器 → `POST /api/runtime/environment` → 步骤的水氧要求按最新读数核对。
走真实 API（TestClient）与真实 Modbus 收发；服务身份按区域授权。"""
import socket
from uuid import uuid4

import pytest

from sim_harness import require_devices  # 连接器在设备仓库（connectors/environment）；导入时把它放进 sys.path


class ServiceHttp:
    def __init__(self, client, headers: dict):
        self.client = client
        self.headers = headers

    def post_json(self, path: str, payload: dict):
        response = self.client.post(path, json=payload, headers=self.headers)
        return response.status_code, response.json()


def _free_port() -> int:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


@pytest.fixture()
def glovebox():
    require_devices()
    from connectors.environment.glovebox_sim import Glovebox

    box = Glovebox(port=_free_port()).start()
    try:
        yield box
    finally:
        box.stop()


@pytest.fixture()
def zone_identity(client, admin):
    zone = f"配液段手套箱-{uuid4().hex[:6]}"
    created = admin.post("/api/service-identities", {
        "source": f"env-{uuid4().hex[:8]}", "name": "手套箱水氧连接器", "scopes": {"environment_zones": [zone]},
    })
    assert created.status_code == 201, created.text
    issued = created.json()
    return zone, ServiceHttp(client, {"X-Service-Source": issued["source"], "X-Service-Secret": issued["secret"]})


def _config(port: int, zone: str, *extra) -> dict:
    return {"sources": [{"kind": "modbus", "name": "手套箱控制器", "host": "127.0.0.1", "port": port, "readings": [
        {"zone": zone, "metric": "o2_ppm", "unit": "ppm", "table": "input", "address": 0, "type": "float32",
         "valid": [0, 1000]},
        {"zone": zone, "metric": "h2o_ppm", "unit": "ppm", "table": "input", "address": 2, "type": "float32",
         "valid": [0, 1000]},
        *extra,
    ]}]}


def test_glovebox_readings_reach_ilcs_and_gate_the_step(glovebox, zone_identity, operator, db):
    from app.core.clock import now
    from app.core.config import settings
    from app.core.context import system_context
    from app.domain import environment as rules
    from app.services.environment_service import EnvironmentService
    from connectors.environment.poller import Poller

    zone, http = zone_identity
    poller = Poller(_config(glovebox.port, zone), http=http)
    outcome = poller.run_once()
    assert outcome["posted"] == [f"{zone}/o2_ppm", f"{zone}/h2o_ppm"] and not outcome["rejected"], outcome
    latest = {row["metric"]: row for row in operator.get(f"/api/environment/readings?zone={zone}").json()}
    assert latest["o2_ppm"]["value"] == pytest.approx(0.5) and latest["h2o_ppm"]["value"] == pytest.approx(0.3)
    assert latest["o2_ppm"]["source"] == "device" and not latest["o2_ppm"]["stale"]

    # 电解液线加料步骤的要求：水、氧 ≤ 1 ppm。读数在范围内就放行，漏气之后同一条要求挡住投递
    service = EnvironmentService(db, system_context(operator.org_id if hasattr(operator, "org_id") else "ORG-001"))
    requirement = {"metric": "h2o_ppm", "max": 1, "zone": zone}

    def problem():
        db.expire_all()
        return rules.check(requirement, zone, service._reading(zone, "h2o_ppm"), now(), settings.environment_max_age_min)

    assert problem() is None
    glovebox.leak()
    assert poller.run_once()["posted"]
    assert "高于要求 1" in (problem() or "")


def test_fault_codes_and_unreachable_sensors_are_never_reported(glovebox, zone_identity, operator):
    from connectors.environment.poller import Poller

    zone, http = zone_identity
    poller = Poller(_config(glovebox.port, zone), http=http)
    assert poller.run_once()["posted"]
    before = {row["metric"]: row["id"] for row in operator.get(f"/api/environment/readings?zone={zone}").json()}
    glovebox.sensor_fault()  # 读数变成故障码 -9999
    outcome = poller.run_once()
    assert not outcome["posted"] and len(outcome["dropped"]) == 2, outcome
    glovebox.stop()
    outcome = poller.run_once()
    assert not outcome["posted"] and len(outcome["unreadable"]) == 2, outcome
    after = {row["metric"]: row["id"] for row in operator.get(f"/api/environment/readings?zone={zone}").json()}
    assert after == before, "故障码与读不到都不上报：最新读数仍是上一条，过期后 ILCS 自己挡住投递"


def test_an_unauthorized_zone_does_not_block_the_authorized_one(glovebox, zone_identity, operator):
    from connectors.environment.poller import Poller

    zone, http = zone_identity
    other = {"zone": "测试段手套箱-未授权", "metric": "o2_ppm", "unit": "ppm", "table": "input", "address": 0,
             "type": "float32"}
    outcome = Poller(_config(glovebox.port, zone, other), http=http).run_once()
    assert outcome["posted"] == [f"{zone}/o2_ppm", f"{zone}/h2o_ppm"]
    assert len(outcome["rejected"]) == 1 and "HTTP 403" in outcome["rejected"][0], outcome


def test_register_decoding_and_config_checks():
    require_devices()
    from connectors.environment.poller import Poller, decode_registers
    from connectors.environment.glovebox_sim import float_registers

    assert decode_registers(float_registers(0.75), "float32") == pytest.approx(0.75)
    assert decode_registers(list(reversed(float_registers(0.75))), "float32", "little") == pytest.approx(0.75)
    assert decode_registers([0xFFFF], "int16") == -1 and decode_registers([0x0001, 0x0000], "uint32") == 65536
    with pytest.raises(ValueError, match="至少要配置一个读数点"):
        Poller({"sources": []}, http=object())
    with pytest.raises(ValueError, match="address"):
        Poller({"sources": [{"kind": "modbus", "host": "h", "readings": [{"zone": "z", "metric": "o2_ppm"}]}]},
               http=object())
    with pytest.raises(ValueError, match="pattern"):
        Poller({"sources": [{"kind": "line", "link": {"kind": "tcp", "host": "h", "port": 1},
                             "readings": [{"zone": "z", "metric": "o2_ppm", "send": "O2?", "pattern": "(\\d+)"}]}]},
               http=object())


def test_environment_zone_scope_is_validated(admin):
    """服务身份可以授予 environment_zones：all 或区域名数组；别的写法拒绝（拼错的授权不能变成假授权）。"""
    everything = admin.post("/api/service-identities", {
        "source": f"env-all-{uuid4().hex[:6]}", "name": "全部区域", "scopes": {"environment_zones": "all"}})
    assert everything.status_code == 201 and everything.json()["scopes"]["environment_zones"] == "all"
    listed = admin.post("/api/service-identities", {
        "source": f"env-list-{uuid4().hex[:6]}", "name": "两个手套箱",
        "scopes": {"environment_zones": [" 测试段手套箱", "配液段手套箱", "配液段手套箱"]}})
    assert listed.status_code == 201 and listed.json()["scopes"]["environment_zones"] == ["测试段手套箱", "配液段手套箱"]
    wrong = admin.post("/api/service-identities", {
        "source": f"env-bad-{uuid4().hex[:6]}", "name": "写错", "scopes": {"environment_zones": "配液段手套箱"}})
    assert wrong.status_code == 422 and wrong.json()["detail"]["code"] == "service_scope_invalid"
