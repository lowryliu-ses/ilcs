"""ILCS 文本命令模拟设备：串口 / TCP 一问一答的仪器，系统侧用 `line_command_v1` 驱动接入。

两种方言（`--dialect`），都不认识 ILCS 指令号——那正是 `line_command_v1` 要解决的问题：

- `oven`：真空干燥箱温控仪表（示例命令手册，经串口服务器转 TCP）

  | 命令 | 回复 |
  |---|---|
  | `*IDN?` | `ILCS-SIMULATOR,<型号>,<设备ID>,<固件>` |
  | `PROG <程序>` / `SP <温度>` / `VAC <真空度>` | `OK`；程序不在目录里 `ERR PROG`，数值非法 `ERR RANGE` |
  | `RUN` | `OK`；门没关 `ERR DOOR`，正在运行 `ERR BUSY` |
  | `STAT?` | `IDLE` / `RUN,<剩余秒>` / `HOLD,<剩余秒>` / `DONE` / `ALARM,<代码>` |
  | `PV?` | `<箱温>,<真空度>` |
  | `HOLD` / `CONT` / `STOP` / `ACK` | `OK`（ACK 把 DONE / ALARM 复位到 IDLE） |
  | `DOOR?` / `REM?` | `CLOSED` / `OPEN`；`REMOTE` / `LOCAL` |

- `ur`：UR 机械臂仪表盘服务（Dashboard Server，TCP 29999）的常用子集：连接时发欢迎语，
  `load <程序>`、`play`、`pause`、`stop`、`programState`、`robotmode`、`safetystatus`、
  `get serial number`、`get robot model`、`PolyscopeVersion`、`quit`。

两种方言都另有模拟器专用命令：`SIM:FAULT <模式> [参数]`、`SIM:STATE?`（故障模式见 simulators/README.md）。

    python simulators/line_device/server.py --dialect oven --device-id SIM-OVEN-01 --port 4001
    python simulators/line_device/server.py --dialect ur --device-id SIM-ARM-01 --port 29999
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
from pathlib import Path
import sys
import threading

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:  # 直接运行（容器）时也能找到 simulators 包
    sys.path.insert(0, str(ROOT))

from simulators.common.device import DeviceRejected, ReceiptLost, SimulatedDevice  # noqa: E402
from simulators.common.linesrv import CLOSE, LineServer  # noqa: E402
from simulators.common.runtime import build_device, configure_logging, device_arguments, serve_forever  # noqa: E402

MARK = "ILCS-SIMULATOR"
ERROR_CODES = {"fail": "E05", "partial": "E07"}


class _Dialect:
    def __init__(self, device: SimulatedDevice, runner: "SimulatorRunner | None"):
        self.device = device
        self.runner = runner
        self.lock = threading.RLock()
        self.counter = itertools.count(1)
        self.current = ""  # 当前作业在设备模型里的编号；设备本身没有「指令号」

    def _programs(self) -> set[str] | None:
        names = {item.get("program") for item in self.device.methods}
        return None if "*" in names else names

    def _task(self):
        return self.device.tasks.get(self.current) if self.current else None

    def _start(self, capability: str, params: dict) -> str | None:
        """开始一个作业；返回 None 表示回执丢失（设备已经动作）。拒绝抛 DeviceRejected。"""
        task = self._task()
        if task is not None and task.state in {"accepted", "running", "held"}:
            raise DeviceRejected("DeviceBusy", "正在运行")
        job = f"JOB-{next(self.counter)}"
        try:
            self.device.submit(job, "dispatch", capability, params, {"batch_id": "device", "step_id": job})
        except ReceiptLost:
            self.current = job
            return None
        self.current = job
        return job

    def _resume(self) -> bool:
        task = self._task()
        if task is None or task.state != "held":
            return False
        self.device.submit(f"RESUME-{next(self.counter)}", "resume", task.capability, {},
                           {"batch_id": "device", "step_id": self.current})
        return True

    def simulator(self, line: str) -> str | None:
        parts = line.split()
        if parts[0] == "SIM:STATE?":
            return json.dumps(self.device.state())
        if parts[0] == "SIM:FAULT" and len(parts) >= 2:
            parameter = float(parts[2]) if len(parts) > 2 else 0.0
            if parts[1] == "offline":
                if self.runner is not None:
                    self.runner.server.go_offline(parameter or 5)
                return "OK"
            try:
                self.device.set_fault(parts[1], parameter)
            except ValueError:
                return "ERR FAULT"
            return "OK"
        return None


class OvenDialect(_Dialect):
    """真空干燥箱温控仪表。设定值写入后 RUN 才动作；干燥结束停在 DONE，ACK 复位。"""

    def __init__(self, device, runner=None):
        super().__init__(device, runner)
        self.setpoints = {"temp": 25.0, "vacuum": 1013.0}
        self.program = ""
        self.done_cleared = True

    def handle(self, line: str) -> str | None:
        with self.lock:
            text = line.strip()
            if not text:
                return None
            if text.startswith("SIM:"):
                return self.simulator(text) or "ERR SYNTAX"
            head, _, argument = text.partition(" ")
            head = head.upper()
            if head == "*IDN?":
                return f"{MARK},{self.device.model},{self.device.device_id},{self.device.firmware}"
            if head == "PROG":
                programs = self._programs()
                if not argument or (programs is not None and argument not in programs):
                    return "ERR PROG"
                self.program = argument
                return "OK"
            if head in {"SP", "VAC"}:
                try:
                    value = float(argument)
                except ValueError:
                    return "ERR RANGE"
                if value < 0:
                    return "ERR RANGE"
                self.setpoints["temp" if head == "SP" else "vacuum"] = value
                return "OK"
            if head == "RUN":
                return self._run()
            if head == "STAT?":
                return self._status()
            if head == "PV?":
                return self._process_values()
            if head == "HOLD":
                task = self._task()
                if task is None or task.state not in {"accepted", "running"}:
                    return "ERR STATE"
                self.device.hold(f"HOLD-{next(self.counter)}", self.current)
                return "OK"
            if head == "CONT":
                return "OK" if self._resume() else "ERR STATE"
            if head == "STOP":
                if self.current:
                    self.device.abort(f"STOP-{next(self.counter)}", self.current)
                return "OK"
            if head == "ACK":
                self.device.tick()
                task = self._task()
                if task is not None and task.state in {"done", "failed", "aborted"}:
                    self.current = ""
                return "OK"
            if head == "DOOR?":
                return "OPEN" if self.device.interlock else "CLOSED"
            if head == "REM?":
                return "LOCAL" if self.device.fault == "busy" else "REMOTE"
            return "ERR SYNTAX"

    def _run(self) -> str | None:
        task = self._task()
        if task is not None:
            self.device.tick()
            if task.state in {"done", "failed"}:
                return "ERR ACK"  # 上一个作业的结束状态没复位
        try:
            started = self._start("cap.vacuum_dry", dict(self.setpoints))
        except DeviceRejected as error:
            return {"Interlocked": "ERR DOOR", "DeviceBusy": "ERR BUSY"}.get(error.identifier, "ERR RANGE")
        return "OK" if started else None

    def _status(self) -> str:
        self.device.tick()
        task = self._task()
        if task is None or task.state == "aborted":
            return "IDLE"
        remaining = max(0, int(round(task.duration_s - task.elapsed_s)))
        if task.state in {"accepted", "running"}:
            return f"RUN,{remaining}"
        if task.state == "held":
            return f"HOLD,{remaining}"
        if task.state == "done":
            return "DONE"
        return f"ALARM,{ERROR_CODES.get(task.fault_at_submit, 'E05')}"

    def _process_values(self) -> str:
        self.device.tick()
        task = self._task()
        if task is not None and task.state == "done" and task.telemetry:
            measured = {point["metric"]: point["value"] for point in task.telemetry}
            return f"{measured.get('temp', 25.0):.1f},{measured.get('vacuum', 1013.0):.2f}"
        if task is not None and task.state in {"running", "held", "failed", "aborted"}:
            progress = min(1.0, task.elapsed_s / max(task.duration_s, 1e-6))
            temp = 25.0 + (task.params.get("temp", 25.0) - 25.0) * progress
            vacuum = 1013.0 + (task.params.get("vacuum", 1013.0) - 1013.0) * progress
            return f"{temp:.1f},{vacuum:.2f}"
        return "25.0,1013.00"


class UrDialect(_Dialect):
    """UR 仪表盘服务子集。程序结束后回到 STOPPED；控制命令的回复文字与 PolyScope 5.x 一致。"""

    GREETING = "Connected: Universal Robots Dashboard Server"

    def __init__(self, device, runner=None):
        super().__init__(device, runner)
        self.loaded = ""

    def handle(self, line: str):
        with self.lock:
            text = line.strip()
            if not text:
                return None
            if text.startswith("SIM:"):
                return self.simulator(text) or "could not understand: SIM"
            command = text.lower()
            if command == "get serial number":
                return self.device.serial
            if command == "get robot model":
                return self.device.model
            if command == "polyscopeversion":
                return f"URSoftware {self.device.firmware} ({MARK})"
            if command == "robotmode":
                return "Robotmode: IDLE" if self.device.fault == "busy" else "Robotmode: RUNNING"
            if command == "safetystatus":
                return "Safetystatus: PROTECTIVE_STOP" if self.device.interlock else "Safetystatus: NORMAL"
            if command == "get loaded program":
                return f"Loaded program: {self.loaded}" if self.loaded else "No program loaded"
            if command.startswith("load "):
                path = text[5:].strip()
                name = Path(path).stem
                programs = self._programs()
                if programs is not None and name not in programs:
                    return f"File not found: {path}"
                self.loaded = path
                return f"Loading program: {path}"
            if command == "play":
                return self._play()
            if command == "pause":
                task = self._task()
                if task is None or task.state not in {"accepted", "running"}:
                    return "Failed to execute: pause"
                self.device.hold(f"PAUSE-{next(self.counter)}", self.current)
                return "Pausing program"
            if command == "stop":
                if self.current:
                    self.device.abort(f"STOP-{next(self.counter)}", self.current)
                return "Stopped"
            if command == "programstate":
                return self._program_state()
            if command == "running":
                self.device.tick()
                task = self._task()
                return f"Program running: {'true' if task is not None and task.state in {'accepted', 'running'} else 'false'}"
            if command == "quit":
                return ("Disconnected", CLOSE)
            return f"could not understand: '{text}'"

    def _play(self) -> str | None:
        if not self.loaded or self.device.interlock:
            return "Failed to execute: play"
        if self._resume():
            return "Starting program"
        try:
            started = self._start("cap.robot_load", {})
        except DeviceRejected:
            return "Failed to execute: play"
        return "Starting program" if started else None

    def _program_state(self) -> str:
        self.device.tick()
        task = self._task()
        name = Path(self.loaded).name if self.loaded else "<unnamed>"
        if task is not None and task.state in {"accepted", "running"}:
            return f"PLAYING {name}"
        if task is not None and task.state == "held":
            return f"PAUSED {name}"
        return f"STOPPED {name}"


DIALECTS = {"oven": OvenDialect, "ur": UrDialect}


class SimulatorRunner:
    def __init__(self, args: argparse.Namespace, device: SimulatedDevice):
        self.args = args
        self.device = device
        self.dialect = DIALECTS[args.dialect](device, self)
        greeting = UrDialect.GREETING if args.dialect == "ur" else ""
        newline = b"\n" if args.dialect == "ur" else b"\r\n"
        self.server = LineServer(args.address, args.port, self.dialect, newline=newline, greeting=greeting)

    def start(self) -> None:
        self.server.start()

    def stop(self) -> None:
        self.server.stop()


def parse(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    device_arguments(parser, default_port=4001)
    parser.add_argument("--dialect", default=os.environ.get("SIM_DIALECT", "oven"), choices=sorted(DIALECTS))
    return parser.parse_args(argv)


def main(argv=None) -> int:
    configure_logging()
    args = parse(argv)
    device = build_device(args)
    runner = SimulatorRunner(args, device)
    runner.start()
    serve_forever(device, args.tick_seconds, runner.stop)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
