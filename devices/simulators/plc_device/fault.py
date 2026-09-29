"""给运行中的 PLC 模拟设备注入故障、查看状态。在模拟器容器里执行，沿用容器的 SIM_* 配置与证书：

    docker compose exec plc-sim-mixer  python devices/simulators/plc_device/fault.py state
    docker compose exec plc-sim-coater python devices/simulators/plc_device/fault.py interlock   # SafetyOk → false
    docker compose exec plc-sim-coater python devices/simulators/plc_device/fault.py fail        # 作业报警（ErrorCode 17）
    docker compose exec plc-sim-mixer  python devices/simulators/plc_device/fault.py none

写的是模拟器专用的 SimFault 点（OPC UA 节点 / Modbus 保持寄存器 900–919），和上位写点一样。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulators.common.device import FAULT_MODES  # noqa: E402
from simulators.common.runtime import cert_dir_default  # noqa: E402


def _opcua(args, text: str | None) -> dict:
    from asyncua import ua
    from asyncua.crypto.security_policies import SecurityPolicyBasic256Sha256
    from asyncua.crypto.uacrypto import CertProperties
    from asyncua.sync import Client

    from simulators.common.opcua import CLIENT_NAME, CLIENT_URI

    logging.getLogger("asyncua").setLevel(logging.ERROR)
    client = Client(f"opc.tcp://127.0.0.1:{args.port}/plc/", timeout=10)
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
        index = client.get_namespace_index("urn:ilcs:sim:plc")

        def node(name):
            return client.get_node(f"ns={index};s={args.machine}.{name}")

        if text is not None:
            node("SimFault").write_value(ua.DataValue(ua.Variant(text, ua.VariantType.String)))
            time.sleep(0.3)
        return {name: node(name).read_value() for name in ("State", "ErrorCode", "RemoteMode", "SafetyOk", "JobLatched", "SimFault")}
    finally:
        client.disconnect()


def _modbus(args, text: str | None) -> dict:
    from pymodbus.client import ModbusTcpClient

    client = ModbusTcpClient("127.0.0.1", port=args.port, timeout=3, retries=0)
    client.connect()
    try:
        if text is not None:
            raw = text.encode("ascii")[:40].ljust(40, b"\0")
            client.write_registers(900, [int.from_bytes(raw[i:i + 2], "big") for i in range(0, 40, 2)])
            time.sleep(0.3)
        words = client.read_holding_registers(0, count=4).registers
        return {"State": words[1], "ErrorCode": words[2], "RemoteMode": bool(words[3] & 1), "SafetyOk": bool(words[3] & 2)}
    finally:
        client.close()


def main() -> int:
    env = os.environ.get
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=["state", *FAULT_MODES])
    parser.add_argument("parameter", nargs="?", default="0")
    parser.add_argument("--protocol", default=env("SIM_PROTOCOL", "opcua"))
    parser.add_argument("--port", type=int, default=int(env("SIM_PORT", "4841")))
    parser.add_argument("--machine", default=env("SIM_MACHINE", "Line"))
    parser.add_argument("--device-id", default=env("SIM_DEVICE_ID", "SIM-DEVICE-01"))
    parser.add_argument("--cert-dir", default=env("SIM_CERT_DIR", cert_dir_default("opcua")))
    parser.add_argument("--insecure", action="store_true", default=env("SIM_INSECURE", "0") == "1")
    args = parser.parse_args()
    # 同一个故障连写两次不会再触发：先清空再写，保证「再注入一次」有效
    text = None if args.mode == "state" else f"{args.mode} {args.parameter}"
    handler = _opcua if args.protocol == "opcua" else _modbus
    if text is not None:
        handler(args, "")
    print(json.dumps(handler(args, text), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
