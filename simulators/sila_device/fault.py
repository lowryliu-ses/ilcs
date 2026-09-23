"""给运行中的 SiLA 2 模拟设备注入故障、查看状态。在模拟器容器里执行，沿用容器的 SIM_* 配置：

    docker compose exec sila-sim-lh python simulators/sila_device/fault.py state
    docker compose exec sila-sim-lh python simulators/sila_device/fault.py lost_receipt
    docker compose exec sila-sim-lh python simulators/sila_device/fault.py offline 30
    docker compose exec sila-sim-lh python simulators/sila_device/fault.py none

模式见 README「故障注入」。走的是 SimulatorControl 特性，和任何 SiLA 2 客户端调用一样。
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

MODES = ["none", "offline", "slow_submit", "lost_receipt", "no_dedup", "fail", "partial", "stuck",
         "interlock", "busy", "clock_skew"]


def main() -> int:
    env = os.environ.get
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=["state", *MODES])
    parser.add_argument("parameter", nargs="?", default="0", help="秒数等参数，按模式解释")
    parser.add_argument("--host", default=env("SIM_HOST_NAME", "localhost"))
    parser.add_argument("--port", type=int, default=int(env("SIM_PORT", "50052")))
    parser.add_argument("--device-id", default=env("SIM_DEVICE_ID", "SIM-DEVICE-01"))
    parser.add_argument("--cert-dir", default=env("SIM_CERT_DIR", "./sila-certs"))
    parser.add_argument("--insecure", action="store_true", default=env("SIM_INSECURE", "0") == "1")
    args = parser.parse_args()

    from sila2.client import SilaClient

    if args.insecure:
        client = SilaClient(args.host, args.port, insecure=True)
    else:
        ca = Path(args.cert_dir) / f"{args.device_id}.crt"
        client = SilaClient(args.host, args.port, root_certs=ca.read_bytes())
    control = client.SimulatorControl
    if args.mode == "state":
        raw = control.SimulatorState.get()
    else:
        raw = control.SetFault(Mode=args.mode, Parameter=str(args.parameter)).StateJson
    print(json.dumps(json.loads(raw), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
