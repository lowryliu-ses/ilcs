"""ILCS OPC UA 模拟设备。

在系统外部独立运行的 OPC UA 服务器，按 `contracts/opcua/TaskExecution.json` 在 `Objects/ILCS/TaskExecution`
上提供任务方法与设备身份变量，外加仅模拟器才有的 `Objects/ILCS/SimulatorControl` 故障注入。系统侧用
`opcua_v1` 驱动接入，和接一台真设备走同一条路。

    python simulators/opcua_device/server.py --device-id SIM-CAL-01 --port 4840 \\
        --cert-dir /run/secrets/ilcs/opcua --host-name opcua-sim-calender

加密模式（默认 Basic256Sha256 + SignAndEncrypt）首次启动在 --cert-dir 生成：
- `<设备ID>.crt/.key`：服务器应用实例证书（系统侧 server_certificate 钉住它）；
- `ilcs-client.crt/.key` 与描述文件 `ilcs-client.json`：试点用的 ILCS 客户端证书（系统侧 credential_ref 指向描述文件）。
服务器只接受 --cert-dir 里这张客户端证书，其他客户端证书一律 BadCertificateUntrusted。
--insecure 只开放 None 安全策略，仅用于本机测试。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:  # 直接运行（容器）时也能找到 simulators 包
    sys.path.insert(0, str(ROOT))

from asyncua import Server, ua  # noqa: E402
from cryptography import x509  # noqa: E402

from simulators.common.certs import ensure_certificate, self_signed_certificate  # noqa: E402
from simulators.common.device import DeviceRejected, ReceiptLost, SimulatedDevice  # noqa: E402
from simulators.common.runtime import build_device, configure_logging, device_arguments, serve_forever  # noqa: E402

CONTRACT = json.loads((ROOT / "contracts" / "opcua" / "TaskExecution.json").read_text(encoding="utf-8"))
REJECTION_CODES = {
    identifier: getattr(ua.StatusCodes, code) for code, identifier in CONTRACT["rejections"].items()
}
CLIENT_NAME = "ilcs-client"
CLIENT_URI = "urn:ilcs:client"
log = logging.getLogger("ilcs.opcua-sim")


def _string(value: str) -> list[ua.Variant]:
    return [ua.Variant(value, ua.VariantType.String)]


class TaskMethods:
    """方法回调在 asyncua 的线程池里执行：应答迟到（slow_submit）不会卡住服务器事件循环。"""

    def __init__(self, device: SimulatedDevice, runner: "SimulatorRunner"):
        self.device = device
        self.runner = runner

    def _run(self, call):
        try:
            return _string(json.dumps(call(), ensure_ascii=False))
        except DeviceRejected as error:
            log.info("拒绝：%s %s", error.identifier, error.message)
            return ua.StatusCode(REJECTION_CODES[error.identifier])
        except ReceiptLost:
            # 设备已经动作，服务器侧出错：客户端只看到 BadUnexpectedError，不知道动没动
            return ua.StatusCode(ua.StatusCodes.BadUnexpectedError)

    @staticmethod
    def _values(arguments) -> list[str]:
        return [str(argument.Value or "") for argument in arguments]

    def submit(self, parent, *arguments):
        command_id, task_type, capability, parameters, context = self._values(arguments)
        return self._run(lambda: self.device.submit(
            command_id, task_type or "dispatch", capability, json.loads(parameters or "{}"), json.loads(context or "{}"),
        ))

    def query(self, parent, *arguments):
        (command_id,) = self._values(arguments)
        return self._run(lambda: self.device.query(command_id))

    def hold(self, parent, *arguments):
        command_id, target = self._values(arguments)
        return self._run(lambda: self.device.hold(command_id, target))

    def abort(self, parent, *arguments):
        command_id, target = self._values(arguments)
        return self._run(lambda: self.device.abort(command_id, target))

    def set_fault(self, parent, *arguments):
        mode, parameter = self._values(arguments)
        seconds = float(parameter or 0)
        if mode == "offline":
            self.runner.go_offline(seconds or 5)
            return _string(json.dumps({**self.device.state(), "fault": "offline", "seconds": seconds or 5}))
        try:
            return _string(json.dumps(self.device.set_fault(mode, seconds), ensure_ascii=False))
        except ValueError:
            return ua.StatusCode(ua.StatusCodes.BadInvalidArgument)


class SimulatorRunner:
    def __init__(self, args: argparse.Namespace, device: SimulatedDevice):
        self.args = args
        self.device = device
        self.methods = TaskMethods(device, self)
        self.endpoint = f"opc.tcp://{args.address}:{args.port}/ilcs/"
        self.application_uri = f"urn:ilcs:simulator:{args.device_id}"
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.server: Server | None = None
        self.variables: dict = {}
        self.lock = threading.Lock()
        self.heartbeat = 0
        self.credentials = None if args.insecure else self._credentials()

    def _credentials(self) -> dict:
        directory = Path(self.args.cert_dir)
        server_key, server_cert = ensure_certificate(directory, self.args.device_id, lambda: self_signed_certificate(
            self.args.host_name, "ILCS OPC UA Simulator", application_uri=self.application_uri,
        ))
        client_key, client_cert = ensure_certificate(directory, CLIENT_NAME, lambda: self_signed_certificate(
            "ilcs", "ILCS", application_uri=CLIENT_URI, client=True,
        ))
        descriptor = directory / f"{CLIENT_NAME}.json"
        if not descriptor.exists():
            descriptor.write_text(json.dumps({"certificate": client_cert.name, "private_key": client_key.name}))
        trusted = x509.load_pem_x509_certificate(client_cert.read_bytes())
        return {"key": server_key, "cert": server_cert, "trusted": trusted.fingerprint(trusted.signature_hash_algorithm)}

    def _call(self, coroutine, timeout: float = 30):
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(timeout)

    async def _validate(self, certificate: x509.Certificate, _description) -> None:
        if certificate.fingerprint(certificate.signature_hash_algorithm) != self.credentials["trusted"]:
            from asyncua.ua.uaerrors import ServiceError

            raise ServiceError(ua.StatusCodes.BadCertificateUntrusted)

    async def _build(self) -> Server:
        server = Server()
        await server.init()
        server.set_endpoint(self.endpoint)
        server.set_server_name(f"ILCS OPC UA 模拟设备 {self.args.device_id}")
        await server.set_application_uri(self.application_uri)
        if self.credentials is None:
            server.set_security_policy([ua.SecurityPolicyType.NoSecurity])
        else:
            server.set_security_policy([
                ua.SecurityPolicyType.Basic256Sha256_SignAndEncrypt, ua.SecurityPolicyType.Basic256Sha256_Sign,
            ])
            await server.load_certificate(str(self.credentials["cert"]), format="pem")
            await server.load_private_key(str(self.credentials["key"]), format="pem")
            server.set_certificate_validator(self._validate)
        index = await server.register_namespace(CONTRACT["namespace"])
        root = await server.nodes.objects.add_object(index, CONTRACT["path"][0])
        task = await root.add_object(index, CONTRACT["path"][1])
        handlers = {
            "SubmitTask": self.methods.submit, "QueryTask": self.methods.query,
            "HoldTask": self.methods.hold, "AbortTask": self.methods.abort,
        }
        for name, spec in CONTRACT["methods"].items():
            await task.add_method(index, name, handlers[name], [ua.VariantType.String] * len(spec["inputs"]),
                                  [ua.VariantType.String])
        self.variables["identity"] = await task.add_variable(index, "DeviceIdentity", "{}", ua.VariantType.String)
        self.variables["heartbeat"] = await task.add_variable(index, "Heartbeat", 0, ua.VariantType.UInt32)
        control_spec = CONTRACT["simulator_control"]
        control = await root.add_object(index, control_spec["path"][1])
        await control.add_method(index, "SetFault", self.methods.set_fault, [ua.VariantType.String] * 2,
                                 [ua.VariantType.String])
        self.variables["state"] = await control.add_variable(index, "SimulatorState", "{}", ua.VariantType.String)
        await server.start()
        return server

    def refresh(self) -> None:
        if self.server is None or not self.variables:
            return
        self.heartbeat += 1
        identity = json.dumps(self.device.identity(), ensure_ascii=False)
        state = json.dumps(self.device.state(), ensure_ascii=False)

        async def write():
            await self.variables["identity"].write_value(identity, ua.VariantType.String)
            await self.variables["heartbeat"].write_value(self.heartbeat, ua.VariantType.UInt32)
            await self.variables["state"].write_value(state, ua.VariantType.String)

        try:
            self._call(write(), timeout=5)
        except Exception:  # 离线切换中：下一轮再写
            pass

    def start(self) -> None:
        with self.lock:
            self.server = self._call(self._build())
        self.refresh()
        log.info("OPC UA 模拟设备 %s 已启动：%s", self.args.device_id, self.endpoint)

    def stop(self) -> None:
        with self.lock:
            server, self.server, self.variables = self.server, None, {}
            if server is not None:
                self._call(server.stop())

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
    device_arguments(parser, default_port=4840)
    parser.add_argument("--cert-dir", default=env("SIM_CERT_DIR", "./opcua-certs"))
    parser.add_argument("--insecure", action="store_true", default=env("SIM_INSECURE", "0") == "1")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    configure_logging()
    logging.getLogger("asyncua").setLevel(logging.WARNING)
    args = parse(argv)
    device = build_device(args)
    runner = SimulatorRunner(args, device)
    runner.start()
    serve_forever(device, args.tick_seconds, runner.stop, on_tick=runner.refresh)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
