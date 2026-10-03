"""ILCS 的 `sila2_v1` × 驱动宿主（devices/host）× 外部 PLC 模拟设备：ILCS 只经 SiLA 2 读写设备、下发作业。

驱动宿主用的插件配置就是 ILCS 映射驱动的那份配置（`plc_config` 原样放进去）：迁过去不用改写配置。
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from sim_harness import HOST_TOKEN as TOKEN, driver_host, host_plc_device as _plc_device, plc_sim, request

ROOT = Path(__file__).resolve().parents[3]
HOST = ROOT / "devices" / "host"


@pytest.fixture(autouse=True)
def credentials(tmp_path, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "adapter_credential_root", str(tmp_path))
    (tmp_path / "host.token").write_text(TOKEN + "\n", encoding="utf-8")
    return tmp_path


def _adapter(port: int, credential_ref: str = "", **config):
    from app.adapters.drivers.sila2 import Sila2Adapter

    return Sila2Adapter(SimpleNamespace(
        station_id="ST-HOST", protocol="SiLA 2", version="1.0", note="", credential_ref=credential_ref,
        config={"host": "127.0.0.1", "port": port, "insecure": True, "request_timeout_sec": 3, **config},
        supports_hold=True, supports_abort=True, supports_query=True, supports_dedup=True,
    ))


def _coat(command_id: str, capability: str = "cap.coat"):
    return request(command_id, params={"thickness": 180, "temp": 110}, capability=capability)


def _wait(adapter, command_id: str, seconds: float = 6):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = adapter.query(command_id)
        if result is not None and result.state in {"done", "failed"}:
            return result
        time.sleep(0.1)
    raise AssertionError(f"{command_id} 没有在时限内结束")


def test_driver_host_passes_its_own_tests():
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(HOST / "tests")], cwd=ROOT,
        capture_output=True, text=True, timeout=300, env=dict(os.environ),
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_ilcs_drives_a_plc_only_through_the_driver_host(credentials):
    from app.adapters.base import AdapterError
    from app.domain.exceptions import classify

    token_ref = f"file://{credentials / 'host.token'}"
    with plc_sim("modbus", task_seconds=0.4) as (program, _, plc_port):
        device = _plc_device(plc_port)
        with driver_host(credentials / "site", {"PLC-1": device}) as site:
            adapter = _adapter(device["port"], token_ref, expected_device_id="SIM-PLC-T")
            health = adapter.healthcheck()
            assert (health["device_id"], health["simulator"], health["accepts_commands"]) == ("SIM-PLC-T", True, True)
            assert health["driver_info"]["config_digest"] == site.devices[0].digest
            assert health["driver_info"]["plugin"] == "modbus_map"
            identity = adapter.identity()
            assert {"DeviceInfo", "PointAccess", "TaskExecution", "AuthorizationService"} <= set(identity["features"])
            assert identity["task_support"]["handoff"] == "async"

            rows = {row["name"]: row for row in adapter.read_points()}
            assert rows["serial"]["value"] == "SIM-PLC-T" and rows["serial"]["error"] == ""
            assert rows["sp_temp"]["writable"] and rows["sp_temp"]["unit"] == "℃" and rows["cmd_start"]["control"]
            outcome = adapter.write_point_manually("sp_temp", 80.0, request_id="PW-1")
            assert outcome["after"] == pytest.approx(80.0) and outcome["matches"]
            with pytest.raises(AdapterError, match="超出"):
                adapter.write_point_manually("sp_temp", 999.0)

            accepted = adapter.submit(_coat("CMD-1"))
            assert (accepted.state, accepted.origin) == ("accepted", "real:sila2_v1")
            adapter.submit(_coat("CMD-1"))
            assert sum(program.device.executions.values()) == 1, "同一指令号只动作一次"
            done = _wait(adapter, "CMD-1")
            assert done.state == "done" and abs(done.delivered["thickness"] - 180) < 2
            assert adapter.query("CMD-NEVER") is None

            with pytest.raises(AdapterError) as unsupported:
                adapter.submit(_coat("CMD-2", capability="cap.other"))
            assert str(unsupported.value).startswith("设备不支持（NotSupported）")
            assert classify(str(unsupported.value)) == "device_fault"
            program.device.interlock = True
            time.sleep(0.3)  # PLC 扫描周期把联锁同步进 SafetyOk
            with pytest.raises(AdapterError) as interlocked:
                adapter.submit(_coat("CMD-3"))
            assert classify(str(interlocked.value)) == "safety", "联锁按错误标识归成安全异常，只转人工"
            assert sum(program.device.executions.values()) == 1, "被拒的指令没有让设备动作"


def test_device_unreachable_before_acting_is_a_definite_failure(credentials):
    from app.adapters.base import AdapterError, AdapterUnreachable
    from app.domain.exceptions import classify

    with plc_sim("modbus") as (_, _, plc_port):
        pass  # PLC 已经停了：驱动宿主在，设备不在
    device = _plc_device(plc_port)
    with driver_host(credentials / "site", {"PLC-1": device}):
        adapter = _adapter(device["port"], f"file://{credentials / 'host.token'}")
        with pytest.raises(AdapterUnreachable):
            adapter.healthcheck()  # 读不到身份：离线
        with pytest.raises(AdapterError) as refused:
            adapter.submit(_coat("CMD-OFF"))
        assert not isinstance(refused.value, AdapterUnreachable), "动作前就连不上：设备没动，是明确失败"
        assert classify(str(refused.value)) == "communication"


def test_point_only_device_and_token_rules(credentials):
    from app.adapters.base import AdapterError

    token_ref = f"file://{credentials / 'host.token'}"
    with plc_sim("modbus") as (program, _, plc_port):
        device = _plc_device(plc_port, tasks=False)
        with driver_host(credentials / "site", {"PLC-P": device}):
            points_only = _adapter(device["port"], token_ref, tasks=False)
            assert points_only.healthcheck()["device_id"] == "SIM-PLC-T"
            assert "TaskExecution" not in points_only.identity()["features"]
            assert points_only.query("CMD-ANY") is None, "不参与自动流程：查询如实回答没有"
            with pytest.raises(AdapterError, match="只读写点位"):
                points_only.submit(_coat("CMD-P"))
            assert points_only.write_point_manually("sp_temp", 25.0, request_id="PW-P")["matches"]
            with pytest.raises(AdapterError, match="tasks"):
                _adapter(device["port"], token_ref).healthcheck()  # 配置说参与自动流程，设备服务却没有任务接口

            with pytest.raises(AdapterError, match="要求令牌"):
                _adapter(device["port"], "", tasks=False).healthcheck()
            (credentials / "wrong.token").write_text("w" * 40, encoding="utf-8")
            with pytest.raises(AdapterError, match="InvalidAccessToken"):
                _adapter(device["port"], f"file://{credentials / 'wrong.token'}", tasks=False).healthcheck()
        assert sum(program.device.executions.values()) == 0


def test_connections_opened_at_the_same_time_do_not_trip_over_each_other(credentials):
    """执行器重启后第一轮会并发探测好几台 SiLA 设备：同时建客户端不能偶发失败。

    sila2 0.14.0 建客户端时现场编译 protobuf、改 sys.modules，并发时会偶发 KeyError（如 'SiLAService_pb2'），
    ILCS 曾因此把在线的设备判成失联、报一条报警。"""
    import threading

    with plc_sim("modbus") as (_, _, plc_port):
        devices = {f"PLC-{index}": _plc_device(plc_port, tasks=False) for index in range(3)}
        with driver_host(credentials / "site", devices):
            failures: list[str] = []

            def connect(port: int) -> None:
                try:
                    _adapter(port, f"file://{credentials / 'host.token'}", tasks=False)._connect()
                except Exception as exc:  # noqa: BLE001  收集起来最后一起断言
                    failures.append(repr(exc))

            for _ in range(6):
                threads = [threading.Thread(target=connect, args=(device["port"],))
                           for device in devices.values() for _ in range(2)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
    assert failures == [], failures[:3]
