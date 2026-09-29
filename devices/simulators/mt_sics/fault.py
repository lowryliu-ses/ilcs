"""给运行中的 MT-SICS 天平模拟设备注入故障、改秤盘重量、查看状态：

    docker compose exec mtsics-sim-balance python devices/simulators/mt_sics/fault.py state
    docker compose exec mtsics-sim-balance python devices/simulators/mt_sics/fault.py busy       # S → "S I"
    docker compose exec mtsics-sim-balance python devices/simulators/mt_sics/fault.py fail       # S → "S +"（超载）
    docker compose exec mtsics-sim-balance python devices/simulators/mt_sics/fault.py load 0.0149   # 秤盘上换一片
    docker compose exec mtsics-sim-balance python devices/simulators/mt_sics/fault.py none
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
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=["state", "load", *FAULT_MODES])
    parser.add_argument("parameter", nargs="?", default="0")
    parser.add_argument("--port", type=int, default=int(os.environ.get("SIM_PORT", "4305")))
    args = parser.parse_args()
    if args.mode == "state":
        line = "SIM:STATE?"
    elif args.mode == "load":
        line = f"SIM:LOAD {args.parameter}"
    else:
        line = f"SIM:FAULT {args.mode} {args.parameter}"
    show(send("127.0.0.1", args.port, line))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
