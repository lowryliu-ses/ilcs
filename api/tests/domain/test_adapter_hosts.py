"""设备主机白名单：主机名、IP、网段与域名后缀。设备网段里新接的设备不用改配置、不用重启。"""
import pytest

from app.core.hosts import parse_allowlist

SAFE = {
    "environment": "production",
    "secret_key": "token-" + "a" * 40,
    "password_pepper": "password-" + "b" * 40,
    "database_url": "postgresql+psycopg2://ilcs:secret@db:5432/ilcs",
    "cors_origins": "https://ilcs.lab.internal",
}


def test_exact_names_and_addresses():
    allow = parse_allowlist("instrument-gateway.lab.internal, 10.20.1.31 ,LOCALHOST")
    assert allow.allows("instrument-gateway.lab.internal") and allow.allows("Instrument-Gateway.Lab.Internal.")
    assert allow.allows("10.20.1.31") and allow.allows("localhost")
    assert not allow.allows("10.20.1.32") and not allow.allows("gateway.lab.internal") and not allow.allows("")


def test_networks_match_ip_literals_only():
    allow = parse_allowlist("10.20.1.0/24,fd00:20::/64")
    assert allow.allows("10.20.1.45") and allow.allows("10.20.1.255")
    assert not allow.allows("10.20.2.1")
    assert allow.allows("[fd00:20::5]") and allow.allows("fd00:20:0:0::9")
    assert not allow.allows("fd00:21::1")
    # 主机名不去解析 DNS 再比网段：解析结果会变，校验时与连接时可能不是同一个地址
    assert not allow.allows("plc-01.lab.internal")


def test_suffix_matches_subdomains_only():
    allow = parse_allowlist(".lab.internal,*.fab.local")
    assert allow.allows("balance-01.lab.internal") and allow.allows("a.b.LAB.internal.")
    assert allow.allows("oven.fab.local")
    assert not allow.allows("lab.internal"), "后缀只认子域，不含域本身"
    assert not allow.allows("evil-lab.internal") and not allow.allows("lab.internal.evil.com")
    # 后缀不匹配 IP 字面量
    assert not parse_allowlist(".0.0.1").allows("127.0.0.1")


def test_numeric_aliases_and_odd_hostnames_are_refused():
    """解析器把 127.1、0x7f000001、2130706433 当 IPv4 的另一种写法：不是合法 IP 字面量，也不当主机名认，* 也不放行。"""
    allow = parse_allowlist("*,10.20.1.0/24,.lab.internal")
    for host in ("127.1", "0x7f000001", "2130706433", "10.20.1.045", "0x0a.20.1.45", "oven.lab.0x7f",
                 "evil.example/.lab.internal", "x@oven.lab.internal", "oven..lab.internal", ""):
        assert not allow.allows(host), host
    assert allow.allows("oven-07.lab.internal") and allow.allows("anything.example")
    # IPv4 映射地址按它实际连的 IPv4 比
    assert parse_allowlist("10.20.1.0/24").allows("::ffff:10.20.1.45")
    assert not parse_allowlist("::ffff:0:0/96").allows("::ffff:10.99.0.1")
    assert parse_allowlist("::ffff:10.20.1.31").allows("10.20.1.31")
    # 白名单里的这类写法本身就是无法识别的条目
    assert parse_allowlist(".0.0.1,127.1,.lab.0x7f,a/b").invalid == (".0.0.1", "127.1", ".lab.0x7f", "a/b")


def test_wildcard_is_honoured_only_outside_production():
    allow = parse_allowlist("*")
    assert allow.allows("anything.example") and not allow.allows("anything.example", wildcard=False)


