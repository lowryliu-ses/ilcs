"""给运行中的文本命令模拟设备注入故障、查看状态。在模拟器容器里执行，沿用容器的 SIM_* 配置：

    docker compose exec line-sim-oven python devices/simulators/line_device/fault.py state
    docker compose exec line-sim-oven python devices/simulators/line_device/fault.py interlock      # 门开：DOOR? → OPEN
    docker compose exec line-sim-arm  python devices/simulators/line_device/fault.py lost_receipt   # play 了但不回复
    docker compose exec line-sim-oven python devices/simulators/line_device/fault.py none

模式见 devices/simulators/README.md「故障注入」。
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulators.common.device import FAULT_MODES  # noqa: E402
from simulators.common.linefault import send, show  # noqa: E402


def main() -> int:
    env = os.environ.get
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=["state", *FAULT_MODES])
    parser.add_argument("parameter", nargs="?", default="0")
    parser.add_argument("--port", type=int, default=int(env("SIM_PORT", "4001")))
    parser.add_argument("--dialect", default=env("SIM_DIALECT", "oven"))
    args = parser.parse_args()
    ur = args.dialect == "ur"
    line = "SIM:STATE?" if args.mode == "state" else f"SIM:FAULT {args.mode} {args.parameter}"
    show(send("127.0.0.1", args.port, line, newline=b"\n" if ur else b"\r\n", greeting=ur))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
