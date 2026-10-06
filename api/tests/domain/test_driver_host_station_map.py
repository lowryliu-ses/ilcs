"""经驱动宿主接的工位以设备仓库为准：scripts/load-driver-host-devices.py 按 ilcs-devices 现场目录的工位对照表
（host/sites/local/ilcs-stations.json）和设备文件推出 ILCS 的 sila2_v1 连接配置，不在 ILCS 里另写一份。"""
from __future__ import annotations

import importlib.util
import json

from tests.sim_harness import DEVICES, ROOT, require_devices


def _script():
    spec = importlib.util.spec_from_file_location("load_driver_host_devices_map", ROOT / "scripts" / "load-driver-host-devices.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_station_connections_follow_the_device_repo():
    require_devices()
    script = _script()
    site = DEVICES / "host" / "sites" / "local"
    table = json.loads((site / "ilcs-stations.json").read_text(encoding="utf-8"))["stations"]
    assert set(script.STATIONS) == set(table)
    for station_id, station in script.STATIONS.items():
        device = json.loads((site / "devices" / f"{table[station_id]['device']}.json").read_text(encoding="utf-8"))
        config = script.config_of(station)
        assert config["port"] == device["port"], station_id
        assert script.supports_of(station) == {f"supports_{k}": bool(v) for k, v in device["supports"].items()}
        assert config["expected_device_id"], station_id
    # 契约照设备：A-Lab 上位机能暂停；只读写点位的传感器不接指令
    assert script.STATIONS["EL-ALAB"]["supports"]["supports_hold"] is True
    assert "simulator_control" not in script.config_of(script.STATIONS["EL-ALAB"])
    assert script.config_of(script.STATIONS["ST-PF-MB"])["tasks"] is False
    # 模拟网关的控制口是网关自己的 API，凭据换成 ILCS 容器里的路径；一条指令几瓶照模块 profile.json
    balance = script.config_of(script.STATIONS["EL-D-BAL"])
    assert balance["simulator_control"]["token_ref"] == "file:///run/secrets/ilcs/gateway/SIM-EL-D-BAL.token"
    assert balance["wells_per_command"] == 1 and balance["request_timeout_sec"] == 18
    assert {"EL-D-ADD", "EL-D-STIR", "EL-D-COLD", "EL-D-MIX"} <= script.GATEWAY_STATIONS
