"""ILCS AGV 车队模拟设备：按 MiR 机器人 REST API（v2.0.0）的常用子集模拟几台 AGV，系统侧用 `rest_map_v1` 接入。

每台 AGV 一个前缀 `/robots/<名称>/api/v2.0.0`（真实现场每台 MiR 各有自己的地址，这里用前缀区分）：

| 请求 | 说明 |
|---|---|
| `GET /status` | `robot_name`、`serial_number`、`model`、`software_version`、`state_id` / `state_text`（Ready、Pause、Executing、EmergencyStop、Error）、`position`、`battery_percentage`、`mission_queue_id` |
| `PUT /status` | `{"state_id": 4}` 暂停、`{"state_id": 3}` 继续 |
| `GET /missions` | 任务模板列表 `[{guid, name}]` |
| `GET /positions` | 站点列表 `[{guid, name}]` |
| `POST /mission_queue` | `{"mission_id", "message", "parameters": [{"id", "value"}]}` → 201 `{"id", "state": "Pending", …}` |
| `GET /mission_queue` / `GET /mission_queue/<id>` | 队列（`[{id, state, url}]`）/ 单个任务（`state`：Pending、Executing、Paused、Done、Aborted） |
| `DELETE /mission_queue/<id>` | 取消；执行中的任务变 Aborted |

认证：`Authorization: Basic base64(用户名:sha256(口令))`（与 MiR 相同）。首次启动在 `--cert-dir` 写凭据描述文件
`fleet.json`（`{"headers": {"Authorization": …}}`），系统侧 `credential_ref` 指向它。
模拟器专用：`POST /simulator/fault {"robot", "mode", "parameter"}`（estop、busy、fail、stuck、slow_submit、
lost_receipt、offline、none）、`GET /simulator/state`。

    python devices/simulators/fleet/server.py --robots AGV-01,AGV-02 --port 8080
"""
from __future__ import annotations

import argparse
import base64
from collections import Counter
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import itertools
import json
import logging
import os
from pathlib import Path
import secrets
import sys
import threading
import time
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:  # 直接运行（容器）时也能找到 simulators 包
    sys.path.insert(0, str(ROOT))

from simulators.common.control import start_control  # noqa: E402
from simulators.common.runtime import cert_dir_default, configure_logging  # noqa: E402

MARK = "ILCS-SIMULATOR"
STATE_TEXT = {3: "Ready", 4: "Pause", 5: "Executing", 10: "EmergencyStop", 12: "Error"}
MISSIONS = [{"guid": "mission-ilcs-transfer", "name": "ILCS 托盘转运"}]
log = logging.getLogger("ilcs.fleet-sim")


