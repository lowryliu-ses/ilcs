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
import sys
import threading
import time
from pathlib import Path
from uuid import UUID, uuid5, NAMESPACE_URL

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:  # 直接运行（容器）时也能找到 simulators 包
    sys.path.insert(0, str(ROOT))

from cryptography import x509  # noqa: E402
from sila2.framework import Feature  # noqa: E402
from sila2.framework.errors.defined_execution_error import DefinedExecutionError  # noqa: E402
from sila2.server import FeatureImplementationBase, SilaServer  # noqa: E402

from simulators.common.certs import self_signed_certificate  # noqa: E402
from simulators.common.device import DeviceRejected, ReceiptLost, SimulatedDevice  # noqa: E402
from simulators.common.runtime import (  # noqa: E402
    build_device, configure_logging, device_arguments, serve_forever,
)

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


def sila_certificate(server_uuid: UUID, host_name: str) -> tuple[bytes, bytes]:
    """自签证书，另附 SiLA 规定的服务器 UUID 扩展。sila2 自带的生成器只写 IP SAN，按服务名连接过不了校验。"""
    return self_signed_certificate(
        host_name, "ILCS SiLA 2 Simulator",
        extensions=(x509.UnrecognizedExtension(
            x509.ObjectIdentifier("1.3.6.1.4.1.58583"), str(server_uuid).encode("ascii")),),
    )


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
        key, cert = sila_certificate(self.server_uuid, self.args.host_name)
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
    device_arguments(parser, default_port=50052)
    parser.add_argument("--cert-dir", default=env("SIM_CERT_DIR", "./sila-certs"))
    parser.add_argument("--insecure", action="store_true", default=env("SIM_INSECURE", "0") == "1")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    configure_logging()
    args = parse(argv)
    device = build_device(args)
    runner = SimulatorRunner(args, device)
    runner.start()
    serve_forever(device, args.tick_seconds, runner.stop)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
