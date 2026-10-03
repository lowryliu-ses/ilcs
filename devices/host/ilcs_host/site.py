"""现场配置：`host.json` 加 `devices/*.json`，一个文件一台设备。

```
sites/<现场>/
  host.json          宿主：运行环境、监听地址、主机白名单、凭据目录、台账目录、令牌文件、TLS
  devices/<设备>.json  插件、端口、支持标志、是否模拟设备、配置版本、插件配置（写法与 ILCS 的适配器配置一样）
```

配置摘要（`DeviceInfo.Driver.ConfigDigest`）按 `devices/contracts/sila2/README.md` 算：参与计算的是插件、是否模拟设备、
支持标志、按配置登记的设备编号、凭据引用，以及去掉超时与探测周期的插件配置；端口、服务器 UUID、配置版本这类部署信息不参与。
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
from uuid import NAMESPACE_URL, UUID, uuid5

# 和 ILCS 现在「有在途指令也能改」的键一致：只决定等多久，不决定连谁、怎么判结论
FREE_CONFIG_KEYS = frozenset({"connect_timeout_sec", "request_timeout_sec", "probe_interval_sec", "acceptance"})
SUPPORTS = ("hold", "abort", "query", "dedup")
DEVICE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class SiteError(ValueError):
    """现场配置写错了：宿主拒绝启动，不带着错配置连设备。"""


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def config_digest(plugin: str, simulator: bool, supports: dict, device_id: str, credential_ref: str, config: dict) -> str:
    material = {
        "plugin": plugin, "simulator": simulator, "supports": supports, "device_id": device_id,
        "credential_ref": credential_ref,
        "config": {key: value for key, value in config.items() if key not in FREE_CONFIG_KEYS},
    }
    return "sha256:" + hashlib.sha256(canonical(material).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DeviceEntry:
    key: str
    plugin: str
    port: int
    config: dict
    supports: dict
    simulator: bool
    device_id: str  # 设备报不出身份时按配置登记的编号；能报的以设备为准
    config_version: str
    server_uuid: UUID
    credential_ref: str = ""  # 凭据只写引用（env:// / file://），原文不进配置
    note: str = ""

    @property
    def digest(self) -> str:
        return config_digest(self.plugin, self.simulator, self.supports, self.device_id, self.credential_ref, self.config)


@dataclass(frozen=True)
class Site:
    name: str
    root: Path
    environment: str
    address: str
    allowed_hosts: str
    credential_root: Path
    state_dir: Path
    tokens_file: Path | None
    certificate: Path | None
    private_key: Path | None
    host_name: str
    devices: tuple[DeviceEntry, ...] = field(default_factory=tuple)

    @property
    def insecure(self) -> bool:
        return self.certificate is None


def _path(root: Path, value) -> Path | None:
    if not value:
        return None
    path = Path(str(value))
    return path if path.is_absolute() else (root / path).resolve()


def _read(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SiteError(f"{path} 读不了或不是 JSON：{exc}") from exc
    if not isinstance(raw, dict):
        raise SiteError(f"{path} 必须是 JSON 对象")
    return raw


def _device(path: Path, plugins: set[str]) -> DeviceEntry:
    raw = _read(path)
    key = path.stem
    if not DEVICE_KEY.fullmatch(key):
        raise SiteError(f"设备文件名 {path.name} 不能用作设备键（字母数字开头，只含字母、数字、点、横线、下划线）")
    plugin = str(raw.get("plugin") or "")
    if plugin not in plugins:
        raise SiteError(f"{path.name}：插件 {plugin or '（缺失）'} 不存在；可用 {'、'.join(sorted(plugins))}")
    port = raw.get("port")
    if isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536:
        raise SiteError(f"{path.name}：port 必须是 1–65535 的整数")
    config = raw.get("config")
    if not isinstance(config, dict) or not config:
        raise SiteError(f"{path.name}：config 必须是插件配置对象")
    supports = raw.get("supports") or {}
    if not isinstance(supports, dict) or set(supports) - set(SUPPORTS):
        raise SiteError(f"{path.name}：supports 只能有 {' / '.join(SUPPORTS)}")
    supports = {name: bool(supports.get(name, name in {"query", "dedup"})) for name in SUPPORTS}
    try:
        server_uuid = UUID(str(raw["server_uuid"])) if raw.get("server_uuid") else uuid5(NAMESPACE_URL, f"ilcs-host:{key}")
    except ValueError as exc:
        raise SiteError(f"{path.name}：server_uuid 不是 UUID") from exc
    return DeviceEntry(
        key=key, plugin=plugin, port=port, config=config, supports=supports, simulator=bool(raw.get("simulator")),
        device_id=str(raw.get("device_id") or ""), config_version=str(raw.get("config_version") or ""),
        server_uuid=server_uuid, credential_ref=str(raw.get("credential_ref") or ""), note=str(raw.get("note") or ""),
    )


def load_site(directory, plugins: set[str]) -> Site:
    root = Path(directory).resolve()
    host = _read(root / "host.json")
    environment = str(host.get("environment") or "development")
    certificate, private_key = _path(root, host.get("certificate")), _path(root, host.get("private_key"))
    if bool(certificate) != bool(private_key):
        raise SiteError("certificate 与 private_key 要么都配、要么都不配（都不配就是不加密，只限非正式环境）")
    if certificate is None and environment == "production":
        raise SiteError("正式环境必须配 TLS 证书（certificate、private_key）")
    tokens_file = _path(root, host.get("tokens_file"))
    if tokens_file is None and environment == "production":
        raise SiteError("正式环境必须配令牌文件 tokens_file：ILCS 的每次调用都要带令牌")
    devices = tuple(sorted(
        (_device(path, plugins) for path in (root / "devices").glob("*.json")), key=lambda entry: entry.key,
    ))
    if not devices:
        raise SiteError(f"{root / 'devices'} 下没有设备配置")
    ports = [entry.port for entry in devices]
    if len(ports) != len(set(ports)):
        raise SiteError("设备端口重复：一台设备一个 SiLA 服务、一个端口")
    if environment == "production" and any(entry.simulator for entry in devices):
        raise SiteError("正式环境不接模拟设备")
    return Site(
        name=str(host.get("name") or root.name), root=root, environment=environment,
        address=str(host.get("address") or "0.0.0.0"),
        allowed_hosts=str(host.get("allowed_hosts") or "127.0.0.1,localhost"),
        credential_root=_path(root, host.get("credential_root") or "secrets"),
        state_dir=_path(root, host.get("state_dir") or "state"),
        tokens_file=tokens_file, certificate=certificate, private_key=private_key,
        host_name=str(host.get("host_name") or "localhost"), devices=devices,
    )
