"""真实接口：一台冷水机（+ 可选的几块搅拌板），包成 `ilcs_gateway.Device`。去重、台账、查询、令牌、TLS 由 SDK 负责。

两个动作（ILCS 能力在配置的 `capabilities` 里对应）：

- `thermostat` 控温：参数 `temp`（℃）、`time`（s，到温之后保温多久，0 = 到温即完成）。写设定值、开控温，
  浴温连续 `settle_sec` 秒在 ±`tolerance_c` 以内算到温（`reach_timeout_sec` 秒没到温判失败，写明最后读数），
  再保温 `time` 秒，完成。
- `stir` 制冷搅拌（配了 `stirrers` 才有）：参数 `temp`、`time`、`rpm`、`position`。冷水机冷着冷块，冷块上的 IKA 板只管搅。
  一条指令几瓶（ILCS 逐孔参数 `wells`），**所有瓶共用一个冷浴温度**：各瓶温度不一样就拒绝。先把冷浴带到温，
  再按各瓶自己的转速启动各自的板，各瓶自己计时，到点停（ILCS 一次都不来查也照停），全部停下且确认了才算完成。

判断规则（`Rejected` = 设备明确没动；其他异常 = 结果未知，SDK 绝不重发）：

- 能力不对、程序没登记、带了不认识的参数、带了物料、温度超出 `min_c`–`max_c` 或冷水机自己的设定范围、转速超出
  位置的极限、位置不对：`Rejected("invalid" / "unsupported")`；
- 冷浴上还有作业、要用的位置不空闲、搅拌子在转：`Rejected("busy")`；动作之前读不到冷水机 / 搅拌板：`Rejected("busy")`；
- 冷水机报警、Julabo 没切到远程控制：`Rejected("interlocked")`；
- 设定值、启动命令冷水机明确不接受：拒绝，设备没动。**写出去了却没确认**：冷水机原来开着（设定值一改就在动）或启动
  命令发出去了，就先把它恢复原样（改回原设定值 / 停下），再按结果未知抛出，交人核查；冷水机原来关着、只写了设定值的，
  冷水机没动，照样拒绝。

后台线程：每条作业一个，`start` 立刻返回；读冷水机（浴温、设定值、启停、报警）、判到温、计时、停板都在线程里，
`status` 不碰设备。运行中冷水机报警、停了、设定值被改、重启过、`lost_after_sec` 秒读不到，作业判失败（先停板）。
作业结束（完成、失败、被终止）之后按 `after` 处理冷水机：`keep` 照最后的设定值接着控温；`standby` 改到 `standby_c`。
停板、回待机温度都确认了才出结论——没确认就一直重试、一直报在跑，不让 ILCS 以为设备已在安全状态。

作业记录写进状态目录：网关重启后照样按作业号答得上来。重启时还没结束的作业**不续做**：先停板、按 `after` 处理冷水机，
再判失败——网关不在的这段时间冷浴温度怎样不知道。
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import logging
import math
from pathlib import Path
import threading
import time
from typing import Any

from ilcs_gateway import Device, Job, ReceiptLost, Rejected, Status

from .chillers import SETPOINT_TOLERANCE, Chiller, ChillerError, Reading, Refused
from .config import Config, Position
from .link import LinkError
from .namur import NamurError, Stirrer

PARAMETERS = {"thermostat": ("temp", "time"), "stir": ("temp", "time", "rpm", "position")}
SINGLE = ""
TERMINAL = {"done", "failed"}
# 转速设定值回读和要求的差多少算没设上
SPEED_TOLERANCE = 10.0
# 实测转速高于它算搅拌子在转；本网关停下不到 SPIN_DOWN_SEC 秒的板还在减速，不算有人在用
IDLE_RPM, SPIN_DOWN_SEC = 20.0, 30.0
# 至少连续几次读不到（且超过 lost_after_sec）才算失去监视；没确认的停止 / 回待机隔多久重试；终止最多等多久
MAX_MISSES, RETRY_SEC, ABORT_WAIT_SEC = 3, 2.0, 15.0
KEEP = 500
log = logging.getLogger("ilcs.gateway.thermostat")


class _StartFailed(Exception):
    """启动搅拌时设备不认（转速设定值回读不对）。"""


@dataclass
class Bottle:
    """制冷搅拌的一瓶：一个孔位在一个位置（一块板）上。"""

    well: str
    position: str
    time: float
    rpm: float
    moved: bool = False          # 启动命令写出去了：板子可能在转
    started: float = 0.0         # 这一瓶开始计时（monotonic）；0 = 还没开始
    wall_started: float = 0.0
    stopping: bool = False
    finished: bool = False       # 停止已确认（或根本没启动）
    failed: str = ""
    rpm_reading: float | None = None
    bath_end: float | None = None
    misses: int = 0
    last_contact: float = 0.0
    next_sample: float = 0.0
    retry_at: float = 0.0
    duration: float | None = None
    stop_note: str = ""

    @property
    def deadline(self) -> float:
        return self.started + self.time

    def public(self) -> dict[str, Any]:
        return {"well": self.well, "position": self.position, "time": self.time, "rpm": self.rpm, "moved": self.moved,
                "wall_started": self.wall_started, "finished": self.finished, "failed": self.failed,
                "rpm_reading": self.rpm_reading, "bath_end": self.bath_end, "duration_s": self.duration,
                "stop_note": self.stop_note}


@dataclass
class Run:
    handle: str
    action: str                  # thermostat / stir
    temp: float
    hold: float = 0.0            # thermostat：到温之后保温多久
    wells: list[str] = field(default_factory=list)   # thermostat：指令里的孔位（逐孔回报同一组实测值）
    bottles: dict[str, Bottle] = field(default_factory=dict)
    state: str = "starting"      # starting / running / done / failed
    phase: str = "reaching"      # reaching / holding / stirring / finishing
    started_ok: bool = False     # 启动完整做完了（找回作业只认这种）
    outcome: str = ""            # 作业本身的结论（done / failed）；停板、回待机都确认之后 state 才跟着变
    fault: str = "none"
    reason: str = ""             # 提前结束的原因：被终止、网关停止服务、网关重启……
    error: str = ""
    note: str = ""               # 运行中的提示（冷水机一时读不到、警告）
    began: float = field(default_factory=time.monotonic)
    wall_began: float = field(default_factory=time.time)
    inside_since: float | None = None
    reached_at: float | None = None
    time_to_reach: float | None = None
    hold_until: float = 0.0
    hold_s: float | None = None
    bath: float | None = None
    deviation: float | None = None
    misses: int = 0
    last_contact: float = 0.0
    next_poll: float = 0.0
    alarmed: bool = False        # 冷水机报警：之后不再给它发命令
    after_done: bool = False
    after_note: str = ""
    retry_at: float = 0.0
    lock: threading.RLock = field(default_factory=threading.RLock)
    wake: threading.Event = field(default_factory=threading.Event)
    over: threading.Event = field(default_factory=threading.Event)
    halt: bool = False
    thread: threading.Thread | None = None

    def public(self) -> dict[str, Any]:
        with self.lock:
            return {"handle": self.handle, "action": self.action, "temp": self.temp, "hold": self.hold,
                    "wells": list(self.wells), "state": self.state, "phase": self.phase, "started_ok": self.started_ok,
                    "outcome": self.outcome, "fault": self.fault, "reason": self.reason, "error": self.error,
                    "note": self.note, "wall_began": self.wall_began, "time_to_reach_s": self.time_to_reach,
                    "hold_s": self.hold_s, "bath": self.bath, "deviation_c": self.deviation,
                    "after_done": self.after_done, "after_note": self.after_note,
                    "bottles": {well: bottle.public() for well, bottle in self.bottles.items()}}


def _where(well: str) -> str:
    return f"孔位 {well} " if well else ""


def _round(value: float | None, digits: int = 1) -> float | None:
    return None if value is None else round(float(value), digits)


def _is_number(value: Any) -> bool:
    # JSON 里可以写 NaN / Infinity：它们和什么比都不成立，会混过范围检查，先挡掉
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


class Station(Device):
    def __init__(self, config: Config, chiller: Chiller, stirrers: dict[str, Stirrer] | None = None, *,
                 state_dir: str | Path | None = None, faults=None):
        stirrers = stirrers or {}
        missing = [key for key in config.keys if key not in stirrers]
        if missing:
            raise ValueError(f"位置 {', '.join(missing)} 没有接口")
        self.config = config
        self.chiller = chiller
        self.stirrers = stirrers
        self.faults = faults  # 只有模拟模式才有：统一控制口的故障注入
        self.lock = threading.RLock()
        self.bath_owner = ""                 # 占着冷浴的作业号：一个冷浴同一时刻只做一条指令
        self.owner: dict[str, str] = {}      # 位置 → 占着它的作业号
        self.settled: dict[str, float] = {}  # 位置 → 本网关最近一次停下它的时刻
        self.runs: dict[str, Run] = {}
        self.info: dict[str, str] | None = None
        self.store = Path(state_dir) / "runs" if state_dir else None
        if self.store:
            self.store.mkdir(parents=True, exist_ok=True)
            self._recover()

    @property
    def label(self) -> str:
        return self.config.chiller.label()

    # ---------- 身份 ----------

    def identity(self) -> dict[str, Any]:
        rows = []
        for key, position in self.config.positions.items():
            try:
                rows.append({"position": key, "name": position.name, "model": self.stirrers[key].name(),
                             "reachable": True})
            except (LinkError, NamurError) as exc:
                rows.append({"position": key, "name": position.name, "reachable": False, "error": str(exc)})
        try:
            if self.info is None:
                self.info = self.chiller.identify()
            alarm, remote = self.chiller.health()
        except (LinkError, ChillerError) as exc:
            raise RuntimeError(f"{self.label}连不上：{exc}") from exc
        interlock = bool(alarm) or (bool(self.faults.interlock) if self.faults else False)
        return {
            "device_id": self.config.device_id, "serial": self.info.get("serial") or self.config.device_id,
            "model": self.config.model or self.info.get("model") or "", "vendor": self.config.vendor,
            "firmware": self.info.get("firmware") or "",
            "methods": [{"program": program.code, "name": program.name,
                         "capability": self.config.capabilities[program.action]}
                        for program in self.config.programs.values()],
            "chiller": {"kind": self.config.chiller.kind, "name": self.label, "link": self.chiller.describe(),
                        "range_c": [self.config.chiller.min_c, self.config.chiller.max_c], "alarm": alarm,
                        "remote": remote},
            "positions": rows, "channels": max(1, len(rows)), "interlock": interlock,
            "accepts_commands": not interlock and remote, "simulator": self.faults is not None,
            # 不做保持，也就没有续跑
            "commands": ["dispatch", "retry", "abort", "query"],
        }

    # ---------- 启动 ----------

    def start(self, job: Job) -> str:
        action = self.config.action_of(job.capability)
        if action is None:
            raise Rejected("unsupported", f"这台设备只做 {', '.join(self.config.capabilities.values())}，"
                                          f"不做 {job.capability}")
        program = self.config.program_for(action, job.program)
        if program is None:
            choices = [code for code, item in self.config.programs.items() if item.action == action]
            raise Rejected("invalid", f"没有登记 {job.capability} 的程序 {job.program or '（未指定）'}；"
                                      f"可选 {', '.join(choices)}")
        if (job.material or {}).get("name"):
            raise Rejected("invalid", f"控温 / 搅拌不投料：这一步带了物料 {job.material['name']}，检查流程")
        rows = self._rows(action, job.params)
        for well, values in rows.items():
            self._check(action, well, values)
        temp, hold = self._shared(action, rows)
        if self.faults:
            self.faults.check_start()
        with self.lock:
            if self.bath_owner:
                raise Rejected("busy", f"冷浴上还有作业 {self.bath_owner}，设备未接受作业：一个冷浴同一时刻只做一条指令")
        layout = self._assign(rows) if action == "stir" else {}
        handle = job.command_id
        run = Run(handle=handle, action=action, temp=temp, hold=hold,
                  wells=list(rows) if action == "thermostat" else [],
                  bottles={well: Bottle(well=well, position=key, time=float(rows[well]["time"]),
                                        rpm=float(rows[well]["rpm"])) for well, key in layout.items()})
        try:
            reading = self._preflight(run)
        except Rejected:
            raise
        except Exception as exc:  # noqa: BLE001  还没动设备：读出意外也是没动，不是结果未知
            raise Rejected("busy", f"读不到设备状态，设备未接受作业：{exc}") from exc
        with self.lock:
            if self.bath_owner or any(self.owner.get(b.position) for b in run.bottles.values()):
                raise Rejected("busy", "冷浴或位置刚被别的作业占了，设备未接受作业")
            self.bath_owner = handle
            for bottle in run.bottles.values():
                self.owner[bottle.position] = handle
            self.runs[handle] = run
        try:
            self._save(run, strict=True)  # 先落盘再动设备：此刻崩掉，重启后也知道冷水机可能被改过
        except OSError as exc:
            self._withdraw(run, save=False)
            raise Rejected("busy", f"作业记录写不进去（{exc}），设备未接受作业：没有记录就不动设备") from exc
        try:
            self._prepare(run)
        except Exception as exc:  # noqa: BLE001  只写了转速设定值、没发启动命令：设备没动
            self._withdraw(run)
            if isinstance(exc, Rejected):
                raise
            raise Rejected("busy", f"转速设定值写不进去，设备未接受作业：{exc}") from exc
        self._run_up(run, reading)
        mode = self.faults.moved() if self.faults else "none"
        with run.lock:
            run.fault, run.state, run.started_ok = mode, "running", True
            run.began, run.wall_began = time.monotonic(), time.time()
        # 先起后台线程再写记录：冷水机已经在动，这之后出什么错都不能没人管
        run.thread = threading.Thread(target=self._supervise, args=(run,), daemon=True, name=f"chill-{handle}")
        run.thread.start()
        self._save(run)
        if mode == "slow_submit":
            time.sleep(self.faults.parameter)
        if mode == "lost_receipt":
            raise ReceiptLost(handle)
        return handle

    def _rows(self, action: str, params: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """{孔位: 参数}。不带 wells 时是一瓶（孔位键为空）；孔位没写的参数用顶层的。"""
        allowed = PARAMETERS[action]
        names = "、".join(allowed)
        unknown = sorted(set(params) - set(allowed) - {"wells"})
        if unknown:
            raise Rejected("invalid", f"网关不接受参数 {', '.join(unknown)}；{action} 只认 {names} 与 wells")
        defaults = {key: value for key, value in params.items() if key != "wells"}
        raw = params.get("wells")
        if raw is None:
            return {SINGLE: defaults}
        if not isinstance(raw, dict) or not raw:
            raise Rejected("invalid", f"wells 要写成 {{孔位: {{{names}}}}}，至少一瓶")
        rows: dict[str, dict[str, Any]] = {}
        for well, values in raw.items():
            if not str(well).strip() or not isinstance(values, dict):
                raise Rejected("invalid", f"孔位 {well!r} 的参数要写成对象")
            extra = sorted(set(values) - set(allowed))
            if extra:
                raise Rejected("invalid", f"孔位 {well} 带了网关不接受的参数 {', '.join(extra)}；每个孔位只认 {names}")
            rows[str(well)] = {**defaults, **values}
        return rows

    def _check(self, action: str, well: str, values: dict[str, Any]) -> None:
        where = _where(well)
        units = {"temp": "℃", "time": "s", "rpm": "rpm"}
        for name in ("temp", "time") + (("rpm",) if action == "stir" else ()):
            value = values.get(name)
            if not _is_number(value):
                raise Rejected("invalid", f"{where}{name} = {value!r} 不是数（{units[name]}）" if value is not None
                               else f"{where}缺 {name}（{units[name]}）")
        spec = self.config.chiller
        temp, seconds = float(values["temp"]), float(values["time"])
        if not spec.min_c <= temp <= spec.max_c:
            raise Rejected("invalid", f"{where}要 {temp:g} ℃：这一站的温度范围是 {spec.min_c:g}–{spec.max_c:g} ℃")
        if action == "thermostat" and seconds < 0:
            raise Rejected("invalid", f"{where}time = {seconds:g} s：保温时长不能是负数（0 = 到温即完成）")
        if action == "stir":
            if seconds <= 0:
                raise Rejected("invalid", f"{where}time = {seconds:g} s：搅拌时长要是正数")
            if float(values["rpm"]) < 0:
                raise Rejected("invalid", f"{where}rpm = {values['rpm']:g}：转速不能是负数")

    @staticmethod
    def _shared(action: str, rows: dict[str, dict[str, Any]]) -> tuple[float, float]:
        """所有瓶共用一个冷浴：温度只能一个（控温时保温时长也只能一个）。返回 (温度, 保温时长)。"""
        for name, what in (("temp", "温度"),) + ((("time", "保温时长"),) if action == "thermostat" else ()):
            groups: dict[float, list[str]] = {}
            for well, values in rows.items():
                groups.setdefault(round(float(values[name]), 2), []).append(well or "（这一瓶）")
            if len(groups) > 1:
                unit = "℃" if name == "temp" else "s"
                detail = "，".join(f"{'、'.join(wells)} 要 {value:g} {unit}" for value, wells in groups.items())
                raise Rejected("invalid", f"同一个冷浴只能一个{what}：{detail}")
        first = next(iter(rows.values()))
        return float(first["temp"]), float(first["time"]) if action == "thermostat" else 0.0

    def _resolve(self, value: Any) -> str:
        keys = self.config.keys
        if isinstance(value, bool):
            raise Rejected("invalid", f"position = {value!r} 不是位置")
        if isinstance(value, (int, float)) and math.isfinite(value) and float(value).is_integer() \
                and 1 <= int(value) <= len(keys):
            return keys[int(value) - 1]
        if isinstance(value, str) and value.strip() in self.config.positions:
            return value.strip()
        raise Rejected("invalid", f"position = {value!r} 不是登记的位置：写 1–{len(keys)} 的序号或 {', '.join(keys)}")

    def _limit(self, well: str, values: dict[str, Any], position: Position) -> None:
        rpm, label = float(values["rpm"]), position.label()
        if rpm > position.max_rpm:
            raise Rejected("invalid", f"{_where(well)}要 {rpm:g} rpm，{label} 最高 {position.max_rpm:g} rpm")
        if 0 < rpm < position.min_rpm:
            raise Rejected("invalid", f"{_where(well)}要 {rpm:g} rpm，{label} 最低 {position.min_rpm:g} rpm（0 表示不搅）")

    def _assign(self, rows: dict[str, dict[str, Any]]) -> dict[str, str]:
        """每瓶落到哪个位置：指定的先核对（登记过、不重复、转速在这个位置的极限里、空闲），
        没指定的按登记顺序挑空闲的（要打开 auto_position）。"""
        layout: dict[str, str] = {}
        owner: dict[str, str] = {}
        for well, values in rows.items():
            if values.get("position") is None:
                continue
            key = self._resolve(values["position"])
            if key in owner:
                raise Rejected("invalid", f"孔位 {owner[key]} 和 {well} 都要用位置 {key}：一个位置只能放一瓶")
            layout[well], owner[key] = key, well
        for well, key in layout.items():
            self._limit(well, rows[well], self.config.positions[key])
        with self.lock:
            taken = set(self.owner)
        busy = [key for key in layout.values() if key in taken]
        if busy:
            raise Rejected("busy", f"位置 {', '.join(busy)} 上还有作业，设备未接受作业")
        missing = [well for well in rows if well not in layout]
        if missing:
            if not self.config.auto_position:
                where = f"（孔位 {', '.join(missing)}）" if missing != [SINGLE] else ""
                raise Rejected("invalid", f"指令没带位置（position）{where}：瓶子放在哪块搅拌板上要由 ILCS 指定")
            free = [key for key in self.config.keys if key not in owner and key not in taken]
            if len(free) < len(missing):
                raise Rejected("busy", f"空闲的位置只有 {len(free)} 个，这条指令要 {len(missing)} 个，设备未接受作业")
            for well, key in zip(missing, free):
                self._limit(well, rows[well], self.config.positions[key])
                layout[well] = key
        return {well: layout[well] for well in rows}

    def _preflight(self, run: Run) -> Reading:
        """动设备之前只读：冷水机读得到、没报警、接受远程命令、温度在它自己的设定范围里；要用的板读得到、搅拌子没在转。"""
        try:
            reading = self.chiller.poll()
            low, high = self.chiller.limits()
        except (LinkError, ChillerError) as exc:
            raise Rejected("busy", f"读不到{self.label}（{exc}），设备未接受作业") from exc
        if reading.alarm:
            raise Rejected("interlocked", f"{self.label}报警（{reading.alarm}），设备未接受作业：现场处理、复位之后再做")
        if not reading.remote:
            raise Rejected("interlocked", f"{self.label}在面板控制模式，不执行远程命令：在面板上切到远程控制，设备未接受作业")
        if (low is not None and run.temp < low - SETPOINT_TOLERANCE) or \
                (high is not None and run.temp > high + SETPOINT_TOLERANCE):
            span = "–".join("—" if value is None else f"{value:g}" for value in (low, high))
            raise Rejected("invalid", f"要 {run.temp:g} ℃，{self.label}自己的设定值范围是 {span} ℃"
                                      "（冷水机菜单里设的）：设备没动")
        now = time.monotonic()
        for bottle in run.bottles.values():
            if bottle.rpm <= 0:
                continue  # 不搅的瓶不碰板子
            position = self.config.positions[bottle.position]
            try:
                speed = self.stirrers[bottle.position].speed()
            except (LinkError, NamurError) as exc:
                raise Rejected("busy", f"读不到{position.label()}（{exc}），设备未接受作业") from exc
            if speed > IDLE_RPM and now - self.settled.get(bottle.position, -SPIN_DOWN_SEC) >= SPIN_DOWN_SEC:
                raise Rejected("busy", f"{position.label()} 的搅拌子在转（实测 {speed:g} rpm），不是本网关启动的：现场可能"
                                       "有人在用，设备未接受作业")
        return reading

    def _prepare(self, run: Run) -> None:
        """写转速设定值、回读核对（还没发启动命令，板子没动）。"""
        for bottle in run.bottles.values():
            if bottle.rpm > 0:
                problem = self._speed(bottle, write=True)
                if problem:
                    raise Rejected("invalid", f"{problem}：设备没动")

    def _speed(self, bottle: Bottle, *, write: bool) -> str:
        """（`write` 时先写转速设定值）回读，不对重发一次；还不对返回问题描述。"""
        plate = self.stirrers[bottle.position]
        if write:
            plate.set_speed(bottle.rpm)
        actual = plate.speed_setpoint()
        if abs(actual - bottle.rpm) > SPEED_TOLERANCE:
            plate.set_speed(bottle.rpm)
            actual = plate.speed_setpoint()
        if abs(actual - bottle.rpm) > SPEED_TOLERANCE:
            label = self.config.positions[bottle.position].label()
            return (f"{_where(bottle.well)}{label} 的转速设定值回读 {actual:g} rpm，要求 {bottle.rpm:g} rpm"
                    "（重发一次也不对）")
        return ""

    def _run_up(self, run: Run, reading: Reading) -> None:
        """改设定值、开控温。冷水机明确不接受：拒绝（设备没动）；写出去了没确认：恢复原样，按结果未知抛出。"""
        old, was_running = reading.setpoint, reading.running
        if abs(old - run.temp) > SETPOINT_TOLERANCE:
            try:
                reported = self.chiller.set_setpoint(run.temp)
            except Refused as exc:
                self._withdraw(run)
                raise Rejected("invalid", f"{self.label}不接受设定值 {run.temp:g} ℃（{exc}），设备没动") from exc
            except LinkError as exc:
                if not exc.sent:
                    self._withdraw(run)
                    raise Rejected("busy", f"设定值没写出去（{exc}），设备未接受作业") from exc
                raise self._abandon(run, old, was_running, f"设定值 {run.temp:g} ℃ 写出去之后没有应答（{exc}）") from exc
            except ChillerError as exc:
                raise self._abandon(run, old, was_running, f"设定值 {run.temp:g} ℃ 写出去之后没确认（{exc}）") from exc
            if abs(reported - run.temp) > SETPOINT_TOLERANCE:
                if abs(reported - old) <= SETPOINT_TOLERANCE:
                    self._withdraw(run)
                    raise Rejected("invalid", f"{self.label}没接受设定值 {run.temp:g} ℃（回读还是原来的 {old:g} ℃：超出"
                                              "冷水机自己的设定范围？），设备没动")
                raise self._abandon(run, old, was_running,
                                    f"{self.label}把设定值压成了 {reported:g} ℃（要求 {run.temp:g} ℃）")
        if was_running:
            return
        try:
            self.chiller.start()
        except Refused as exc:
            self._restore(old, run.temp)
            self._withdraw(run)
            raise Rejected("interlocked", f"{self.label}没接受启动（{exc}），设备没动") from exc
        except LinkError as exc:
            if not exc.sent:
                self._restore(old, run.temp)
                self._withdraw(run)
                raise Rejected("busy", f"启动命令没写出去（{exc}），设备未接受作业") from exc
            raise self._abandon(run, old, False, f"启动命令写出去之后没有应答（{exc}）", started=True) from exc
        except ChillerError as exc:
            raise self._abandon(run, old, False, f"发了启动命令、没确认{self.label}开起来（{exc}）",
                                started=True) from exc

    def _restore(self, old: float, current: float) -> bool:
        """把设定值改回原来的（尽力而为）。没改过返回 True；改回并确认了返回 True。"""
        if abs(old - current) <= SETPOINT_TOLERANCE:
            return True
        try:
            return abs(self.chiller.set_setpoint(old) - old) <= SETPOINT_TOLERANCE
        except (LinkError, ChillerError) as exc:
            log.warning("%s 设定值改回 %g ℃ 没成功：%s", self.label, old, exc)
            return False

    def _abandon(self, run: Run, old: float, was_running: bool, problem: str, *, started: bool = False) -> Exception:
        """命令写出去了却没确认：恢复原样（尽力而为），返回要抛出的异常。冷水机原来关着、只写了设定值的，设备没动，
        照样拒绝；否则结果未知，交人核查。"""
        if not was_running and not started:
            restored = self._restore(old, run.temp)
            self._withdraw(run)
            note = f"设定值已改回 {old:g} ℃" if restored else f"设定值可能已经变了（原来 {old:g} ℃）"
            return Rejected("busy", f"{problem}；{self.label}原来没开着，设备没动（{note}）")
        steps = []
        if started:
            try:
                self.chiller.stop()
                steps.append("已发停止并确认停下")
            except (LinkError, ChillerError) as exc:
                steps.append(f"发了停止、没确认（{exc}），{self.label}可能还开着")
        if abs(old - run.temp) > SETPOINT_TOLERANCE:
            steps.append(f"设定值已改回 {old:g} ℃" if self._restore(old, run.temp)
                         else f"设定值没能确认改回 {old:g} ℃")
        with run.lock:
            run.state, run.outcome, run.phase = "failed", "failed", "finishing"
            run.error = f"{problem}：启动没做完，结果未知"
        self._release(run)
        self._save(run)
        run.over.set()
        return RuntimeError(f"{problem}：这条指令结果未知；{'；'.join(steps)}，请到现场核查{self.label}")

    def _release(self, run: Run) -> None:
        with self.lock:
            if self.bath_owner == run.handle:
                self.bath_owner = ""
            for key in [key for key, owner in self.owner.items() if owner == run.handle]:
                self.owner.pop(key)

    def _withdraw(self, run: Run, *, save: bool = True) -> None:
        """设备没动：撤掉占位与记录。"""
        self._release(run)
        with self.lock:
            self.runs.pop(run.handle, None)
        with run.lock:
            run.state, run.outcome, run.error = "failed", "failed", "设备明确拒绝，没有动作"
        if save:
            self._save(run)
        run.over.set()

    # ---------- 后台线程 ----------

    def _supervise(self, run: Run) -> None:
        """读冷水机、判到温、计时、停板、回待机；都确认了才出结论。"""
        while not run.halt and not run.over.is_set():
            run.wake.clear()
            try:
                self._step(run)
                wait = self._idle(run)
            except Exception:  # noqa: BLE001  后台线程不能悄悄死掉：冷水机、板子可能还在动
                log.exception("作业 %s 的后台线程出错", run.handle)
                with run.lock:
                    run.reason = run.reason or "网关后台线程出错，提前结束"
                wait = RETRY_SEC
            if run.over.is_set():
                break
            run.wake.wait(wait)

    def _step(self, run: Run) -> None:
        if not run.outcome:
            with run.lock:
                reason = run.reason
            if reason:
                self._conclude(run, "failed", reason)
            else:
                if time.monotonic() >= run.next_poll:
                    self._poll(run)
                if not run.outcome:
                    self._advance(run, time.monotonic())
        if run.outcome:
            self._wind_down(run)

    def _poll(self, run: Run) -> None:
        spec = self.config.chiller
        try:
            reading = self.chiller.poll()
        except (LinkError, ChillerError) as exc:
            with run.lock:
                run.misses += 1
                silent = time.monotonic() - (run.last_contact or run.began)
                run.note = f"{self.label}读不到（{exc}）"
                run.next_poll = time.monotonic() + self.config.poll_sec
                lost = run.misses >= MAX_MISSES and silent >= self.config.lost_after_sec
            if lost:
                self._conclude(run, "failed", f"{self.label} {silent:.0f} 秒读不到（连续 {run.misses} 次，{exc}）："
                                              "失去监视，提前结束")
            return
        problem = ""
        if reading.alarm:
            problem = f"{self.label}报警：{reading.alarm}"
        elif reading.restarted:
            problem = f"{self.label}重启过（断电？）：远程写的设定值已经回到面板上的值"
        elif not reading.remote:
            problem = f"{self.label}在作业途中切到了面板控制模式：远程命令不再执行"
        elif not reading.running:
            problem = f"{self.label}在作业途中停了（面板上被关，或报警停机）"
        elif abs(reading.setpoint - run.temp) > SETPOINT_TOLERANCE:
            problem = (f"{self.label}的设定值变成了 {reading.setpoint:g} ℃（作业要 {run.temp:g} ℃）：面板上有人改过，"
                       "或冷水机重启过")
        now = time.monotonic()
        with run.lock:
            run.misses, run.last_contact, run.bath, run.note = 0, now, reading.bath, reading.warning
            run.next_poll = now + self.config.poll_sec
            run.alarmed = run.alarmed or bool(reading.alarm)
            inside = abs(reading.bath - run.temp) <= spec.tolerance_c
            if run.phase == "reaching":
                run.inside_since = (now if run.inside_since is None else run.inside_since) if inside else None
            elif run.reached_at is not None:
                run.deviation = max(run.deviation or 0.0, abs(reading.bath - run.temp))
        if problem:
            self._conclude(run, "failed", problem)

    def _advance(self, run: Run, now: float) -> None:
        spec = self.config.chiller
        if run.phase == "reaching":
            if run.inside_since is not None and now - run.inside_since >= spec.settle_sec:
                self._reached(run, now)
            elif now - run.began >= spec.reach_timeout_sec:
                last = f"最后读数 {run.bath:g} ℃" if run.bath is not None else "一直没读到浴温"
                self._conclude(run, "failed", f"{spec.reach_timeout_sec:g} 秒内没到温：设定 {run.temp:g} ℃，{last}"
                                              f"（要连续 {spec.settle_sec:g} 秒在 ±{spec.tolerance_c:g} ℃ 以内）")
            return
        if run.phase == "holding":
            if run.fault != "stuck" and now >= run.hold_until:
                self._conclude(run, "done")
            return
        if run.phase != "stirring":
            return
        for bottle in run.bottles.values():
            if bottle.finished:
                continue
            with run.lock:
                if not bottle.stopping and run.fault != "stuck" and now >= bottle.deadline:
                    bottle.stopping = True
            if bottle.stopping:
                if now >= bottle.retry_at:
                    self._stop_bottle(run, bottle)
                continue
            if bottle.moved and now >= bottle.next_sample:
                self._sample(run, bottle)
        if all(bottle.finished for bottle in run.bottles.values()):
            failed = [bottle.failed for bottle in run.bottles.values() if bottle.failed]
            self._conclude(run, "failed" if failed else "done", "；".join(failed))

    def _reached(self, run: Run, now: float) -> None:
        with run.lock:
            run.reached_at, run.time_to_reach = now, round(now - run.began, 1)
            run.deviation = abs(run.bath - run.temp) if run.bath is not None else None
            if run.action == "thermostat":
                run.phase, run.hold_until = "holding", now + run.hold
            else:
                run.phase = "stirring"
        # 先记下「开始搅拌」再发启动命令：此刻崩掉，重启后也会把这些板停一遍
        self._save(run)
        if run.action == "stir":
            self._start_stirring(run)
            self._save(run)

    def _start_stirring(self, run: Run) -> None:
        """冷浴到温了：按各瓶的转速启动各自的板，各自开始计时。只启动了一部分就出错：启动了的都停下，判失败。"""
        current: Bottle | None = None
        try:
            for bottle in run.bottles.values():
                current = bottle
                if bottle.rpm > 0:
                    try:
                        self.stirrers[bottle.position].start()
                    except LinkError as exc:
                        bottle.moved = bottle.moved or exc.sent
                        raise
                    bottle.moved = True
                now = time.monotonic()
                with run.lock:
                    bottle.started, bottle.wall_started = now, time.time()
                    bottle.next_sample = now + self.config.poll_sec
                if bottle.rpm > 0:
                    # 有的型号启动后转速设定值会复位：回读，不对重发一次（读得到也说明启动命令送到了）
                    problem = self._speed(bottle, write=False)
                    if problem:
                        raise _StartFailed(problem)
        except Exception as exc:  # noqa: BLE001
            started = [self.config.positions[b.position].label() for b in run.bottles.values()
                       if b.moved and b is not current]
            label = self.config.positions[current.position].label() if current is not None else "后面的位置"
            with run.lock:
                for bottle in run.bottles.values():
                    if bottle.moved or bottle.started:
                        bottle.stopping = True
                        bottle.failed = bottle.failed or f"{_where(bottle.well)}启动只做了一部分，已停下"
                    else:
                        bottle.finished, bottle.failed = True, f"{_where(bottle.well)}没有启动"
            done = f"{'、'.join(started)} 已经开始搅拌，" if started else ""
            self._conclude(run, "failed", f"{done}{label} 启动搅拌出错（{exc}）：这条指令只做了一部分，"
                                          "已把启动了的板都停下（停止确认之后才报失败）")

    def _sample(self, run: Run, bottle: Bottle) -> None:
        position = self.config.positions[bottle.position]
        try:
            rpm = self.stirrers[bottle.position].speed()
        except (LinkError, NamurError) as exc:
            with run.lock:
                bottle.misses += 1
                silent = time.monotonic() - (bottle.last_contact or bottle.started)
                if bottle.misses >= MAX_MISSES and silent >= self.config.lost_after_sec and not bottle.failed:
                    bottle.failed = (f"{_where(bottle.well)}{position.label()} {silent:.0f} 秒读不到（连续 {bottle.misses} 次，"
                                     f"{exc}）：失去监视，提前停下这一瓶")
                    bottle.stopping = True
        else:
            with run.lock:
                bottle.rpm_reading, bottle.misses, bottle.last_contact = rpm, 0, time.monotonic()
        bottle.next_sample = time.monotonic() + self.config.poll_sec

    def _stop_bottle(self, run: Run, bottle: Bottle) -> bool:
        """停一块板：先读一次转速，再关搅拌（顺手关加热），紧跟一条读命令确认送到了。没确认返回 False（稍后重试）。"""
        if not bottle.moved:  # 不搅的瓶（0 rpm）：到点就算完
            with run.lock:
                bottle.finished = True
                if bottle.started:
                    bottle.duration = round(time.monotonic() - bottle.started, 1)
                bottle.bath_end = run.bath
            return True
        position, plate = self.config.positions[bottle.position], self.stirrers[bottle.position]
        if bottle.rpm_reading is None or not bottle.stop_note:
            try:
                rpm = plate.speed()
                with run.lock:
                    bottle.rpm_reading = rpm
            except (LinkError, NamurError) as exc:
                log.warning("位置 %s 停之前读不到转速：%s", bottle.position, exc)
        try:
            plate.stop()
            plate.confirm()
        except (LinkError, NamurError) as exc:
            with run.lock:
                bottle.retry_at = time.monotonic() + RETRY_SEC
                bottle.stop_note = (f"{_where(bottle.well)}{position.label()} 的停止还没确认（{exc}）："
                                    "网关在重试，板子可能还在转")
            log.warning("作业 %s %s", run.handle, bottle.stop_note)
            return False
        now = time.monotonic()
        with run.lock:
            bottle.finished, bottle.stop_note = True, ""
            if bottle.started:
                bottle.duration = round(now - bottle.started, 1)
            bottle.bath_end = run.bath
        with self.lock:
            self.settled[bottle.position] = now
        return True

    def _conclude(self, run: Run, outcome: str, error: str = "") -> None:
        """作业本身有结论了：还在搅的瓶转入停止，提前停下的瓶写明原因。停板、回待机之后才报出去。"""
        now = time.monotonic()
        with run.lock:
            if run.outcome:
                return
            run.outcome, run.phase = outcome, "finishing"
            if error:
                run.error = error
            if run.action == "thermostat" and run.reached_at is not None:
                run.hold_s = round(now - run.reached_at, 1)
            for bottle in run.bottles.values():
                if bottle.finished:
                    continue
                if bottle.moved or bottle.started:
                    if not bottle.stopping and outcome == "failed" and not bottle.failed:
                        bottle.failed = f"{_where(bottle.well)}提前停下：{error or run.reason}"
                    bottle.stopping = True
                else:
                    bottle.finished = True  # 还没开始的瓶：什么都没做
        run.wake.set()

    def _wind_down(self, run: Run) -> None:
        now = time.monotonic()
        for bottle in run.bottles.values():
            if not bottle.finished and now >= bottle.retry_at:
                self._stop_bottle(run, bottle)
        if not all(bottle.finished for bottle in run.bottles.values()):
            return
        if not run.after_done:
            if now >= run.retry_at:
                self._after(run)
            if not run.after_done:
                return
        self._finish(run)

    def _after(self, run: Run) -> None:
        """作业之后冷水机怎样：keep 照最后的设定值接着控温；standby 改到 standby_c（确认了才算）。"""
        spec = self.config.chiller
        if spec.after == "keep" or run.alarmed or abs(run.temp - float(spec.standby_c)) <= SETPOINT_TOLERANCE:
            with run.lock:
                run.after_done = True
                run.after_note = f"{self.label}报警，没改待机温度" if spec.after == "standby" and run.alarmed else ""
            return
        try:
            reported = self.chiller.set_setpoint(float(spec.standby_c))
            if abs(reported - float(spec.standby_c)) > SETPOINT_TOLERANCE:
                raise ChillerError(f"回读 {reported:g} ℃")
        except Refused as exc:  # 冷水机明确不收：重试也没用，结论里写明，交现场处理
            with run.lock:
                run.after_done = True
                run.after_note = (f"{self.label}没接受待机温度 {spec.standby_c:g} ℃（{exc}）：冷浴还按 {run.temp:g} ℃ "
                                  "控温，请现场处理")
            log.warning("作业 %s %s", run.handle, run.after_note)
            return
        except (LinkError, ChillerError) as exc:
            with run.lock:
                run.retry_at = time.monotonic() + RETRY_SEC
                run.after_note = f"{self.label}回待机温度 {spec.standby_c:g} ℃ 还没确认（{exc}）：网关在重试"
            log.warning("作业 %s %s", run.handle, run.after_note)
            return
        with run.lock:
            run.after_done, run.after_note = True, ""

    def _finish(self, run: Run) -> None:
        with run.lock:
            if run.fault == "fail" and run.started_ok and run.outcome == "done":
                run.outcome, run.error = "failed", f"模拟故障：{self.label}报警（模拟），这一步判失败"
            if run.outcome == "failed" and not run.error:
                run.error = run.reason or "作业失败"
            if run.after_note:
                run.error = "；".join(part for part in (run.error, run.after_note) if part)
            run.state = run.outcome if run.started_ok else "failed"
        self._release(run)
        self._save(run)
        run.over.set()
        if self.store:  # 结论已经在盘上：内存里只留最近的，旧的查询从记录里读
            with self.lock:
                done = [handle for handle, item in self.runs.items() if item.over.is_set()]
                for handle in done[: max(0, len(done) - 200)]:
                    self.runs.pop(handle, None)

    def _idle(self, run: Run) -> float:
        """下一件事还要等多久。"""
        now = time.monotonic()
        spec = self.config.chiller
        moments: list[float] = []
        if not run.outcome:
            moments.append(run.next_poll)
            if run.phase == "reaching":
                moments.append(run.began + spec.reach_timeout_sec)
                if run.inside_since is not None:
                    moments.append(run.inside_since + spec.settle_sec)
            elif run.phase == "holding" and run.fault != "stuck":
                moments.append(run.hold_until)
        else:
            if not run.after_done:
                moments.append(run.retry_at)
        for bottle in run.bottles.values():
            if bottle.finished:
                continue
            if bottle.stopping or run.outcome:
                moments.append(bottle.retry_at)
            elif bottle.started:
                if bottle.moved:
                    moments.append(bottle.next_sample)
                if run.fault != "stuck":
                    moments.append(bottle.deadline)
        return min(self.config.poll_sec, max(0.01, min(moments, default=now + self.config.poll_sec) - now))

    # ---------- 状态 ----------

    def status(self, job: Job) -> Status:
        run = self.runs.get(job.handle)
        data = run.public() if run is not None else self._load(job.handle)
        if data is None:
            # 网关的记录里没有：不知道这条作业怎样了，交网关照报原状态、人工核查
            raise RuntimeError(f"作业 {job.handle} 不在网关的记录里：不知道冷水机上怎样了")
        return self._status(data)

    def _status(self, data: dict[str, Any]) -> Status:
        bottles = data.get("bottles") or {}
        telemetry = []
        if isinstance(data.get("bath"), (int, float)):
            telemetry.append({"metric": "temp", "value": round(float(data["bath"]), 2), "setpoint": data["temp"]})
        for well, bottle in bottles.items():
            if isinstance(bottle.get("rpm_reading"), (int, float)):
                telemetry.append({"metric": f"rpm@{well}" if well else "rpm",
                                  "value": round(float(bottle["rpm_reading"]), 1), "setpoint": bottle["rpm"]})
        if data["state"] not in TERMINAL:
            notes = [data.get("note"), data.get("after_note")] + [
                bottle.get("stop_note") or bottle.get("failed") for bottle in bottles.values()
                if bottle.get("moved") or bottle.get("wall_started")]
            return Status("running", telemetry=telemetry, error="；".join(note for note in notes if note))
        if data["action"] == "thermostat":
            row = {"setpoint": data["temp"], "temp": _round(data.get("bath"), 2),
                   "time_to_reach_s": data.get("time_to_reach_s"), "hold_s": data.get("hold_s"),
                   "deviation_c": _round(data.get("deviation_c"), 2)}
            row = {key: value for key, value in row.items() if value is not None}
            wells = data.get("wells") or [SINGLE]
            actuals = row if wells == [SINGLE] else {"wells": {well: dict(row) for well in wells}}
        else:
            rows = {well: self._row(bottle, data) for well, bottle in bottles.items() if bottle.get("wall_started")}
            actuals = rows.get(SINGLE, {}) if set(bottles) == {SINGLE} else {"wells": rows}
        return Status(data["state"], actuals=actuals, telemetry=telemetry, error=data.get("error") or "")

    @staticmethod
    def _row(bottle: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
        row: dict[str, Any] = {"position": bottle["position"]}
        bath = bottle.get("bath_end") if isinstance(bottle.get("bath_end"), (int, float)) else data.get("bath")
        if isinstance(bath, (int, float)):
            row["temp"] = _round(bath, 2)
        if isinstance(bottle.get("rpm_reading"), (int, float)):
            row["rpm"] = _round(bottle["rpm_reading"], 0)
        elif not bottle.get("rpm"):
            row["rpm"] = 0.0
        if bottle.get("duration_s") is not None:
            row["duration_s"] = bottle["duration_s"]
        if bottle.get("failed"):
            row["error"] = bottle["failed"]
        return row

    # ---------- 终止、找回、退出 ----------

    def abort(self, job: Job) -> None:
        run = self.runs.get(job.handle)
        if run is None or run.over.is_set():
            return  # 已经结束（或网关重启前就结束了）：设备在安全状态
        with run.lock:
            if not run.outcome:
                run.reason = run.reason or "被终止"
        run.wake.set()
        if not run.over.wait(ABORT_WAIT_SEC):
            notes = "；".join(note for note in [run.after_note] + [b.stop_note for b in run.bottles.values()] if note)
            raise RuntimeError(f"发了停止，{ABORT_WAIT_SEC:g} 秒内还没确认停下（{notes or '还在停'}）："
                               "网关在重试，请到现场核查")
        if run.state == "done":
            # 停止到之前已经做完：如实拒绝终止，原作业照报完成
            what = (f"{run.temp:g} ℃ 到温、保温 {run.hold_s:g} s" if run.action == "thermostat" and run.hold_s is not None
                    else f"{len(run.bottles)} 瓶都搅完了" if run.action == "stir" else "已经完成")
            raise Rejected("invalid", f"来不及终止：这一步已经做完（{what}）")

    def lookup(self, job: Job) -> str | None:
        """启动没拿到应答：只认启动完整做完了的作业。没做完的（已恢复原样）不认，留给人核查。"""
        run = self.runs.get(job.command_id)
        data = run.public() if run is not None else self._load(job.command_id)
        return job.command_id if data is not None and data.get("started_ok") else None

    def close(self, *, stop: bool = True, timeout: float = 5.0) -> None:
        """网关退出。`stop` 时先把在跑的作业停下（停板、按 after 处理冷水机；等不到确认的，下次启动时接着做）；
        `stop=False` 只是让后台线程退出、不动设备（测试里模拟进程崩掉）。"""
        active = [run for run in list(self.runs.values()) if not run.over.is_set() and not run.halt]
        for run in active:
            if stop:
                with run.lock:
                    if not run.outcome:
                        run.reason = run.reason or "网关停止服务"
            else:
                run.halt = True
            run.wake.set()
        deadline = time.monotonic() + timeout
        for run in active:
            if stop:
                run.over.wait(max(0.0, deadline - time.monotonic()))
            run.halt = True
            run.wake.set()
            if run.thread is not None:
                run.thread.join(timeout=max(0.1, deadline - time.monotonic()))
        self.chiller.link.close()
        for plate in self.stirrers.values():
            plate.link.close()

    def fault_target(self):
        return self.faults

    # ---------- 作业记录 ----------

    def _path(self, handle: str) -> Path | None:
        # 文件名取作业号的摘要：作业号里可能有文件名不收的字符
        return self.store / f"{hashlib.sha256(handle.encode('utf-8')).hexdigest()[:24]}.json" if self.store else None

    def _save(self, run: Run, *, strict: bool = False) -> None:
        """写作业记录（临时文件 + 原子替换）。设备动了之后写不进去只记日志、不抛：不能因为记录让后台线程停下。"""
        path = self._path(run.handle)
        if path is None:
            return
        try:
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(run.public(), ensure_ascii=False), encoding="utf-8")
            temporary.replace(path)
        except OSError:
            if strict:
                raise
            log.exception("作业 %s 的记录写不进去：网关重启后按作业号答不上来", run.handle)

    def _load(self, handle: str) -> dict[str, Any] | None:
        path = self._path(handle)
        try:
            return json.loads(path.read_text(encoding="utf-8")) if path is not None and path.exists() else None
        except (OSError, ValueError):
            return None

    def _recover(self) -> None:
        """网关重启：还没结束的作业不续做，停板、按 after 处理冷水机、判失败。旧的结论只留最近 KEEP 条。"""
        files = sorted(self.store.glob("*.json"), key=lambda item: item.stat().st_mtime)
        finished = []
        for path in files:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                log.warning("作业记录 %s 读不懂，跳过", path)
                continue
            if data.get("state") in TERMINAL:
                finished.append(path)
                continue
            self._revive(data)
        for path in finished[: max(0, len(finished) - KEEP)]:
            path.unlink(missing_ok=True)

    def _revive(self, data: dict[str, Any]) -> None:
        started_ok = bool(data.get("started_ok"))
        reason = "网关重启时作业还没结束" if started_ok else "网关在启动途中重启"
        elapsed = f"，开始了约 {time.time() - float(data['wall_began']):.0f} s" if data.get("wall_began") else ""
        stirring = data.get("phase") in {"stirring", "finishing"}
        bottles = {}
        for well, item in (data.get("bottles") or {}).items():
            key = str(item.get("position"))
            if key not in self.stirrers:
                log.error("作业 %s 的位置 %s 已经不在配置里：请到现场核查这块板停了没有", data.get("handle"), key)
                continue
            rpm = float(item["rpm"])
            # 记录停在「开始搅拌」之后：启动命令可能已经发了，记录没来得及写，每块要搅的板都停一遍
            bottle = Bottle(well=well, position=key, time=float(item["time"]), rpm=rpm,
                            moved=bool(item.get("moved")) or (stirring and rpm > 0),
                            wall_started=float(item.get("wall_started") or 0), rpm_reading=item.get("rpm_reading"),
                            finished=bool(item.get("finished")), duration=item.get("duration_s"))
            if not bottle.finished:
                if bottle.moved:
                    bottle.stopping = True
                else:
                    bottle.finished = True
                if bottle.moved or bottle.wall_started:
                    bottle.failed = f"{_where(well)}{self.config.positions[key].label()}：{reason}，已停下，这一瓶没做完"
            bottles[well] = bottle
        temp, hold = float(data["temp"]), float(data.get("hold") or 0)
        plan = (f"控温 {temp:g} ℃、保温 {hold:g} s" if data.get("action") == "thermostat"
                else f"{temp:g} ℃ 制冷搅拌 {len(bottles)} 瓶")
        after = "冷水机照最后的设定值接着控温" if self.config.chiller.after == "keep" else \
            f"冷水机回到待机温度 {self.config.chiller.standby_c:g} ℃"
        run = Run(handle=str(data["handle"]), action=str(data.get("action") or "thermostat"), temp=temp, hold=hold,
                  wells=list(data.get("wells") or []), bottles=bottles, state="running", phase="finishing",
                  started_ok=started_ok, outcome="failed", reason=reason,
                  error=f"{reason}（{plan}{elapsed}）：已停下搅拌、{after}，这一步没做完；网关不在的这段时间冷浴温度怎样不知道",
                  wall_began=float(data.get("wall_began") or time.time()), time_to_reach=data.get("time_to_reach_s"),
                  bath=data.get("bath"))
        with self.lock:
            self.bath_owner = run.handle
            for bottle in bottles.values():
                if not bottle.finished:
                    self.owner[bottle.position] = run.handle
            self.runs[run.handle] = run
        log.warning("作业 %s %s：先停板、处理冷水机", run.handle, reason)
        run.thread = threading.Thread(target=self._supervise, args=(run,), daemon=True, name=f"chill-{run.handle}")
        run.thread.start()
