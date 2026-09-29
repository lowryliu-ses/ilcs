"""PLC 点表映射：设备有自己的变量 / 寄存器表，没有实现 ILCS 任务契约。

`opcua_map_v1`（OPC UA 节点）与 `modbus_map_v1`（Modbus 寄存器 / 线圈）共用这里的作业逻辑，子类只负责
按点名读写。配置里先登记点（`points`），再用点名描述每项能力怎么下发：

```json
{
  "points": {"state": …, "error": …, "cmd_start": …, "sp_temp": …, "pv_temp": …},
  "identity": {"device_id": "serial", "model": "model", "firmware": "firmware"},
  "ready": {"point": "remote", "ok": [true]},
  "interlock": {"point": "safety_ok", "ok": [true]},
  "heartbeat": {"point": "heartbeat", "stale_sec": 30},
  "job_id": {"write": "job_id", "echo": "job_latched"},
  "capabilities": {"cap.coat": {
      "constants": {"operation": 1},
      "recipe": {"point": "recipe_no", "map": {"COAT-STD": 1, "COAT-180": 12}, "default": "COAT-STD"},
      "write": {"thickness": "sp_thickness", "temp": "sp_temp"},
      "start": {"point": "cmd_start", "value": true, "pulse_ms": 300},
      "actuals": {"thickness": "pv_thickness", "temp": "pv_temp"}}},
  "status": {"point": "state", "states": {"0": "idle", "1": "running", "2": "held", "3": "done", "4": "failed"}},
  "error": {"point": "error", "codes": {"17": "涂布头压力超限"}},
  "hold": {"point": "cmd_hold", "value": true, "pulse_ms": 300},
  "resume": {…}, "abort": {…}, "acknowledge": {…}
}
```

`recipe` 把设备方法的设备端程序（步骤没引用设备方法时用 `default`）换成 PLC 的程序号；`map` 里没有的程序
直接拒绝。下发顺序：就绪 / 联锁检查 → 常量 → 程序号 → 设定值 → 指令号（可选）→ 启动信号。启动信号之前的任何写入
失败都说明设备没动（明确失败）；启动信号本身被拒是明确失败，没拿到结论是结果未知。PLC 回显指令号时
（`job_id.echo`），启动未确认的作业可以按回显找回。
"""
from __future__ import annotations

import math
import time

from ..base import AdapterError, AdapterIndeterminate, AdapterUnreachable
from ..jobs import MappedJobAdapter, render_value

STATE_NAMES = {"idle", "running", "held", "done", "failed"}


