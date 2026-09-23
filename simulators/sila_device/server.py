"""ILCS SiLA 2 模拟设备。

在系统外部独立运行，用标准 SiLA 2（gRPC + TLS）暴露 `TaskExecution` 契约，外加仅模拟器
才有的 `SimulatorControl` 故障注入特性。系统侧用 `sila2_v1` 驱动接入，和接一台真设备走同一条路。

    python simulators/sila_device/server.py --device-id SIM-LH-01 --profile liquid_handler \\
        --port 50052 --cert-dir /run/secrets/ilcs/sila --host-name sila-sim-lh

未提供证书时生成自签证书并写到 --cert-dir（系统用 ca_file 指向其中的 .crt）；--insecure 只用于本机测试。
配了 ILCS_URL 与服务凭据时，运行中的任务会按周期把遥测推到 ILCS 的遥测上报接口。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import threading
import time
import urllib.request
from pathlib import Path
from uuid import UUID, uuid5, NAMESPACE_URL

from sila2.framework import Feature
from sila2.framework.errors.defined_execution_error import DefinedExecutionError
from sila2.server import FeatureImplementationBase, SilaServer

try:  # 作为包导入（测试）或直接运行（容器）都要能找到设备模型
    from .device import DeviceRejected, ReceiptLost, SimulatedDevice, load_material_map
except ImportError:  # pragma: no cover
    from device import DeviceRejected, ReceiptLost, SimulatedDevice, load_material_map

ROOT = Path(__file__).resolve().parents[2]
FEATURES = ROOT / "contracts" / "sila2"
TASK_FEATURE = Feature((FEATURES / "TaskExecution.sila.xml").read_text(encoding="utf-8"))
CONTROL_FEATURE = Feature((FEATURES / "SimulatorControl.sila.xml").read_text(encoding="utf-8"))
log = logging.getLogger("ilcs.sila-sim")


def _reject(error: DeviceRejected) -> DefinedExecutionError:
    return DefinedExecutionError(TASK_FEATURE.defined_execution_errors[error.identifier], error.message)


class TaskExecutionImpl(FeatureImplementationBase):
    def __init__(self, parent_server: SilaServer, device: SimulatedDevice):
        super().__init__(parent_server)
        self.device = device

    def _run(self, call):
        try:
            return json.dumps(call(), ensure_ascii=False)
        except DeviceRejected as error:
            raise _reject(error) from error
        except ReceiptLost as error:
            # 未定义执行错误：客户端收到的是「出错了」，但不知道设备其实已经在动
            raise RuntimeError(str(error)) from error

    def SubmitTask(self, CommandId, TaskType, Capability, ParametersJson, ContextJson, *, metadata):
        return self._run(lambda: self.device.submit(
            CommandId, TaskType or "dispatch", Capability,
            json.loads(ParametersJson or "{}"), json.loads(ContextJson or "{}"),
        ))

    def QueryTask(self, CommandId, *, metadata):
        return self._run(lambda: self.device.query(CommandId))

    def HoldTask(self, CommandId, TargetCommandId, *, metadata):
        return self._run(lambda: self.device.hold(CommandId, TargetCommandId))

    def AbortTask(self, CommandId, TargetCommandId, *, metadata):
        return self._run(lambda: self.device.abort(CommandId, TargetCommandId))

    def get_DeviceIdentity(self, *, metadata):
        return json.dumps(self.device.identity(), ensure_ascii=False)


class SimulatorControlImpl(FeatureImplementationBase):
    def __init__(self, parent_server: SilaServer, device: SimulatedDevice, runner: "SimulatorRunner"):
        super().__init__(parent_server)
        self.device = device
        self.runner = runner

    def SetFault(self, Mode, Parameter, *, metadata):
        parameter = float(Parameter or 0)
        if Mode == "offline":
            # 真的断开：停掉 gRPC 服务 N 秒再恢复。设备内部状态（在跑的任务）保持不变
            self.runner.go_offline(parameter or 5)
            return json.dumps({**self.device.state(), "fault": "offline", "seconds": parameter or 5})
        return json.dumps(self.device.set_fault(Mode, parameter), ensure_ascii=False)

    def get_SimulatorState(self, *, metadata):
        return json.dumps(self.device.state(), ensure_ascii=False)


def self_signed_certificate(server_uuid: UUID, host_name: str) -> tuple[bytes, bytes]:
    """自签证书：主机名写进 DNS SAN，另附 SiLA 规定的服务器 UUID 扩展。

    sila2 自带的生成器只把「生成时解析到的 IP」写进 SAN：系统按服务名（如 sila-sim-lh）连接时
    主机名校验永远不通过，容器重启换了 IP 证书也就作废。
    """
    import ipaddress
    import socket
    from datetime import datetime, timedelta, timezone

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    names: list[x509.GeneralName] = []
    try:
        names.append(x509.IPAddress(ipaddress.ip_address(host_name)))
    except ValueError:
        names.append(x509.DNSName(host_name))
        try:  # 同时写入当前解析到的地址，便于按 IP 直连调试
            for info in socket.getaddrinfo(host_name, None):
                address = ipaddress.ip_address(info[4][0])
                if x509.IPAddress(address) not in names:
                    names.append(x509.IPAddress(address))
        except OSError:
            pass
    subject = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, host_name),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "ILCS SiLA 2 Simulator"),
    ])
    moment = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject).issuer_name(subject).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(moment - timedelta(hours=1)).not_valid_after(moment + timedelta(days=825))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        .add_extension(
            x509.UnrecognizedExtension(x509.ObjectIdentifier("1.3.6.1.4.1.58583"), str(server_uuid).encode("ascii")),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    return (
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                          serialization.NoEncryption()),
        cert.public_bytes(serialization.Encoding.PEM),
    )


class TelemetryPusher:
    """把运行中任务的遥测推到 ILCS 的遥测上报接口（服务身份认证）。"""

    def __init__(self, base_url: str, station_id: str, source: str, secret: str):
        self.url = f"{base_url.rstrip('/')}/api/runtime/stations/{station_id}/telemetry"
        self.headers = {
            "Content-Type": "application/json", "X-Service-Source": source, "X-Service-Secret": secret,
        }

    def __call__(self, event_id: str, points: list[dict], command_id: str) -> None:
        body = json.dumps({"event_id": event_id, "command_id": command_id, "points": points}).encode()
        request = urllib.request.Request(self.url, data=body, headers=self.headers, method="POST")
        with urllib.request.urlopen(request, timeout=5):
            pass


class SimulatorRunner:
    def __init__(self, args: argparse.Namespace, device: SimulatedDevice):
        self.args = args
        self.device = device
        self.server: SilaServer | None = None
        self.server_uuid = uuid5(NAMESPACE_URL, f"ilcs-sila-sim:{args.device_id}")
        self.lock = threading.Lock()
        self.private_key, self.cert = self._credentials()

    def _credentials(self) -> tuple[bytes | None, bytes | None]:
        if self.args.insecure:
            return None, None
        directory = Path(self.args.cert_dir)
        key_path = directory / f"{self.args.device_id}.key"
        cert_path = directory / f"{self.args.device_id}.crt"
        if key_path.exists() and cert_path.exists():
            return key_path.read_bytes(), cert_path.read_bytes()
        key, cert = self_signed_certificate(self.server_uuid, self.args.host_name)
        directory.mkdir(parents=True, exist_ok=True)
        key_path.write_bytes(key)
        key_path.chmod(0o600)
        cert_path.write_bytes(cert)
        log.info("已生成自签证书 %s；ILCS 适配器 ca_file 指向它", cert_path)
        return key, cert

    def build(self) -> SilaServer:
        server = SilaServer(
            server_name=self.args.device_id[:255], server_type="ILCSSimulator",
            server_description=f"ILCS SiLA 2 模拟设备（{self.device.profile}）",
            server_version="1.0", server_vendor_url="https://ses.ai", server_uuid=self.server_uuid,
        )
        server.set_feature_implementation(TASK_FEATURE, TaskExecutionImpl(server, self.device))
        server.set_feature_implementation(CONTROL_FEATURE, SimulatorControlImpl(server, self.device, self))
        return server

    def start(self) -> None:
        with self.lock:
            self.server = self.build()
            if self.args.insecure:
                self.server.start_insecure(self.args.address, self.args.port, enable_discovery=False)
            else:
                self.server.start(
                    self.args.address, self.args.port, private_key=self.private_key,
                    cert_chain=self.cert, enable_discovery=False,
                )
        log.info("SiLA 2 模拟设备 %s 已启动：%s:%s", self.args.device_id, self.args.address, self.args.port)

    def stop(self) -> None:
        with self.lock:
            if self.server is not None and self.server.running:
                self.server.stop(grace_period=0)
            self.server = None

    def go_offline(self, seconds: float) -> None:
        def cycle():
            time.sleep(0.2)  # 先把 SetFault 的应答送回去
            self.stop()
            log.info("模拟离线 %.0f s", seconds)
            time.sleep(seconds)
            self.start()

        threading.Thread(target=cycle, daemon=True).start()


def parse(argv=None) -> argparse.Namespace:
    env = os.environ.get
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--device-id", default=env("SIM_DEVICE_ID", "SIM-DEVICE-01"))
    parser.add_argument("--profile", default=env("SIM_PROFILE", "generic"), choices=["liquid_handler", "cycler", "generic"])
    parser.add_argument("--address", default=env("SIM_ADDRESS", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(env("SIM_PORT", "50052")))
    parser.add_argument("--host-name", default=env("SIM_HOST_NAME", "localhost"), help="写进证书的主机名，须与 ILCS 连接用的主机名一致")
    parser.add_argument("--cert-dir", default=env("SIM_CERT_DIR", "./sila-certs"))
    parser.add_argument("--insecure", action="store_true", default=env("SIM_INSECURE", "0") == "1")
    parser.add_argument("--channels", type=int, default=int(env("SIM_CHANNELS", "1")))
    parser.add_argument("--task-seconds", type=float, default=float(env("SIM_TASK_SECONDS", "5")))
    parser.add_argument("--material-map", default=env("SIM_MATERIAL_MAP", ""),
                        help='组分 → 物料映射，如 {"electrolyte": {"material": "电解液 LP57", "unit": "mL", "factor": 0.001}}')
    parser.add_argument("--tick-seconds", type=float, default=float(env("SIM_TICK_SECONDS", "1")))
    return parser.parse_args(argv)


def main(argv=None) -> int:
    logging.basicConfig(level=os.environ.get("SIM_LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    args = parse(argv)
    sink = None
    if os.environ.get("ILCS_URL") and os.environ.get("ILCS_SERVICE_SOURCE"):
        sink = TelemetryPusher(
            os.environ["ILCS_URL"], os.environ.get("ILCS_STATION_ID", args.device_id),
            os.environ["ILCS_SERVICE_SOURCE"], os.environ.get("ILCS_SERVICE_SECRET", ""),
        )
    device = SimulatedDevice(
        args.device_id, args.profile, channels=args.channels, task_seconds=args.task_seconds,
        material_map=load_material_map(args.material_map), telemetry_sink=sink,
    )
    runner = SimulatorRunner(args, device)
    runner.start()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    while not stop.wait(args.tick_seconds):
        device.tick()
    runner.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