@pytest.mark.parametrize(("raw", "fragment"), [
    ("*", "不允许 *"),
    ("10.0.0.0/8", "网段 10.0.0.0/8 太宽"),
    ("fd00::/16", "太宽"),
    (".internal", "域名后缀 .internal 太宽"),
    ("127.0.0.1,localhost,::1", "只列了本机地址"),
    ("", "没有列任何主机"),
    ("10.20.1.0/33", "无法识别的条目：10.20.1.0/33"),
    ("plc-*.lab.internal", "无法识别的条目"),
    ("::ffff:0:0/96", "嵌入 IPv4"),
    ("64:ff9b::/96", "嵌入 IPv4"),
])
def test_production_rejects_unsafe_entries(raw, fragment):
    problems = parse_allowlist(raw).production_problems()
    assert any(fragment in problem for problem in problems), problems


def test_production_accepts_bounded_networks_and_suffixes():
    assert parse_allowlist("10.20.1.0/24,10.21.0.0/16,.lab.internal,instrument-gateway.lab.internal").production_problems() == []
    assert parse_allowlist("127.0.0.1,10.20.1.31").production_problems() == []


def test_settings_gate_and_matcher(monkeypatch):
    from app.core.config import Settings, settings

    broad = Settings(**SAFE, adapter_allowed_hosts="10.0.0.0/8")
    assert any("ILCS_ADAPTER_ALLOWED_HOSTS" in issue and "太宽" in issue for issue in broad.production_issues())
    assert Settings(**SAFE, adapter_allowed_hosts="10.20.1.0/24,.lab.internal").production_issues() == []

    monkeypatch.setattr(settings, "adapter_allowed_hosts", "10.20.1.0/24,.lab.internal")
    assert settings.adapter_host_allowed("10.20.1.9") and settings.adapter_host_allowed("oven.lab.internal")
    assert not settings.adapter_host_allowed("10.20.9.9")
    monkeypatch.setattr(settings, "adapter_allowed_hosts", "*")
    assert settings.adapter_host_allowed("10.99.0.1")
    monkeypatch.setattr(settings, "environment", "production")
    assert not settings.adapter_host_allowed("10.99.0.1"), "正式环境不认 *（配置门禁也会拒绝启动）"


def test_drivers_accept_new_devices_inside_the_allowed_network(monkeypatch):
    """新设备落在已放行的网段里：驱动直接接受，不用改白名单、不用重启。"""
    from app.adapters.base import AdapterError
    from app.adapters.http_client import HttpTransport
    from app.core.config import settings

    monkeypatch.setattr(settings, "adapter_allowed_hosts", "10.20.1.0/24,.lab.internal")
    HttpTransport({"base_url": "https://10.20.1.45:8443/api/v1"}, "", driver="http_json_v1")
    HttpTransport({"base_url": "https://gw-07.lab.internal/api/v1"}, "", driver="http_json_v1")
    with pytest.raises(AdapterError, match="白名单"):
        HttpTransport({"base_url": "https://10.20.2.45:8443/api/v1"}, "", driver="http_json_v1")
    with pytest.raises(AdapterError, match="白名单"):
        HttpTransport({"base_url": "https://gw-07.other.internal/api/v1"}, "", driver="http_json_v1")


def test_http_paths_cannot_leave_the_base_url_host(monkeypatch):
    """映射配置里的路径写成完整网址，会把请求与凭据带去白名单之外：拼出的地址必须仍是 base_url 那台主机。"""
    from app.adapters.base import AdapterError
    from app.adapters.http_client import HttpTransport
    from app.core.config import settings

    monkeypatch.setattr(settings, "adapter_allowed_hosts", "10.20.1.0/24")
    transport = HttpTransport({"base_url": "https://10.20.1.45:8443/api/v1"}, "", driver="http_json_v1")
    assert transport.url("/jobs/{id}", {"id": "a/b"}) == "https://10.20.1.45:8443/api/v1/jobs/a%2Fb"
    assert transport.url("//jobs") == "https://10.20.1.45:8443/api/v1/jobs"
    for path in ("https://evil.example/steal", "http://10.20.1.45:8443/api/v1/x", "https://10.20.1.45:9443/x",
                 "https://user:pw@10.20.1.45:8443/x"):
        with pytest.raises(AdapterError, match="base_url 之外"):
            transport.url(path)
