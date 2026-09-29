"""给运行中的中间库模拟设备注入故障、查看作业表。在模拟器容器里执行，沿用容器的 SIM_* 配置：

    docker compose exec sql-sim-slurry-b python devices/simulators/sql_device/fault.py state
    docker compose exec sql-sim-slurry-b python devices/simulators/sql_device/fault.py interlock   # 新作业改 rejected
    docker compose exec sql-sim-slurry-b python devices/simulators/sql_device/fault.py offline 30  # 停止处理与心跳
    docker compose exec sql-sim-slurry-b python devices/simulators/sql_device/fault.py none

写的是设备表的 sim_fault 列（模拟器专用）；设备侧每个周期读一次。
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulators.common.device import FAULT_MODES  # noqa: E402


def main() -> int:
    env = os.environ.get
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=["state", *FAULT_MODES])
    parser.add_argument("parameter", nargs="?", default="0")
    parser.add_argument("--url", default=env("SIM_DATABASE_URL", "sqlite:///./exchange.db"))
    parser.add_argument("--device-id", default=env("SIM_DEVICE_ID", "SIM-DEVICE-01"))
    args = parser.parse_args()
    from sqlalchemy import create_engine, text

    engine = create_engine(args.url)
    with engine.begin() as connection:
        if args.mode != "state":
            # 同一个故障连写两次不会再触发：先清空再写
            connection.execute(text("UPDATE ilcs_device SET sim_fault = '' WHERE device_id = :d"), {"d": args.device_id})
        if args.mode != "state":
            connection.execute(text("UPDATE ilcs_device SET sim_fault = :f WHERE device_id = :d"),
                               {"f": f"{args.mode} {args.parameter}", "d": args.device_id})
        device = connection.execute(text("SELECT * FROM ilcs_device WHERE device_id = :d"), {"d": args.device_id}).mappings().first()
        jobs = connection.execute(text("SELECT command_id, task_type, state, error FROM ilcs_jobs ORDER BY created_at DESC")).mappings().fetchmany(20)
    print(json.dumps({"device": dict(device or {}), "recent_jobs": [dict(row) for row in jobs]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
