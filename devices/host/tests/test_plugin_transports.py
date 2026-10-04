"""插件的通道与编码：设备主机按驱动宿主的白名单放行（网段、后缀），HTTP 路径不能跳出 base_url，选项型参数写设备代码。"""
import pytest


def test_drivers_accept_new_devices_inside_the_allowed_network(host_settings, monkeypatch):
    """新设备落在已放行的网段里：插件直接接受，不用改白名单、不用重启。"""
    from ilcs_host.plugins.base import AdapterError
    from ilcs_host.plugins.http_client import HttpTransport
    from ilcs_host.plugins.line_command import LineTransport

    monkeypatch.setattr(host_settings, "allowed_hosts", "10.20.1.0/24,.lab.internal")
    HttpTransport({"base_url": "https://10.20.1.45:8443/api/v1"}, "", driver="rest_map_v1")
    LineTransport({"transport": {"kind": "tcp", "host": "oven-07.lab.internal", "port": 4001}})
    with pytest.raises(AdapterError, match="白名单"):
        HttpTransport({"base_url": "https://10.20.2.45:8443/api/v1"}, "", driver="rest_map_v1")
    with pytest.raises(AdapterError, match="白名单"):
        LineTransport({"transport": {"kind": "tcp", "host": "oven-07.other.internal", "port": 4001}})
    with pytest.raises(AdapterError, match="白名单"):
        LineTransport({"transport": {"kind": "serial", "port": "rfc2217://moxa.other.internal:4001"}})
    with pytest.raises(AdapterError, match="本机串口只允许"):
        LineTransport({"transport": {"kind": "serial", "port": "/etc/passwd"}})


def test_http_paths_cannot_leave_the_base_url_host(host_settings, monkeypatch):
    """映射配置里的路径写成完整网址，会把请求与凭据带去白名单之外：拼出的地址必须仍是 base_url 那台主机。"""
    from ilcs_host.plugins.base import AdapterError
    from ilcs_host.plugins.http_client import HttpTransport

    monkeypatch.setattr(host_settings, "allowed_hosts", "10.20.1.0/24")
    transport = HttpTransport({"base_url": "https://10.20.1.45:8443/api/v1"}, "", driver="rest_map_v1")
    assert transport.url("/jobs/{id}", {"id": "a/b"}) == "https://10.20.1.45:8443/api/v1/jobs/a%2Fb"
    assert transport.url("//jobs") == "https://10.20.1.45:8443/api/v1/jobs"
    for path in ("https://evil.example/steal", "http://10.20.1.45:8443/api/v1/x", "https://10.20.1.45:9443/x",
                 "https://user:pw@10.20.1.45:8443/x"):
        with pytest.raises(AdapterError, match="base_url 之外"):
            transport.url(path)


def test_point_map_writes_option_codes():
    from ilcs_host.plugins.base import AdapterError
    from ilcs_host.plugins.point_map import PointMapAdapter

    item = {"point": "sp_solvent", "map": {"THF": 1, "DMF": 2}}
    assert PointMapAdapter._coded("solvent", item, "DMF") == 2
    assert PointMapAdapter._coded("temp", "sp_temp", 60) == 60
    with pytest.raises(AdapterError, match="没有在 write.solvent.map 里登记"):
        PointMapAdapter._coded("solvent", item, "Toluene")
