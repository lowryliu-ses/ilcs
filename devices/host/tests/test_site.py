"""现场配置：配置摘要算什么、不算什么；配错了宿主拒绝启动。"""
from __future__ import annotations

import pytest

from conftest import write_site


def _device(port: int = 50101, **extra) -> dict:
    config = {"host": "127.0.0.1", "port": 502, "request_timeout_sec": 1,
              "points": {"state": {"table": "holding", "address": 1, "type": "uint16"}}}
    return {"plugin": "modbus_map", "port": port, "config": config, **extra}


def _load(root):
    from ilcs_host.plugins import PLUGINS
    from ilcs_host.site import load_site

    return load_site(root, set(PLUGINS))


def test_digest_tracks_what_decides_outcomes_not_timeouts_or_deployment():
    from ilcs_host.site import config_digest

    base = {"host": "10.0.0.5", "points": {"state": {"address": 1}}, "request_timeout_sec": 1}
    supports = {"hold": False, "abort": False, "query": True, "dedup": True}
    digest = config_digest("modbus_map", False, supports, "", "", base)
    assert digest.startswith("sha256:") and len(digest) == 71
    assert config_digest("modbus_map", False, supports, "", "", {**base, "request_timeout_sec": 9}) == digest
    assert config_digest("modbus_map", False, supports, "", "", {**base, "host": "10.0.0.6"}) != digest
    assert config_digest("opcua_map", False, supports, "", "", base) != digest
    assert config_digest("modbus_map", True, supports, "", "", base) != digest
    assert config_digest("modbus_map", False, {**supports, "hold": True}, "", "", base) != digest
    assert config_digest("modbus_map", False, supports, "", "file:///run/secrets/x", base) != digest


def test_port_and_uuid_are_deployment_details(tmp_path):
    first = _load(write_site(tmp_path / "a", {"PLC-1": _device(50101)})).devices[0]
    moved = _load(write_site(tmp_path / "b", {"PLC-1": _device(50999, config_version="r2")})).devices[0]
    assert first.digest == moved.digest, "换端口、换配置版本号不改摘要"
    assert first.server_uuid == moved.server_uuid, "服务器 UUID 按设备键生成，重新部署不变"


@pytest.mark.parametrize(("devices", "host", "message"), [
    ({"A": _device(50101), "B": _device(50101)}, {}, "端口重复"),
    ({"A": {**_device(), "plugin": "nope"}}, {}, "插件 nope 不存在"),
    ({"A": {**_device(), "supports": {"pause": True}}}, {}, "supports 只能有"),
    ({"A": _device()}, {"environment": "production", "allowed_hosts": "10.20.0.0/16"}, "必须配 TLS"),
])
def test_bad_sites_are_refused(tmp_path, devices, host, message):
    from ilcs_host.site import SiteError

    with pytest.raises(SiteError, match=message):
        _load(write_site(tmp_path, devices, **host))


def test_production_needs_tokens_and_refuses_simulators(tmp_path):
    from ilcs_host.site import SiteError

    tls = {"environment": "production", "allowed_hosts": "10.20.0.0/16", "certificate": "host.crt", "private_key": "host.key"}
    with pytest.raises(SiteError, match="令牌文件"):
        _load(write_site(tmp_path / "a", {"A": _device()}, tokens=False, **tls))
    with pytest.raises(SiteError, match="不接模拟设备"):
        _load(write_site(tmp_path / "b", {"A": _device(simulator=True)}, **tls))


def test_plugin_rejects_a_bad_mapping_before_anything_starts(tmp_path):
    from ilcs_host.server import prepare
    from ilcs_host.site import SiteError

    broken = _device()
    broken["config"]["capabilities"] = {"cap.x": {"start": {"point": "state"}}}  # 配了能力映射却没有状态点
    with pytest.raises(SiteError, match="status.point"):
        prepare(_load(write_site(tmp_path, {"PLC-1": broken})))


def test_shipped_sites_load_and_every_plugin_constructs(tmp_path):
    """仓库里带的现场配置都能加载，每台设备的插件都能按配置构造（不连设备）。"""
    from pathlib import Path

    from ilcs_host.features import DeviceRuntime
    from ilcs_host.plugins import PLUGINS
    from ilcs_host.settings import settings
    from ilcs_host.site import load_site

    sites = sorted(path for path in (Path(__file__).resolve().parents[1] / "sites").iterdir() if path.is_dir())
    assert sites, "仓库里应带着试点现场配置"
    for site_dir in sites:
        site = load_site(site_dir, set(PLUGINS))
        settings.configure(environment="development", allowed_hosts=site.allowed_hosts,
                           credential_root=str(tmp_path), state_dir=str(tmp_path))
        for entry in site.devices:
            runtime = DeviceRuntime(entry, PLUGINS[entry.plugin], tmp_path)
            assert runtime.points or runtime.tasks, f"{site_dir.name}/{entry.key} 既没有点表也没有能力映射"