class PointMapAdapter(MappedJobAdapter):
    NOTE = "PLC 点表映射"
    # 写下设定值与启动沿就是交接：PLC 扫描到才判断，拒绝（忙、联锁）要之后读状态点才看得到（接入验收据此等它）
    handoff = "async"

    def __init__(self, record, journal_key: str = ""):
        super().__init__(record, journal_key)
        points = self.config.get("points")
        if not isinstance(points, dict) or not points:
            raise AdapterError(f"{self.DRIVER} 必须在 points 里登记用到的点")
        self.points = points
        self.status = self.config.get("status") or {}
        if not self.status.get("point"):
            raise AdapterError("status.point 必填：没有状态点就无法判断作业做没做完")
        states = self.status.get("states") or {}
        if not isinstance(states, dict) or not states or not set(states.values()) <= STATE_NAMES:
            raise AdapterError("status.states 必须把状态值映射到 idle / running / held / done / failed")
        self.states = {str(key).lower(): value for key, value in states.items()}
        self.heartbeat_spec = self.config.get("heartbeat") or {}
        if isinstance(self.heartbeat_spec, str):
            self.heartbeat_spec = {"point": self.heartbeat_spec}
        self.heartbeat_stale = self._seconds("stale_sec", 30.0, source=self.heartbeat_spec) if self.heartbeat_spec else 0
        self._heartbeat: tuple[object, float] | None = None
        for name in self._referenced():
            if name not in self.points:
                raise AdapterError(f"配置引用了没有登记的点 {name}")

    def _referenced(self) -> set[str]:
        names = {self.status["point"]}
        for key in ("ready", "interlock", "error", "hold", "resume", "abort", "acknowledge"):
            spec = self.config.get(key) or {}
            if spec.get("point"):
                names.add(spec["point"])
        names |= {value for value in (self.config.get("identity") or {}).values() if isinstance(value, str)}
        if self.heartbeat_spec.get("point"):
            names.add(self.heartbeat_spec["point"])
        job_id = self.config.get("job_id") or {}
        names |= {job_id[key] for key in ("write", "echo") if job_id.get(key)}
        for capability, spec in (self.config.get("capabilities") or {}).items():
            if not isinstance(spec, dict):
                raise AdapterError(f"capabilities.{capability} 必须是对象")
            start = spec.get("start") or {}
            if not start.get("point") and not start.get("method"):
                raise AdapterError(f"capabilities.{capability}.start 必须指定启动点（point）或方法（method）")
            if start.get("point"):
                names.add(start["point"])
            names |= set((spec.get("constants") or {}).keys())
            names |= {self._point_name(item) for item in (spec.get("write") or {}).values()}
            names |= set((spec.get("actuals") or {}).values())
            if (spec.get("recipe") or {}).get("point"):
                names.add(spec["recipe"]["point"])
        return names

    @staticmethod
    def _point_name(item) -> str:
        return item if isinstance(item, str) else str((item or {}).get("point") or "")

    # ---------- 子类 I/O ----------

    def read_point(self, name: str):
        raise NotImplementedError

    def write_point(self, name: str, value) -> None:
        """写不进（设备回 Bad 状态 / 异常应答）抛 AdapterError；没拿到结论抛 AdapterUnreachable。"""
        raise NotImplementedError

    def call_method(self, spec: dict, arguments: list) -> None:
        raise AdapterError(f"{self.PROTOCOL} 没有方法调用；启动请用启动点（start.point）")

    # ---------- 工具 ----------

    def _scale(self, name: str) -> float:
        point = self.points[name]
        scale = float(point.get("scale", 1) if isinstance(point, dict) else 1)
        if not math.isfinite(scale) or scale == 0:
            raise AdapterError(f"点 {name} 的 scale 不能为 0")
        return scale

    def _read(self, name: str):
        value = self.read_point(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            scale = self._scale(name)
            return value * scale if scale != 1 else value
        return value

    def _write(self, name: str, value) -> None:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            scale = self._scale(name)
            value = value / scale if scale != 1 else value
        self.write_point(name, value)

    def _pulse(self, spec: dict, label: str) -> None:
        point = spec["point"]
        self._write(point, spec.get("value", True))
        if spec.get("pulse_ms"):
            time.sleep(float(spec["pulse_ms"]) / 1000)
            reset = spec.get("reset", False if isinstance(spec.get("value", True), bool) else 0)
            try:
                self._write(point, reset)
            except (AdapterError, AdapterUnreachable) as exc:
                # 信号已经发出：复位没写进去不改变「已触发」这个事实，留给下一次触发前的检查
                raise AdapterIndeterminate(f"{label}信号已发出，但复位 {point} 失败：{exc}") from exc

    @staticmethod
    def _matches(value, allowed: list) -> bool:
        return any(value == item or str(value).lower() == str(item).lower() for item in allowed)

    # ---------- 钩子 ----------

    def accepted_params(self, spec: dict) -> set[str]:
        return set((spec.get("write") or {}).keys()) | set(spec.get("accept") or [])

    def read_identity(self) -> dict:
        identity: dict = {"vendor": self.config.get("vendor", ""), "model": self.config.get("model", "")}
        for field, point in (self.config.get("identity") or {}).items():
            if isinstance(point, str):
                identity[field] = str(self._read(point) or "").strip()
        ready = self.config.get("ready") or {}
        if ready.get("point"):
            identity["accepts_commands"] = self._matches(self._read(ready["point"]), ready.get("ok") or [True])
        interlock = self.config.get("interlock") or {}
        if interlock.get("point"):
            identity["interlock"] = not self._matches(self._read(interlock["point"]), interlock.get("ok") or [True])
        if self.heartbeat_spec.get("point"):
            # PLC 程序停了变量照样能读：心跳计数长时间不变就当失联
            beat, moment = self._read(self.heartbeat_spec["point"]), time.monotonic()
            if self._heartbeat is None or self._heartbeat[0] != beat:
                self._heartbeat = (beat, moment)
            elif moment - self._heartbeat[1] > self.heartbeat_stale:
                raise AdapterUnreachable(f"PLC 心跳 {self.heartbeat_spec['point']} {self.heartbeat_stale:g} s 没有变化，程序可能已停止")
        identity.setdefault("device_id", identity.get("serial", ""))
        return identity

    def precheck(self, spec: dict) -> None:
        ready = self.config.get("ready") or {}
        if ready.get("point") and not self._matches(self._read(ready["point"]), ready.get("ok") or [True]):
            raise AdapterError("设备未就绪（不在远程 / 自动模式），未发出启动信号")
        interlock = self.config.get("interlock") or {}
        if interlock.get("point") and not self._matches(self._read(interlock["point"]), interlock.get("ok") or [True]):
            raise AdapterError("设备联锁未解除（Interlocked），未发出启动信号")

    def start_job(self, job: dict, spec: dict, values: dict) -> None:
        writes: list[tuple[str, object]] = list((spec.get("constants") or {}).items())
        recipe = spec.get("recipe") or {}
        if recipe.get("point"):
            name = values.get("program") or str(recipe.get("default") or "")
            mapping = recipe.get("map")
            if isinstance(mapping, dict):
                if name not in mapping:
                    raise AdapterError(f"设备端程序 {name or '（未指定）'} 没有在 recipe.map 里登记程序号")
                writes.append((recipe["point"], mapping[name]))
            else:
                writes.append((recipe["point"], name))
        for parameter, item in (spec.get("write") or {}).items():
            if parameter not in values:
                raise AdapterError(f"参数 {parameter} 指令里没有、配置也没有缺省值")
            writes.append((self._point_name(item), values[parameter]))
        job_id = self.config.get("job_id") or {}
        if job_id.get("write"):
            writes.append((job_id["write"], job["id"]))
        for point, value in writes:
            try:
                self._write(point, value)
            except AdapterUnreachable as exc:
                raise AdapterError(f"写 {point} 没有结论（{exc}）；启动信号还没发出，设备没有动作") from exc
            except AdapterError as exc:
                raise AdapterError(f"写 {point} = {value!r} 被设备拒绝（{exc}）；启动信号没有发出") from exc
        start = spec["start"]
        if start.get("method"):
            self.call_method(start["method"], render_value(list(start["method"].get("args") or []), values))
        else:
            self._pulse(start, "启动")

    def read_status(self, job: dict) -> tuple[str, str]:
        value = self._read(self.status["point"])
        key = str(value).lower()
        if isinstance(value, float) and value.is_integer():
            key = str(int(value))
        state = self.states.get(key)
        if state is None:
            raise AdapterIndeterminate(f"状态点 {self.status['point']} 的值 {value!r} 没有在 status.states 里映射")
        detail = ""
        error = self.config.get("error") or {}
        if state == "failed" and error.get("point"):
            code = self._read(error["point"])
            code = str(int(code)) if isinstance(code, float) and code.is_integer() else str(code)
            detail = (error.get("codes") or {}).get(code, f"设备故障代码 {code}")
        return state, detail

    def device_state(self) -> str:
        return self.read_status({})[0]

    def start_refused(self, job: dict, elapsed: float) -> str:
        """启动沿写下去、PLC 停在空闲并在故障点报了 `start_refused.codes` 里的代码：设备明确拒绝了这次启动，没有动作。

        `after_sec`（缺省 1 秒）之后才认：故障点上可能还留着上一次拒绝的代码，给 PLC 一个扫描周期先做出反应——
        它接了这次启动就会进入运行，不会走到这里。
        """
        spec = self.config.get("start_refused") or {}
        codes = {str(code) for code in spec.get("codes") or []}
        error = self.config.get("error") or {}
        if not codes or not error.get("point") or elapsed < float(spec.get("after_sec", 1.0)):
            return ""
        code = self._read(error["point"])
        code = str(int(code)) if isinstance(code, float) and code.is_integer() else str(code)
        if code not in codes:
            return ""
        return f"设备拒绝启动：{(error.get('codes') or {}).get(code, '故障码 ' + code)}（故障码 {code}），设备没有动作"

    def read_actuals(self, job: dict, spec: dict) -> dict:
        actuals = {}
        for name, point in (spec.get("actuals") or {}).items():
            value = self._read(point)
            try:
                actuals[name] = float(value)
            except (TypeError, ValueError) as exc:
                raise AdapterIndeterminate(f"实测点 {point} 的值 {value!r} 不是数值") from exc
        return actuals

    def lookup(self, job: dict) -> bool:
        echo = (self.config.get("job_id") or {}).get("echo")
        if not echo:
            return False
        if str(self._read(echo) or "").strip() != job["id"]:
            return False
        # PLC 回显了这条指令号：启动信号确实到了
        job["handle"] = job["id"]
        job["unconfirmed"] = False
        job["state"] = "accepted"
        return True

    def _signal(self, name: str) -> None:
        spec = self.config.get(name) or {}
        if not spec.get("point"):
            raise AdapterError(f"映射里没有配置 {name} 信号")
        self._pulse(spec, {"hold": "保持", "resume": "恢复", "abort": "终止", "acknowledge": "复位"}[name])

    def hold_job(self, job: dict) -> None:
        self._signal("hold")

    def resume_job(self, job: dict) -> None:
        self._signal("resume")

    def abort_job(self, job: dict | None) -> None:
        self._signal("abort")

    def acknowledge(self) -> bool:
        if not (self.config.get("acknowledge") or {}).get("point"):
            return False
        self._signal("acknowledge")
        time.sleep(float((self.config.get("acknowledge") or {}).get("settle_ms", 200)) / 1000)
        return True
