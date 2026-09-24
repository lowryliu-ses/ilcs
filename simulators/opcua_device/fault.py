"""给运行中的 OPC UA 模拟设备注入故障、查看状态。在模拟器容器里执行，沿用容器的 SIM_* 配置与证书：

    docker compose exec opcua-sim-calender python simulators/opcua_device/fault.py state
    docker compose exec opcua-sim-calender python simulators/opcua_device/fault.py lost_receipt
    docker compose exec opcua-sim-calender python simulators/opcua_device/fault.py offline 30
    docker compose exec opcua-sim-calender python simulators/opcua_device/fault.py none

调的是 Objects/ILCS/SimulatorControl.SetFault，和任何 OPC UA 客户端一样（用试点客户端证书加密连接）。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulators.common.device import FAULT_MODES  # noqa: E402
from simulators.opcua_device.server import CLIENT_NAME, CLIENT_URI, CONTRACT  # noqa: E402


def main() -> int:
    env = os.environ.get
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=["state", *FAULT_MODES])
    parser.add_argument("parameter", nargs="?", default="0", help="秒数等参数，按模式解释")
    parser.add_argument("--host", default=env("SIM_FAULT_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(env("SIM_PORT", "4840")))
    parser.add_argument("--device-id", default=env("SIM_DEVICE_ID", "SIM-DEVICE-01"))
    parser.add_argument("--cert-dir", default=env("SIM_CERT_DIR", "./opcua-certs"))
    parser.add_argument("--insecure", action="store_true", default=env("SIM_INSECURE", "0") == "1")
    args = parser.parse_args()
    logging.getLogger("asyncua").setLevel(logging.ERROR)

    from asyncua import ua
    from asyncua.crypto.security_policies import SecurityPolicyBasic256Sha256
    from asyncua.crypto.uacrypto import CertProperties
    from asyncua.sync import Client

    client = Client(f"opc.tcp://{args.host}:{args.port}/ilcs/", timeout=10)
    client.application_uri = CLIENT_URI
    if not args.insecure:
        directory = Path(args.cert_dir)
        client.set_security(
            SecurityPolicyBasic256Sha256, CertProperties(str(directory / f"{CLIENT_NAME}.crt"), "pem"),
            CertProperties(str(directory / f"{CLIENT_NAME}.key"), "pem"),
            server_certificate=CertProperties(str(directory / f"{args.device_id}.crt"), "pem"),
            mode=ua.MessageSecurityMode.SignAndEncrypt,
        )
    client.connect()
    try:
        index = client.get_namespace_index(CONTRACT["namespace"])
        control = client.nodes.objects.get_child([f"{index}:{name}" for name in CONTRACT["simulator_control"]["path"]])
        if args.mode == "state":
            raw = control.get_child(f"{index}:SimulatorState").read_value()
        else:
            raw = control.call_method(f"{index}:SetFault", args.mode, str(args.parameter))
        print(json.dumps(json.loads(raw), ensure_ascii=False, indent=2))
    finally:
        client.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
