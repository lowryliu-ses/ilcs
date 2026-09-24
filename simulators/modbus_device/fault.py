"""给运行中的 Modbus TCP 模拟设备注入故障、查看状态。在模拟器容器里执行，沿用容器的 SIM_* 配置：

    docker compose exec modbus-sim-mixer python simulators/modbus_device/fault.py state
    docker compose exec modbus-sim-mixer python simulators/modbus_device/fault.py lost_receipt
    docker compose exec modbus-sim-mixer python simulators/modbus_device/fault.py offline 30
    docker compose exec modbus-sim-mixer python simulators/modbus_device/fault.py none

写的是模拟器专有的 simulator_control 寄存器（故障码、参数、触发），读的是 simulator_state 诊断区，
和任何 Modbus 主站访问一样。模式见 simulators/README.md「故障注入」。
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulators.common.device import FAULT_MODES  # noqa: E402
from simulators.modbus_device.server import BLOCKS, decode, encode_field  # noqa: E402


def main() -> int:
    env = os.environ.get
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=["state", *FAULT_MODES])
    parser.add_argument("parameter", nargs="?", default="0", help="秒数等参数，按模式解释")
    parser.add_argument("--host", default=env("SIM_FAULT_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(env("SIM_PORT", "5020")))
    args = parser.parse_args()

    from pymodbus.client import ModbusTcpClient

    client = ModbusTcpClient(args.host, port=args.port, timeout=3, retries=0)
    if not client.connect():
        print(f"连不上 {args.host}:{args.port}", file=sys.stderr)
        return 2
    try:
        if args.mode != "state":
            control = BLOCKS["simulator_control"]
            fields = control["fields"]
            trigger = client.read_holding_registers(control["address"] + fields["trigger"][0], count=1).registers[0]
            words = encode_field(fields["fault"], FAULT_MODES.index(args.mode)) + \
                encode_field(fields["parameter"], float(args.parameter))
            client.write_registers(control["address"], words)
            client.write_registers(control["address"] + fields["trigger"][0], [trigger % 65535 + 1])
            if args.mode == "offline":
                print(json.dumps({"fault": "offline", "seconds": float(args.parameter) or 5}, ensure_ascii=False))
                return 0
            time.sleep(0.3)  # 等设备处理完触发、刷新诊断区
        spec = BLOCKS["simulator_state"]
        registers: list[int] = []
        for start in range(0, spec["length"], 120):
            count = min(120, spec["length"] - start)
            registers.extend(client.read_holding_registers(spec["address"] + start, count=count).registers)
        length = decode("simulator_state", registers)["length"]
        raw = b"".join(word.to_bytes(2, "big") for word in registers[1:])[:length]
        print(json.dumps(json.loads(raw.decode("utf-8") or "{}"), ensure_ascii=False, indent=2))
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
