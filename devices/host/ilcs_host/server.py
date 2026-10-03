"""按现场配置起服务：每台设备一个 SiLA 服务器（自己的端口与服务器 UUID），同一个进程里托管。

一台设备实现哪些特性看它的配置：都实现 DeviceInfo；登记了点表的加 PointAccess；配了能力映射的加 TaskExecution。
配了令牌文件就加 AuthorizationService，三组设备特性的全部调用都要带令牌。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import ipaddress
import logging
from pathlib import Path

from sila2.features.authorizationservice import AuthorizationServiceFeature
from sila2.server import SilaServer

from . import __version__
from .auth import Authorization, TokenCheck, read_tokens
from .features import (
    DEVICE_INFO, POINT_ACCESS, TASK_EXECUTION, DeviceInfoImpl, DeviceRuntime, PointAccessImpl, TaskExecutionImpl,
)
from .plugins import PLUGINS
from .settings import settings
from .site import Site, SiteError

log = logging.getLogger("ilcs.host")


def prepare(site: Site) -> list[DeviceRuntime]:
    """配置宿主、建好每台设备的插件实例；配置不对就整体拒绝启动。"""
    settings.configure(
        environment=site.environment, allowed_hosts=site.allowed_hosts,
        credential_root=str(site.credential_root), state_dir=str(site.state_dir),
    )
    site.state_dir.mkdir(parents=True, exist_ok=True)
    return [DeviceRuntime(entry, PLUGINS[entry.plugin], site.state_dir) for entry in site.devices]


def build(runtime: DeviceRuntime, tokens: frozenset[str] | None) -> SilaServer:
    entry = runtime.entry
    server = SilaServer(
        server_name=entry.key[:255], server_type="ILCSDeviceService",
        server_description=f"ILCS 设备服务：{entry.key}（{entry.plugin}）", server_version=__version__,
        server_vendor_url="https://ses.ai", server_uuid=entry.server_uuid,
    )
    protected = [DEVICE_INFO]
    server.set_feature_implementation(DEVICE_INFO, DeviceInfoImpl(server, runtime))
    if runtime.points:
        server.set_feature_implementation(POINT_ACCESS, PointAccessImpl(server, runtime))
        protected.append(POINT_ACCESS)
    if runtime.tasks:
        server.set_feature_implementation(TASK_EXECUTION, TaskExecutionImpl(server, runtime))
        protected.append(TASK_EXECUTION)
    if tokens:
        server.set_feature_implementation(AuthorizationServiceFeature, Authorization(server, protected))
        server.add_metadata_interceptor(TokenCheck(tokens))
    return server


def credentials(site: Site) -> tuple[bytes, bytes] | None:
    """TLS 私钥与证书。非正式环境缺文件时生成一张自签证书（ILCS 的 ca_file 指向它）；正式环境必须事先签发。"""
    if site.insecure:
        return None
    if not site.certificate.exists() or not site.private_key.exists():
        if site.environment == "production":
            raise SiteError(f"证书 {site.certificate} 或私钥 {site.private_key} 不存在")
        key, cert = self_signed(site.host_name)
        site.certificate.parent.mkdir(parents=True, exist_ok=True)
        site.private_key.parent.mkdir(parents=True, exist_ok=True)
        site.private_key.write_bytes(key)
        site.private_key.chmod(0o600)
        site.certificate.write_bytes(cert)
        log.info("已生成自签证书 %s；ILCS 的 ca_file 指向它", site.certificate)
    return site.private_key.read_bytes(), site.certificate.read_bytes()


def self_signed(host_name: str) -> tuple[bytes, bytes]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host_name), x509.NameAttribute(NameOID.ORGANIZATION_NAME, "ILCS driver host")])
    try:
        alt = x509.IPAddress(ipaddress.ip_address(host_name))
    except ValueError:
        alt = x509.DNSName(host_name)
    moment = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(moment - timedelta(minutes=5))
        .not_valid_after(moment + timedelta(days=825))
        .add_extension(x509.SubjectAlternativeName([alt]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return (
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()),
        cert.public_bytes(serialization.Encoding.PEM),
    )


def start(site: Site, runtimes: list[DeviceRuntime]) -> list[SilaServer]:
    tokens = read_tokens(site.tokens_file) if site.tokens_file else None
    if tokens is None:
        log.warning("没有配令牌文件：设备服务不认证调用方（只限非正式环境）")
    material = credentials(site)
    servers = []
    try:
        for runtime in runtimes:
            server = build(runtime, tokens)
            if material is None:
                server.start_insecure(site.address, runtime.entry.port, enable_discovery=False)
            else:
                server.start(site.address, runtime.entry.port, private_key=material[0], cert_chain=material[1],
                             enable_discovery=False)
            servers.append(server)
            runtime.record_digest()
            log.info("设备 %s（%s）已上线：%s:%s，特性 %s", runtime.entry.key, runtime.entry.plugin, site.address,
                     runtime.entry.port, "、".join(f.fully_qualified_identifier.split("/")[2] for f in features_of(runtime)))
    except Exception:
        stop(servers)
        raise
    return servers


def features_of(runtime: DeviceRuntime):
    return [DEVICE_INFO] + ([POINT_ACCESS] if runtime.points else []) + ([TASK_EXECUTION] if runtime.tasks else [])


def stop(servers: list[SilaServer]) -> None:
    for server in servers:
        if server.running:
            server.stop(grace_period=1)
