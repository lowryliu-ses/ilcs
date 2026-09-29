"""给运行中的 AGV 车队模拟设备注入故障、查看队列：

    docker compose exec fleet-sim python devices/simulators/fleet/fault.py state
    docker compose exec fleet-sim python devices/simulators/fleet/fault.py estop --robot AGV-01      # 急停
    docker compose exec fleet-sim python devices/simulators/fleet/fault.py lost_receipt --robot AGV-02
    docker compose exec fleet-sim python devices/simulators/fleet/fault.py none --robot AGV-01
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.request

MODES = ["none", "estop", "busy", "fail", "stuck", "slow_submit", "lost_receipt", "offline"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=["state", *MODES])
    parser.add_argument("parameter", nargs="?", default="0")
    parser.add_argument("--robot", default="AGV-01")
    parser.add_argument("--port", type=int, default=int(os.environ.get("SIM_PORT", "8080")))
    args = parser.parse_args()
    base = f"http://127.0.0.1:{args.port}/simulator"
    if args.mode == "state":
        request = urllib.request.Request(f"{base}/state")
    else:
        body = json.dumps({"robot": args.robot, "mode": args.mode, "parameter": float(args.parameter)}).encode()
        request = urllib.request.Request(f"{base}/fault", data=body, method="POST",
                                         headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as response:
        print(json.dumps(json.loads(response.read()), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
