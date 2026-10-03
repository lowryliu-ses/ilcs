"""设备主机白名单：精确主机名、IP、网段（CIDR）与域名后缀。

白名单是「适配器配置能把带凭据的请求发往哪里」的边界，不归工位编辑权限管：它写在部署配置里，
启动时读一次。支持网段与后缀，是为了让设备网段里新接的设备不用每次改 `.env`、重启服务；
要放行一个新网段，仍然是运维改部署配置。

- `instrument-gateway.lab.internal`、`10.20.1.31`：精确匹配；
- `10.20.1.0/24`、`fd00:20::/64`：IP 字面量落在网段里才匹配。主机名不做 DNS 解析去比网段——
  解析结果会变（DNS 重绑定），校验时的地址和真正连接时的地址可以不是同一个；
- `.lab.internal`（也可写成 `*.lab.internal`）：这个域下的任意主机名，不含 `lab.internal` 本身；
- `*`：任意主机，只在非正式环境生效，正式环境由配置门禁拒绝。

像数字又不是合法 IP 字面量的主机（`127.1`、`0x7f000001`、`2130706433`、`10.20.1.045`）一律不认：
系统解析器会把它们当成 IPv4 的另一种写法，校验时看到的和真正连上的不是同一个东西。
IPv4 映射地址（`::ffff:10.20.1.5`）按它实际连的 IPv4 地址比。
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import lru_cache
import ipaddress
import re

# 正式环境允许的最宽网段：再宽就等于放行了整个厂区网络，白名单形同虚设
MIN_PREFIX = {4: 16, 6: 48}
LOOPBACK_NAMES = {"localhost"}
HOST_LABEL = re.compile(r"^[a-z0-9_-]{1,63}$")
# 最后一级是十进制或 0x 十六进制数字：解析器按 IPv4 理解（URL 标准的 ends-in-a-number 规则）
NUMERIC_LABEL = re.compile(r"^(0x[0-9a-f]*|[0-9]+)$")
# 嵌入 IPv4 的 IPv6 网段：放行它们等于放行了对应的 IPv4 地址（映射地址、NAT64）
EMBEDS_IPV4 = tuple(ipaddress.ip_network(net) for net in ("::ffff:0:0/96", "64:ff9b::/96", "64:ff9b:1::/48"))
_SKIP = ContextVar("ilcs_host_check_skipped", default=False)


@contextmanager
def host_check_skipped():
    """只给「构造驱动实例查配置、不连设备」用：设备模板要能跨部署导入导出，示例地址不必在本部署的白名单里。

    按上下文生效（每个请求线程各自一份），出了 with 立即恢复；在里面做任何网络 I/O 都是用错了。
    """
    token = _SKIP.set(True)
    try:
        yield
    finally:
        _SKIP.reset(token)


def host_check_is_skipped() -> bool:
    return _SKIP.get()


def normalize_host(host: str) -> str:
    """小写、去掉末尾的点与 IPv6 的方括号；IP 按标准写法（`::0001` → `::1`），IPv4 映射地址写成 IPv4。"""
    value = (host or "").strip().lower().rstrip(".")
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return value
    if address.version == 6 and address.ipv4_mapped is not None:
        address = address.ipv4_mapped  # ::ffff:10.20.1.5 连的就是 10.20.1.5
    return str(address)


def plain_hostname(value: str) -> bool:
    """普通主机名：各级只含字母、数字、横线、下划线，最后一级不是数字（不是 IPv4 的另一种写法）。"""
    labels = value.split(".")
    return (0 < len(value) <= 253 and all(HOST_LABEL.fullmatch(label) for label in labels)
            and not NUMERIC_LABEL.fullmatch(labels[-1]))


@dataclass(frozen=True)
class HostAllowlist:
    names: frozenset[str]
    suffixes: tuple[str, ...]
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]
    wildcard: bool
    invalid: tuple[str, ...]

    def allows(self, host: str, *, wildcard: bool = True) -> bool:
        """`wildcard=False`：不认 `*`（正式环境）。"""
        value = normalize_host(host)
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            address = None
            if not plain_hostname(value):
                return False  # 空的、带怪字符的、像数字又不是合法 IP 的：解析器怎么理解说不准，一律不认
        if self.wildcard and wildcard:
            return True
        if value in self.names:
            return True
        if address is None:
            return any(value.endswith(suffix) for suffix in self.suffixes)
        return any(address.version == network.version and address in network for network in self.networks)

    def production_problems(self) -> list[str]:
        """正式环境不接受的写法。空列表表示可以用。"""
        problems: list[str] = []
        if self.invalid:
            problems.append(f"无法识别的条目：{'、'.join(self.invalid)}")
        if self.wildcard:
            problems.append("不允许 *")
        for network in self.networks:
            if network.prefixlen < MIN_PREFIX[network.version]:
                problems.append(f"网段 {network} 太宽（IPv{network.version} 至少 /{MIN_PREFIX[network.version]}）")
            embedded = next((net for net in EMBEDS_IPV4 if network.version == 6 and network.overlaps(net)), None)
            if embedded is not None:
                problems.append(f"网段 {network} 与 {embedded} 重叠：那是嵌入 IPv4 的地址，请直接写 IPv4 网段")
        for suffix in self.suffixes:
            if suffix.count(".") < 2:
                problems.append(f"域名后缀 {suffix} 太宽（至少两级，如 .lab.internal）")
        if not (self.names or self.suffixes or self.networks or self.wildcard):
            problems.append("没有列任何主机")
        elif not self._reaches_beyond_loopback():
            problems.append("只列了本机地址")
        return problems

    def _reaches_beyond_loopback(self) -> bool:
        if self.suffixes or self.wildcard:
            return True
        if any(not network.is_loopback for network in self.networks):
            return True
        for name in self.names:
            try:
                if not ipaddress.ip_address(name).is_loopback:
                    return True
            except ValueError:
                if name not in LOOPBACK_NAMES:
                    return True
        return False


@lru_cache(maxsize=32)
def parse_allowlist(raw: str) -> HostAllowlist:
    names: set[str] = set()
    suffixes: list[str] = []
    networks: list = []
    invalid: list[str] = []
    wildcard = False
    for entry in (raw or "").split(","):
        item = entry.strip().lower()
        if not item:
            continue
        if item == "*":
            wildcard = True
        elif "/" in item:
            try:
                networks.append(ipaddress.ip_network(item, strict=False))
            except ValueError:
                invalid.append(entry.strip())
        elif item.startswith("*.") or item.startswith("."):
            suffix = "." + item.lstrip("*").lstrip(".").rstrip(".")
            # 后缀要是普通域名（最后一级含字母）：`.0.0.1` 这种写法匹配不到任何正经主机名
            if not plain_hostname(suffix[1:]) or not re.search("[a-z]", suffix.rsplit(".", 1)[-1]):
                invalid.append(entry.strip())
            else:
                suffixes.append(suffix)
        elif "*" in item:
            invalid.append(entry.strip())
        else:
            name = normalize_host(item)
            try:
                ipaddress.ip_address(name)
            except ValueError:
                if not plain_hostname(name):
                    invalid.append(entry.strip())
                    continue
            names.add(name)
    return HostAllowlist(
        names=frozenset(names), suffixes=tuple(suffixes), networks=tuple(networks), wildcard=wildcard,
        invalid=tuple(invalid),
    )
