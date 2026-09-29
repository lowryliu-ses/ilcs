"""OPC UA 模拟设备共用的安全配置：服务器应用实例证书、试点用 ILCS 客户端证书、只信任这张客户端证书。

证书目录里没有就生成（已有就沿用：系统侧 server_certificate 钉住的就是它）：
- `<设备ID>.crt/.key`：服务器证书，SAN 带主机名与应用 URI；
- `ilcs-client.crt/.key` + `ilcs-client.json`：试点用客户端证书与描述文件（系统侧 credential_ref 指向它），
  同一目录下的几台 OPC UA 模拟设备共用这一张。
"""
from __future__ import annotations

import json
from pathlib import Path

from asyncua import ua
from cryptography import x509

from .certs import ensure_certificate, self_signed_certificate

CLIENT_NAME = "ilcs-client"
CLIENT_URI = "urn:ilcs:client"


class ServerSecurity:
    def __init__(self, cert_dir: str, device_id: str, host_name: str, application_uri: str, organization: str):
        directory = Path(cert_dir)
        self.key, self.cert = ensure_certificate(directory, device_id, lambda: self_signed_certificate(
            host_name, organization, application_uri=application_uri,
        ))
        client_key, client_cert = ensure_certificate(directory, CLIENT_NAME, lambda: self_signed_certificate(
            "ilcs", "ILCS", application_uri=CLIENT_URI, client=True,
        ))
        descriptor = directory / f"{CLIENT_NAME}.json"
        if not descriptor.exists():
            descriptor.write_text(json.dumps({"certificate": client_cert.name, "private_key": client_key.name}))
        trusted = x509.load_pem_x509_certificate(client_cert.read_bytes())
        self.trusted = trusted.fingerprint(trusted.signature_hash_algorithm)

    async def validate(self, certificate: x509.Certificate, _description) -> None:
        if certificate.fingerprint(certificate.signature_hash_algorithm) != self.trusted:
            from asyncua.ua.uaerrors import ServiceError

            raise ServiceError(ua.StatusCodes.BadCertificateUntrusted)

    async def apply(self, server, *, allow_writes: bool = False) -> None:
        """allow_writes：证书受信任的客户端可以写变量（PLC 的 OPC UA 服务器就是这样）；增删节点仍只限管理员。"""
        ruleset = None
        if allow_writes:
            from asyncua.crypto.permission_rules import SimpleRoleRuleset
            from asyncua.server.users import UserRole

            ruleset = SimpleRoleRuleset()
            ruleset._permission_dict[UserRole.User].add(ua.NodeId(ua.ObjectIds.WriteRequest_Encoding_DefaultBinary))
        server.set_security_policy([
            ua.SecurityPolicyType.Basic256Sha256_SignAndEncrypt, ua.SecurityPolicyType.Basic256Sha256_Sign,
        ], permission_ruleset=ruleset)
        await server.load_certificate(str(self.cert), format="pem")
        await server.load_private_key(str(self.key), format="pem")
        server.set_certificate_validator(self.validate)


async def stop_hard(server) -> None:
    """模拟断网：先关监听（新连接立刻被拒）、断开已有连接，再走 asyncua 的收尾。

    asyncua 的 `Server.stop()` 先等客户端处理任务收完才关监听：客户端一直在重连时它就一直关不掉，
    模拟设备也就「离线」不了。真设备断网时新连接是马上被拒的。
    """
    listener = getattr(getattr(server, "bserver", None), "_server", None)
    if listener is not None:
        listener.close()
    for transport in list(getattr(getattr(server, "iserver", None), "asyncio_transports", []) or []):
        transport.close()
    await server.stop()