class Robot:
    def __init__(self, name: str, task_seconds: float):
        self.name = name
        self.task_seconds = task_seconds
        self.state_id = 3
        self.position = "HOME"
        self.queue: list[dict] = []
        self.fault = "none"
        self.parameter = 0.0
        self.executions: Counter = Counter()
        self.lock = threading.RLock()

    def status(self) -> dict:
        with self.lock:
            active = next((m for m in self.queue if m["state"] in {"Executing", "Paused"}), None)
            state_id = 5 if active is not None and self.state_id == 3 else self.state_id
            return {
                "robot_name": self.name, "serial_number": f"{MARK}-{self.name}", "model": "MiR250",
                "software_version": "2.13.4 (ILCS-SIMULATOR)", "state_id": state_id,
                "state_text": STATE_TEXT.get(state_id, "Error"), "mode_text": "Mission",
                "battery_percentage": 87.5, "position": {"x": 12.3, "y": 4.5, "orientation": 90.0, "name": self.position},
                "mission_queue_id": active["id"] if active else None,
                "mission_text": active["message"] if active else "", "errors": [],
            }

    def tick(self) -> None:
        with self.lock:
            if self.state_id != 3:
                return
            for mission in self.queue:
                if mission["state"] == "Executing":
                    if self.fault == "stuck" and mission.get("fault") == "stuck":
                        return
                    if time.monotonic() - mission["_started"] >= self.task_seconds:
                        if mission.get("fault") == "fail":
                            mission["state"], mission["message_result"] = "Aborted", "路径被阻挡，任务中止"
                        else:
                            mission["state"] = "Done"
                            self.position = next((p["value"] for p in mission["parameters"] if p.get("id") == "To"),
                                                 self.position)
                        mission["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                    return
            pending = next((m for m in self.queue if m["state"] == "Pending"), None)
            if pending is not None:
                pending["state"], pending["_started"] = "Executing", time.monotonic()
                pending["started"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                self.executions[pending["id"]] += 1


class Fleet:
    def __init__(self, robots: list[str], task_seconds: float):
        self.robots = {name: Robot(name, task_seconds) for name in robots}
        self.counter = itertools.count(101)
        self.lock = threading.Lock()

    def tick(self) -> None:
        for robot in self.robots.values():
            robot.tick()

    def state(self) -> dict:
        return {name: {"fault": robot.fault, "state_id": robot.state_id, "position": robot.position,
                       "queue": [{"id": m["id"], "state": m["state"], "message": m["message"]} for m in robot.queue],
                       "executions": dict(robot.executions)}
                for name, robot in self.robots.items()}


class FleetTarget:
    """统一控制口（devices/simulators/common/control.py）：一个进程模拟整个车队，`unit` 指定哪台车。

    车队不认 ILCS 指令号（任务号是车队自己编的），只报总动作次数；联锁对应急停。离线是整个车队接口断开。
    """

    knows_command_ids = False
    SUPPORTED = {"none", "interlock", "busy", "fail", "stuck", "slow_submit", "lost_receipt"}

    def __init__(self, fleet: "Fleet", go_offline):
        self.fleet = fleet
        self.go_offline = go_offline

    def _robot(self, unit: str) -> "Robot":
        robot = self.fleet.robots.get(unit)
        if robot is None:
            raise KeyError(f"车队里没有 {unit or '（未指定车辆）'}；可选 {', '.join(self.fleet.robots)}")
        return robot

    def set_fault(self, mode: str, parameter: float, unit: str = "") -> dict:
        if mode == "offline":
            self.go_offline(parameter or 5)
            return {"fault": "offline", "seconds": parameter or 5, "knows_command_ids": False}
        if mode not in self.SUPPORTED:
            raise ValueError(f"车队模拟设备不支持故障 {mode}")
        robot = self._robot(unit)
        mode = "estop" if mode == "interlock" else mode
        with robot.lock:
            robot.fault, robot.parameter = mode, parameter
            robot.state_id = {"estop": 10, "busy": 12}.get(mode, 3 if robot.state_id in {10, 12} else robot.state_id)
        return self.state(unit)

    def state(self, unit: str = "") -> dict:
        robot = self._robot(unit)
        with robot.lock:
            return {"device_id": robot.name, "fault": robot.fault, "fault_parameter": robot.parameter,
                    "state_id": robot.state_id, "motions": sum(robot.executions.values()), "knows_command_ids": False}


class _Drop(Exception):
    """回执丢失：任务已进队列，连接直接断开。"""


def handler_for(fleet: Fleet, runner: "SimulatorRunner", authorization: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "MiR-Sim/2.13"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            log.debug("%s %s", self.address_string(), fmt % args)

        def _reply(self, status: int, body=None) -> None:
            raw = b"" if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            try:
                value = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return {}
            return value if isinstance(value, dict) else {}

        def _route(self, method: str):
            parts = [unquote(p) for p in urlparse(self.path).path.strip("/").split("/") if p]
            if parts[:1] == ["simulator"]:
                if method == "GET" and parts == ["simulator", "state"]:
                    return 200, fleet.state()
                if method == "POST" and parts == ["simulator", "fault"]:
                    body = self._body()
                    robot = fleet.robots.get(str(body.get("robot") or ""))
                    mode, parameter = str(body.get("mode") or "none"), float(body.get("parameter") or 0)
                    if mode == "offline":
                        runner.go_offline(parameter or 5)
                        return 200, {"offline": parameter or 5}
                    if robot is None:
                        return 404, {"error": "robot not found"}
                    with robot.lock:
                        robot.fault, robot.parameter = mode, parameter
                        robot.state_id = {"estop": 10, "busy": 12}.get(mode, 3 if robot.state_id in {10, 12} else robot.state_id)
                    return 200, fleet.state()[robot.name]
                return 404, {"error": "not found"}
            if len(parts) < 4 or parts[0] != "robots" or parts[2:4] != ["api", "v2.0.0"]:
                return 404, {"error_human": "not found"}
            robot = fleet.robots.get(parts[1])
            if robot is None:
                return 404, {"error_human": f"robot {parts[1]} not found"}
            rest = parts[4:]
            if rest == ["status"] and method == "GET":
                return 200, robot.status()
            if rest == ["status"] and method == "PUT":
                state_id = int(self._body().get("state_id") or 0)
                with robot.lock:
                    if robot.state_id in {10, 12}:
                        return 400, {"error_human": "Robot is in emergency stop / error state"}
                    if state_id == 4:
                        robot.state_id = 4
                        for mission in robot.queue:
                            if mission["state"] == "Executing":
                                mission["state"], mission["_paused_at"] = "Paused", time.monotonic()
                    elif state_id == 3:
                        robot.state_id = 3
                        for mission in robot.queue:
                            if mission["state"] == "Paused":
                                mission["_started"] += time.monotonic() - mission.pop("_paused_at", time.monotonic())
                                mission["state"] = "Executing"
                    else:
                        return 400, {"error_human": f"state_id {state_id} not supported"}
                return 200, robot.status()
            if rest == ["missions"] and method == "GET":
                return 200, MISSIONS
            if rest == ["mission_queue"] and method == "GET":
                with robot.lock:
                    return 200, [{"id": m["id"], "state": m["state"], "url": f"/v2.0.0/mission_queue/{m['id']}"}
                                 for m in robot.queue]
            if rest == ["mission_queue"] and method == "POST":
                body = self._body()
                if body.get("mission_id") not in {m["guid"] for m in MISSIONS}:
                    return 400, {"error_human": f"Mission {body.get('mission_id')} not found"}
                with robot.lock:
                    if robot.state_id in {10, 12}:
                        return 409, {"error_human": "Robot is in emergency stop / error state"}
                    mission = {
                        "id": next(fleet.counter), "state": "Pending", "mission_id": body["mission_id"],
                        "message": str(body.get("message") or ""), "parameters": list(body.get("parameters") or []),
                        "priority": int(body.get("priority") or 0), "started": None, "finished": None,
                        "fault": robot.fault if robot.fault in {"fail", "stuck"} else "",
                    }
                    robot.queue.append(mission)
                    fault, parameter = robot.fault, robot.parameter
                if fault == "slow_submit":
                    time.sleep(parameter)
                if fault == "lost_receipt":
                    raise _Drop()
                return 201, self._public(mission)
            if len(rest) == 2 and rest[0] == "mission_queue":
                with robot.lock:
                    mission = next((m for m in robot.queue if str(m["id"]) == rest[1]), None)
                    if mission is None:
                        return 404, {"error_human": "Mission not found"}
                    if method == "GET":
                        return 200, self._public(mission)
                    if method == "DELETE":
                        if mission["state"] in {"Pending", "Executing", "Paused"}:
                            mission["state"] = "Aborted"
                            mission["message_result"] = "已取消"
                        return 204, None
            return 405, {"error_human": "method not allowed"}

        @staticmethod
        def _public(mission: dict) -> dict:
            return {key: value for key, value in mission.items() if not key.startswith("_") and key != "fault"}

        def _handle(self, method: str) -> None:
            if not urlparse(self.path).path.startswith("/simulator") and not hmac.compare_digest(
                self.headers.get("Authorization") or "", authorization,
            ):
                self._reply(401, {"error_human": "Unauthorized"})
                return
            try:
                status, body = self._route(method)
            except _Drop:
                self.close_connection = True
                self.connection.close()
                return
            self._reply(status, body)

        def do_GET(self):  # noqa: N802
            self._handle("GET")

        def do_POST(self):  # noqa: N802
            self._handle("POST")

        def do_PUT(self):  # noqa: N802
            self._handle("PUT")

        def do_DELETE(self):  # noqa: N802
            self._handle("DELETE")

    return Handler


def mir_authorization(username: str, password: str) -> str:
    """MiR 的 Basic 认证：base64(用户名:sha256(口令))。"""
    digest = hashlib.sha256(password.encode()).hexdigest()
    return "Basic " + base64.b64encode(f"{username}:{digest}".encode()).decode()


class SimulatorRunner:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.fleet = Fleet([name.strip() for name in args.robots.split(",") if name.strip()], args.task_seconds)
        self.authorization = self._credentials()
        self.server: ThreadingHTTPServer | None = None
        self.closed = threading.Event()
        threading.Thread(target=self._tick, daemon=True).start()

    def _credentials(self) -> str:
        path = Path(self.args.cert_dir) / "fleet.json"
        if path.exists():
            return json.loads(path.read_text())["headers"]["Authorization"]
        authorization = mir_authorization("ilcs", secrets.token_urlsafe(16))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"headers": {"Authorization": authorization}}))
        path.chmod(0o600)
        log.info("已生成车队凭据 %s；ILCS 适配器 credential_ref 指向它", path)
        return authorization

    def _tick(self) -> None:
        while not self.closed.wait(0.05):
            self.fleet.tick()

    def start(self) -> None:
        server = ThreadingHTTPServer((self.args.address, self.args.port),
                                     handler_for(self.fleet, self, self.authorization))
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True).start()
        self.server = server
        log.info("AGV 车队模拟设备已启动：http://%s:%s（%s）", self.args.address, self.args.port,
                 ", ".join(self.fleet.robots))

    def stop(self) -> None:
        server, self.server = self.server, None
        if server is not None:
            server.shutdown()
            server.server_close()

    def close(self) -> None:
        self.closed.set()
        self.stop()

    def go_offline(self, seconds: float) -> None:
        def cycle():
            time.sleep(0.2)
            self.stop()
            time.sleep(seconds)
            self.start()

        threading.Thread(target=cycle, daemon=True).start()

    def control_target(self) -> FleetTarget:
        return FleetTarget(self.fleet, self.go_offline)


def parse(argv=None) -> argparse.Namespace:
    env = os.environ.get
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--robots", default=env("SIM_ROBOTS", "AGV-01,AGV-02"))
    parser.add_argument("--address", default=env("SIM_ADDRESS", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(env("SIM_PORT", "8080")))
    parser.add_argument("--task-seconds", type=float, default=float(env("SIM_TASK_SECONDS", "5")))
    parser.add_argument("--cert-dir", default=env("SIM_CERT_DIR", cert_dir_default("fleet")))
    return parser.parse_args(argv)


def main(argv=None) -> int:
    configure_logging()
    args = parse(argv)
    runner = SimulatorRunner(args)
    runner.start()
    control = start_control(runner.control_target())
    stop = threading.Event()
    import signal

    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    stop.wait()
    runner.close()
    if control is not None:
        control.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
