"""各协议模拟器共用的启动参数、遥测推送与主循环。"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import threading
import urllib.request
from typing import Callable

from .device import PROFILES, SimulatedDevice, load_material_map


class TelemetryPusher:
    """把运行中任务的遥测推到 ILCS 的遥测上报接口（服务身份认证）。"""

    def __init__(self, base_url: str, station_id: str, source: str, secret: str):
        self.url = f"{base_url.rstrip('/')}/api/runtime/stations/{station_id}/telemetry"
        self.headers = {
            "Content-Type": "application/json", "X-Service-Source": source, "X-Service-Secret": secret,
        }

    def __call__(self, event_id: str, points: list[dict], command_id: str) -> None:
        body = json.dumps({"event_id": event_id, "command_id": command_id, "points": points}).encode()
        request = urllib.request.Request(self.url, data=body, headers=self.headers, method="POST")
        with urllib.request.urlopen(request, timeout=5):
            pass


def device_arguments(parser: argparse.ArgumentParser, *, default_port: int) -> argparse.ArgumentParser:
    """设备行为参数：各协议模拟器一致，环境变量同名（SIM_*）。"""
    env = os.environ.get
    parser.add_argument("--device-id", default=env("SIM_DEVICE_ID", "SIM-DEVICE-01"))
    parser.add_argument("--profile", default=env("SIM_PROFILE", "generic"), choices=sorted(PROFILES))
    parser.add_argument("--model", default=env("SIM_MODEL", "ILCS-SIM"))
    parser.add_argument("--address", default=env("SIM_ADDRESS", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(env("SIM_PORT", str(default_port))))
    parser.add_argument("--host-name", default=env("SIM_HOST_NAME", "localhost"),
                        help="写进证书的主机名，须与 ILCS 连接用的主机名一致")
    parser.add_argument("--channels", type=int, default=int(env("SIM_CHANNELS", "1")))
    parser.add_argument("--task-seconds", type=float, default=float(env("SIM_TASK_SECONDS", "5")))
    parser.add_argument("--material-map", default=env("SIM_MATERIAL_MAP", ""),
                        help='组分 → 物料映射，如 {"electrolyte": {"material": "电解液 LP57", "unit": "mL", "factor": 0.001}}')
    parser.add_argument("--tick-seconds", type=float, default=float(env("SIM_TICK_SECONDS", "1")))
    return parser


def build_device(args: argparse.Namespace) -> SimulatedDevice:
    sink = None
    if os.environ.get("ILCS_URL") and os.environ.get("ILCS_SERVICE_SOURCE"):
        sink = TelemetryPusher(
            os.environ["ILCS_URL"], os.environ.get("ILCS_STATION_ID", args.device_id),
            os.environ["ILCS_SERVICE_SOURCE"], os.environ.get("ILCS_SERVICE_SECRET", ""),
        )
    return SimulatedDevice(
        args.device_id, args.profile, channels=args.channels, task_seconds=args.task_seconds,
        material_map=load_material_map(args.material_map), model=args.model, telemetry_sink=sink,
    )


def serve_forever(device: SimulatedDevice, tick_seconds: float, stop_server: Callable[[], None],
                  on_tick: Callable[[], None] | None = None) -> None:
    """推进设备直到 SIGTERM / SIGINT；on_tick 给需要刷新寄存器之类的协议层用。"""
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    while not stop.wait(tick_seconds):
        device.tick()
        if on_tick is not None:
            on_tick()
    stop_server()


def configure_logging() -> None:
    logging.basicConfig(level=os.environ.get("SIM_LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
