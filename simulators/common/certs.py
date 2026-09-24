"""模拟设备的自签证书。

主机名写进 DNS SAN：系统按服务名（如 sila-sim-lh、gateway-sim-coater）连接时要通过主机名校验，
只写「生成时解析到的 IP」的证书在容器重启换 IP 后就作废。OPC UA 另要求 SAN 里带应用 URI。
"""
from __future__ import annotations

import ipaddress
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def _host_names(host_name: str) -> list[x509.GeneralName]:
    names: list[x509.GeneralName] = []
    try:
        names.append(x509.IPAddress(ipaddress.ip_address(host_name)))
    except ValueError:
        names.append(x509.DNSName(host_name))
        try:  # 同时写入当前解析到的地址，便于按 IP 直连调试
            for info in socket.getaddrinfo(host_name, None):
                address = x509.IPAddress(ipaddress.ip_address(info[4][0]))
                if address not in names:
                    names.append(address)
        except OSError:
            pass
    return names


def self_signed_certificate(
    host_name: str, organization: str, *, application_uri: str = "", client: bool = False,
    extensions: tuple[x509.ExtensionType, ...] = (),
) -> tuple[bytes, bytes]:
    """返回 (私钥 PEM, 证书 PEM)。application_uri 非空时按 OPC UA 应用实例证书的要求生成。"""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    names = _host_names(host_name)
    if application_uri:
        names.insert(0, x509.UniformResourceIdentifier(application_uri))
    subject = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, host_name),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, organization),
    ])
    usage = [ExtendedKeyUsageOID.CLIENT_AUTH if client else ExtendedKeyUsageOID.SERVER_AUTH]
    if application_uri:  # OPC UA 应用证书两端通用
        usage = [ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH]
    moment = datetime.now(timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject).issuer_name(subject).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(moment - timedelta(hours=1)).not_valid_after(moment + timedelta(days=825))
        .add_extension(x509.BasicConstraints(ca=not application_uri, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage(usage), critical=False)
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
    )
    if application_uri:
        builder = builder.add_extension(x509.KeyUsage(
            digital_signature=True, content_commitment=True, key_encipherment=True, data_encipherment=True,
            key_agreement=False, key_cert_sign=True, crl_sign=False, encipher_only=False, decipher_only=False,
        ), critical=False)
    for extension in extensions:
        builder = builder.add_extension(extension, critical=False)
    cert = builder.sign(key, hashes.SHA256())
    return (
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                          serialization.NoEncryption()),
        cert.public_bytes(serialization.Encoding.PEM),
    )


def ensure_certificate(directory: str | Path, name: str, generate) -> tuple[Path, Path]:
    """证书已存在就沿用（系统侧 ca_file 指向它，不能每次重启都换）；没有才生成。返回 (私钥, 证书) 路径。"""
    directory = Path(directory)
    key_path, cert_path = directory / f"{name}.key", directory / f"{name}.crt"
    if not (key_path.exists() and cert_path.exists()):
        key, cert = generate()
        directory.mkdir(parents=True, exist_ok=True)
        key_path.write_bytes(key)
        key_path.chmod(0o600)
        cert_path.write_bytes(cert)
    return key_path, cert_path
