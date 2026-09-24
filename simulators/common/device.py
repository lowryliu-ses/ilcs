"""模拟设备的行为模型，与协议层（SiLA 2 / Modbus TCP / OPC UA / HTTPS 网关）分开，便于直接单测。

各协议的模拟器只做报文转换，设备行为、故障注入与 `executions` 计数都在这里，同一份逻辑。

它模拟的是「一台会出问题的真设备」，而不是一个永远成功的桩：
- 任务按 ILCS 指令号登记；默认对重复指令号去重（返回原任务，不再动作）。
- 长任务在后台推进，查询看到 accepted → running → done；可以保持、恢复、终止。
- 配液工作站按孔位回报每种组分的实际加入量（带确定性的小偏差），并折算成物料消耗；
  充放电柜按通道占用，通道满了明确拒绝，运行中持续上报遥测。
- 故障注入：离线、应答迟到、回执丢失、不去重、失败、执行一半停住、卡死、联锁、忙、时钟偏差。

`executions` 记录每个指令号真正触发了几次物理动作——验收「不重复执行」就看它。
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

PROFILES = {"liquid_handler", "cycler", "generic"}
# 顺序固定：Modbus 模拟器的故障寄存器按这里的序号编码
FAULT_MODES = (
    "none", "offline", "slow_submit", "lost_receipt", "no_dedup", "fail", "partial", "stuck",
    "interlock", "busy", "clock_skew",
)
FAULTS = set(FAULT_MODES)
REJECTIONS = ("InvalidParameters", "Interlocked", "DeviceBusy", "NotSupported")


class DeviceRejected(Exception):
    """设备明确拒绝，没有动作。identifier 是协议无关的拒绝类别，各协议层映射成自己的错误表达
    （SiLA DefinedExecutionError / Modbus 应答码 / OPC UA 状态码 / HTTP 4xx）。"""

    def __init__(self, identifier: str, message: str):
        super().__init__(message)
        self.identifier = identifier
        self.message = message


class ReceiptLost(Exception):
    """设备已经动作，但回执没能送回去——调用方只能判结果未知。"""


@dataclass
class Task:
    command_id: str
    task_type: str
    capability: str
    params: dict
    context: dict
    duration_s: float
    state: str = "accepted"  # accepted | running | held | done | failed | aborted
    elapsed_s: float = 0.0
    last_tick: float = field(default_factory=time.monotonic)
    channel: int | None = None
    delivered: dict = field(default_factory=dict)
    telemetry: list = field(default_factory=list)
    error: str = ""
    fault_at_submit: str = "none"


class SimulatedDevice:
    def __init__(
        self,
        device_id: str,
        profile: str = "generic",
        *,
        channels: int = 1,
        task_seconds: float = 5.0,
        material_map: dict | None = None,
        model: str = "ILCS-SIM",
        serial: str = "",
        telemetry_sink: Callable[[str, list[dict]], None] | None = None,
        methods: list[dict] | None = None,
        vendor: str = "SES 模拟器",
        firmware: str = "sim-1.0",
    ):
        if profile not in PROFILES:
            raise ValueError(f"未知设备类型 {profile}")
        self.device_id = device_id
        self.profile = profile
        self.channels = max(1, channels)
        self.task_seconds = max(0.0, task_seconds)
        self.material_map = material_map or {}
        self.model = model
        self.serial = serial or device_id
        self.telemetry_sink = telemetry_sink
        # 自报的方法目录：没配置时报「*」——模拟器接受任何设备端程序
        self.methods = methods or [{"program": "*", "name": "任意设备端程序（模拟器）"}]
        self.vendor = vendor
        self.firmware = firmware
        self.fault = "none"
        self.fault_parameter = 0.0
        self.interlock = False
        self.tasks: dict[str, Task] = {}
        self.executions: Counter[str] = Counter()
        self.lock = threading.RLock()
        self._telemetry_seq = 0

    # ---------- 故障注入 ----------

    def set_fault(self, mode: str, parameter: float = 0.0) -> dict:
        if mode not in FAULTS:
            raise ValueError(f"未知故障模式 {mode}")
        with self.lock:
            self.fault = mode
            self.fault_parameter = parameter
            self.interlock = mode == "interlock"
        return self.state()

    def state(self) -> dict:
        with self.lock:
            return {
                "device_id": self.device_id, "profile": self.profile, "fault": self.fault,
                "fault_parameter": self.fault_parameter,
                "tasks": {cid: task.state for cid, task in self.tasks.items()},
                "executions": dict(self.executions),
                "busy_channels": sorted(t.channel for t in self._active() if t.channel is not None),
            }

    # ---------- 时间 ----------

    def now(self) -> datetime:
        moment = datetime.now(timezone.utc)
        if self.fault == "clock_skew":
            moment += timedelta(seconds=self.fault_parameter)
        return moment

    def _active(self) -> list[Task]:
        return [t for t in self.tasks.values() if t.state in {"accepted", "running", "held"}]

    def tick(self) -> None:
        """推进在跑的任务。查询前和后台线程都会调用。"""
        finished: list[Task] = []
        with self.lock:
            now = time.monotonic()
            for task in {id(t): t for t in self.tasks.values()}.values():
                if task.state == "accepted":
                    task.state = "running"
                    task.last_tick = now
                if task.state != "running":
                    task.last_tick = now
                    continue
                task.elapsed_s += now - task.last_tick
                task.last_tick = now
                if task.fault_at_submit == "stuck":
                    continue
                if task.fault_at_submit == "partial" and task.elapsed_s >= task.duration_s / 2:
                    task.state = "failed"
                    task.error = "执行到一半设备停止：部分执行"
                    task.delivered = self._delivered(task, fraction=0.5)
                    finished.append(task)
                elif task.elapsed_s >= task.duration_s:
                    if task.fault_at_submit == "fail":
                        task.state = "failed"
                        task.error = "设备报告执行失败（模拟）"
                    else:
                        task.state = "done"
                        task.delivered = self._delivered(task)
                        task.telemetry = self._final_telemetry(task)
                    finished.append(task)
            running = [t for t in self.tasks.values() if t.state == "running"]
        if running and self.telemetry_sink is not None:
            self._emit_telemetry(running)

    # ---------- 指令 ----------

    def submit(self, command_id: str, task_type: str, capability: str, params: dict, context: dict) -> dict:
        if not command_id:
            raise DeviceRejected("InvalidParameters", "缺少 CommandId")
        with self.lock:
            if self.interlock:
                raise DeviceRejected("Interlocked", "安全联锁触发，设备未动作")
            if self.fault == "busy":
                raise DeviceRejected("DeviceBusy", "设备忙，未接受任务")
            existing = self.tasks.get(command_id)
            if existing is not None and self.fault != "no_dedup":
                return self.receipt(existing, command_id)
            self._validate(params)
            if task_type == "resume":
                held = self._held_for(context)
                if held is not None:
                    held.state = "running"
                    held.last_tick = time.monotonic()
                    self.tasks[command_id] = held
                    self.executions[command_id] += 1
                    return self.receipt(held, command_id)
            channel = None
            if self.profile == "cycler":
                busy = {t.channel for t in self._active()}
                free = [c for c in range(1, self.channels + 1) if c not in busy]
                if not free:
                    raise DeviceRejected("DeviceBusy", f"{self.channels} 个通道都在使用，未接受任务")
                channel = free[0]
            duration = float(params.get("duration_s") or self.task_seconds)
            task = Task(
                command_id=command_id, task_type=task_type, capability=capability, params=params,
                context=context, duration_s=duration, channel=channel, fault_at_submit=self.fault,
            )
            self.tasks[command_id] = task
            self.executions[command_id] += 1
            fault, parameter = self.fault, self.fault_parameter
            receipt = self.receipt(task, command_id)
        if fault == "slow_submit":
            time.sleep(parameter)
        if fault == "lost_receipt":
            raise ReceiptLost("任务已开始，但回执在返回途中丢失")
        return receipt

    def query(self, command_id: str) -> dict:
        self.tick()
        with self.lock:
            task = self.tasks.get(command_id)
            if task is None:
                return {"command_id": command_id, "state": "not_found", "device_ts": self._ts(), "quality": "good"}
            return self.receipt(task, command_id)

    def hold(self, command_id: str, target: str) -> dict:
        self.tick()
        with self.lock:
            task = self.tasks.get(target)
            if task is None or task.state not in {"accepted", "running"}:
                raise DeviceRejected("InvalidParameters", f"没有可保持的在途任务 {target or '（未指定）'}")
            task.state = "held"
            return self._control_receipt(command_id, f"任务 {target} 已保持")

    def abort(self, command_id: str, target: str) -> dict:
        self.tick()
        with self.lock:
            task = self.tasks.get(target) if target else None
            if task is not None and task.state in {"accepted", "running", "held"}:
                task.state = "aborted"
                task.error = f"被 {command_id} 终止"
                task.delivered = self._delivered(task, fraction=min(1.0, task.elapsed_s / max(task.duration_s, 1e-6)))
            # 目标已经结束或不存在：设备已在安全状态，终止照样确认
            return self._control_receipt(command_id, "设备已终止并处于安全状态")

    def identity(self) -> dict:
        return {
            "device_id": self.device_id, "model": self.model, "serial": self.serial,
            "vendor": self.vendor, "firmware": self.firmware, "simulator": True, "profile": self.profile,
            "methods": self.methods, "commands": ["dispatch", "resume", "retry", "hold", "abort", "query"],
            "channels": self.channels, "interlock": self.interlock,
            "accepts_commands": self.fault not in {"busy"}, "device_ts": self._ts(),
        }

    # ---------- 回执 ----------

    def receipt(self, task: Task, command_id: str) -> dict:
        state = {"held": "running", "aborted": "failed"}.get(task.state, task.state)
        return {
            "command_id": command_id, "state": state, "phase": task.state,
            "device_ts": self._ts(), "quality": "good" if state != "failed" else "bad",
            "delivered": task.delivered, "telemetry": task.telemetry, "error": task.error,
            "channel": task.channel,
        }

    def _control_receipt(self, command_id: str, note: str) -> dict:
        return {
            "command_id": command_id, "state": "done", "device_ts": self._ts(), "quality": "good",
            "delivered": {"note": note}, "telemetry": [], "error": "",
        }

    def _ts(self) -> str:
        return self.now().isoformat(timespec="seconds")

    # ---------- 各类设备的物理结果 ----------

    def _validate(self, params: dict) -> None:
        def numeric(value: Any) -> bool:
            return isinstance(value, (int, float)) and not isinstance(value, bool)

        for key, value in params.items():
            if key == "wells":
                if not isinstance(value, dict):
                    raise DeviceRejected("InvalidParameters", "wells 必须是 {孔位: {参数: 值}}")
                for well, values in value.items():
                    for name, amount in (values or {}).items():
                        if not numeric(amount) or amount < 0:
                            raise DeviceRejected("InvalidParameters", f"{well}.{name} = {amount!r} 不是非负数值")
            elif numeric(value) and value < 0:
                raise DeviceRejected("InvalidParameters", f"{key} = {value} 不能为负")

    def _noise(self, key: str) -> float:
        """确定性的 ±0.5% 偏差：同一指令重放得到同一实测值，但不等于设定值。"""
        digest = hashlib.sha256(key.encode()).digest()
        return 1 + (digest[0] / 255 - 0.5) / 100

    def _delivered(self, task: Task, fraction: float = 1.0) -> dict:
        params = task.params or {}
        if self.profile == "liquid_handler":
            wells = params.get("wells") or {}
            flat = {k: v for k, v in params.items() if k != "wells" and isinstance(v, (int, float))}
            actual_wells = {
                well: {
                    name: round(amount * fraction * self._noise(f"{task.command_id}:{well}:{name}"), 4)
                    for name, amount in {**flat, **(values or {})}.items()
                    if isinstance(amount, (int, float))
                }
                for well, values in wells.items()
            }
            totals: Counter[str] = Counter()
            if actual_wells:
                for values in actual_wells.values():
                    totals.update(values)
            else:
                totals.update({k: v * fraction for k, v in flat.items()})
            materials = []
            for name, amount in totals.items():
                mapping = self.material_map.get(name)
                if mapping:
                    materials.append({
                        "material": mapping["material"],
                        "quantity": round(amount * float(mapping.get("factor", 1)), 6),
                        "unit": mapping.get("unit", ""),
                    })
            return {"wells": actual_wells, "totals": dict(totals), "materials": materials}
        if self.profile == "cycler":
            cycles = int(params.get("cycles") or 1)
            return {
                "channel": task.channel, "cycles_completed": int(cycles * fraction),
                "discharge_capacity_mAh": round(3.2 * self._noise(task.command_id), 4),
            }
        return {"params": params, "fraction": fraction}

    def _final_telemetry(self, task: Task) -> list[dict]:
        if self.profile == "cycler":
            return [{"metric": "voltage", "value": 3.65, "setpoint": None}]
        return [
            {"metric": name, "value": round(value * self._noise(f"{task.command_id}:{name}"), 4), "setpoint": value}
            for name, value in (task.params or {}).items()
            if isinstance(value, (int, float)) and not isinstance(value, bool) and name != "duration_s"
        ]

    def _emit_telemetry(self, running: list[Task]) -> None:
        with self.lock:
            self._telemetry_seq += 1
            seq = self._telemetry_seq
        for task in running:
            if self.profile == "cycler":
                progress = task.elapsed_s / max(task.duration_s, 1e-6)
                points = [
                    {"metric": "voltage", "value": round(3.0 + 1.2 * progress, 4), "device_ts": self._ts()},
                    {"metric": "current", "value": 0.32, "device_ts": self._ts()},
                ]
            else:
                points = [{"metric": "progress", "value": round(min(1.0, task.elapsed_s / max(task.duration_s, 1e-6)), 4),
                           "device_ts": self._ts()}]
            try:
                self.telemetry_sink(f"{self.device_id}:{task.command_id}:{seq}", points, task.command_id)
            except Exception:  # 遥测是尽力而为，不能拖垮设备本身
                pass

    def _held_for(self, context: dict) -> Task | None:
        step_id, batch_id = context.get("step_id"), context.get("batch_id")
        for task in self.tasks.values():
            if task.state == "held" and task.context.get("step_id") == step_id and task.context.get("batch_id") == batch_id:
                return task
        return None


def load_material_map(raw: str) -> dict:
    return json.loads(raw) if raw else {}
