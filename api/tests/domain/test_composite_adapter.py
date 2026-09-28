"""`composite_v1`：一个工位多台仪器（真空干燥箱走串口命令 + 天平走 MT-SICS），按能力分派。"""
import time

import pytest

from sim_harness import balance_config, balance_sim, line_config, line_sim, record, request


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "adapter_state_root", str(tmp_path / "adapter-state"))
    return tmp_path


def _station(oven_port: int, balance_port: int, **routes):
    from app.adapters.composite import CompositeAdapter

    config = {"routes": [
        {"name": "oven", "capabilities": ["cap.vacuum_dry"], "driver": "line_command_v1",
         "config": line_config(oven_port, methods=["VD-120"])},
        {"name": "balance", "capabilities": ["cap.weigh"], "driver": "mt_sics_v1",
         "config": balance_config(balance_port, methods=[{"program": "*"}])},
    ]}
    config.update(routes)
    return CompositeAdapter(record("组合工位", config))


def test_capabilities_route_to_their_instruments_and_survive_restart(isolated):
    with line_sim(task_seconds=0.3) as (oven, _, oven_port), balance_sim() as (balance, _, balance_port):
        station = _station(oven_port, balance_port)
        health = station.healthcheck()
        assert health["simulator"] and not health["interlock"] and health["accepts_commands"]
        assert "oven=SIM-OVEN-T" in health["device_id"] and "balance=SIM-BAL-T" in health["device_id"]

        assert station.submit(request("CMD-DRY")).state == "accepted"
        weighed = station.submit(request("CMD-W", capability="cap.weigh", params={"mass": 0.0152}))
        assert weighed.state == "done" and balance.weighings == 1
        assert sum(oven.executions.values()) == 1

        restarted = _station(oven_port, balance_port)  # 执行器重启：各路由从自己的台账读回
        assert restarted.query("CMD-W").state == "done"
        deadline = time.monotonic() + 5
        while restarted.query("CMD-DRY").state != "done" and time.monotonic() < deadline:
            oven.tick()
            time.sleep(0.05)
        assert restarted.query("CMD-DRY").state == "done"
        assert restarted.query("CMD-NOBODY") is None
        assert (isolated / "adapter-state" / "ST-SIM#oven.json").exists()

        catalog = station.identity()
        assert catalog["methods_source"] == "config" and {"program": "*"} in catalog["methods"]


def test_misconfigured_routes_are_rejected():
    from app.adapters import AdapterError
    from app.adapters.composite import CompositeAdapter

    same = [{"name": "a", "capabilities": ["cap.weigh"], "driver": "mt_sics_v1", "config": balance_config(1)},
            {"name": "b", "capabilities": ["cap.weigh"], "driver": "mt_sics_v1", "config": balance_config(2)}]
    with pytest.raises(AdapterError, match="同时落在"):
        CompositeAdapter(record("组合工位", {"routes": same}))
    with pytest.raises(AdapterError, match="嵌套"):
        CompositeAdapter(record("组合工位", {"routes": [{"name": "x", "capabilities": ["c"], "driver": "composite_v1"}]}))
    with pytest.raises(AdapterError, match="没有登记"):
        CompositeAdapter(record("组合工位", {"routes": [{"name": "x", "capabilities": ["c"], "driver": "nope"}]}))
    with pytest.raises(AdapterError, match="配置无效"):
        CompositeAdapter(record("组合工位", {"routes": [{"name": "x", "capabilities": ["c"], "driver": "mt_sics_v1",
                                                          "config": {"transport": {"kind": "tcp", "host": "evil.example", "port": 1}}}]}))


def test_one_member_offline_makes_the_station_unreachable_and_unknown_capability_is_refused():
    from app.adapters import AdapterError, AdapterUnreachable

    with line_sim() as (_, _, oven_port):
        with balance_sim() as (_, _, balance_port):
            station = _station(oven_port, balance_port)
            with pytest.raises(AdapterError, match="没有落在"):
                station.submit(request("CMD-C", capability="cap.coat"))
        with pytest.raises(AdapterUnreachable):
            station.healthcheck()


def test_hold_follows_the_owner_of_the_target():
    with line_sim(task_seconds=30) as (oven, _, oven_port), balance_sim() as (_, _, balance_port):
        station = _station(oven_port, balance_port)
        station.submit(request("CMD-D"))
        assert station.hold(request("CMD-HOLD", "hold", target="CMD-D", capability="cap.weigh")).state == "done"
        assert next(iter(oven.tasks.values())).state == "held"
