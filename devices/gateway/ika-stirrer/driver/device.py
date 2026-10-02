"""真实接口：一个工位上的几块 IKA 磁力加热搅拌器，包成 `ilcs_gateway.Device`。去重、台账、查询、令牌、TLS 由 SDK 负责。

一条 ILCS 指令 = 在一个或几个位置上各搅一瓶（一个位置一块板、一个瓶）：

- 不带 `wells`：一瓶，参数 `temp`（℃）、`time`（s）、`rpm`、`position`；
- 带 `wells`（ILCS 的逐孔参数，一个孔位一瓶）：`{孔位: {temp, time, rpm, position}}`，孔位没写的用顶层的值
  （ILCS 里步骤固定参数是逐孔参数的缺省值）。瓶子放在哪个位置通常由「放瓶」人工步骤按样本记下、前馈到这一步。

判断规则：

- 能力不对、程序没登记、带了网关不认识的参数、带了物料：`Rejected`，设备没动；**不认识的参数一律拒绝**，不悄悄忽略；
- 温度低于室温（`ambient_c`）：拒绝——这些板只能加热、**不能制冷**；高于室温才开加热，等于室温只搅拌；
- 超过这个位置的 `max_temp_c` / `max_rpm`、低于 `min_rpm`（0 = 不搅拌）、时长不是正数、既不加热也不搅拌：拒绝；
- 位置不在登记里、两瓶落到同一个位置：`Rejected("invalid")`；要用的位置有一个不空闲（网关还有作业在上面、
  或搅拌子在转）：`Rejected("busy")`，一个都不启动；读不到板子、设定值写进去回读不对：拒绝，设备没动；
- **已经启动了几块、后面出了错**（命令写不出去、启动后设定值回读不对又重发也不对）：先把这条指令启动了的板都停下，
  再抛异常按结果未知处理，交人核查。不报「没动」（板子转过了），也不留无人计时的加热板；
- 其他异常原样抛出：网关按结果未知处理，绝不重发。

计时在网关里：每次启动起一个计时线程，到 `time` 秒就停（STOP_4、STOP_1），ILCS 不来查也照样停。
写命令没有应答，停完紧跟一条读命令确认送到了；**每块板的停止都确认之后才报完成 / 失败**——没确认就一直重试、报在跑，
不让 ILCS 以为设备已经在安全状态。中途某块板 `lost_after_sec` 秒读不到（失去监视）就提前停下这一瓶、判失败。

作业记录写进状态目录：网关重启后照样按作业号答得上来。重启时还没结束的作业**不续时**：先把它的板都停下，再判失败
（写明计划多久、重启前开始了多久）——宁可重做一步，也不留一块没人管的加热板。
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

from .config import Config, Position
from .namur import TEMP_EXTERNAL, Hotplate, LinkError, NamurError

PARAMETERS = ("temp", "time", "rpm", "position")
SINGLE = ""
TERMINAL = {"done", "failed"}
# 设定值回读和要求的差多少算没设上
TEMP_TOLERANCE, SPEED_TOLERANCE = 1.0, 10.0
# 实测转速高于它算搅拌子在转；本网关停下不到 SETTLE_SEC 秒的板还在减速，不算有人在用
IDLE_RPM, SETTLE_SEC = 20.0, 30.0
# 至少连续几次读不到（且超过 lost_after_sec）才算失去监视；停止没确认隔多久重试；终止最多等多久
MAX_MISSES, RETRY_SEC, ABORT_WAIT_SEC = 3, 2.0, 15.0
# 外置探头读数超出这个范围，多半是没插探头
PROBE_RANGE = (-50.0, 400.0)
KEEP = 500
log = logging.getLogger("ilcs.gateway.ika")


class _StartFailed(Exception):
    """启动途中设备不认（设定值回读不对）。"""


@dataclass
class Bottle:
    """一个孔位（一瓶）在一个位置上。"""

    well: str
    position: str
    temp: float
    time: float
    rpm: float
    heat: bool
    moved: bool = False          # 启动命令写出去了：板子可能已经在转 / 在加热
    started: float = 0.0         # 启动时刻（monotonic）
    wall_started: float = 0.0    # 启动时刻（墙钟，写进记录，重启后算开始了多久）
    stopping: bool = False
    finished: bool = False       # 停止已确认（或根本没启动）
    failed: str = ""             # 这一瓶为什么失败；空 = 正常
    reading: dict[str, float] = field(default_factory=dict)
    read_final: bool = False
    misses: int = 0
    last_contact: float = 0.0    # 最近一次读得到这块板（monotonic）
    next_sample: float = 0.0
    next_feed: float = 0.0
    retry_at: float = 0.0
    duration: float | None = None
    stop_note: str = ""

    @property
    def deadline(self) -> float:
        return self.started + self.time

    def public(self) -> dict[str, Any]:
        return {"well": self.well, "position": self.position, "temp": self.temp, "time": self.time, "rpm": self.rpm,
                "heat": self.heat, "moved": self.moved, "wall_started": self.wall_started, "finished": self.finished,
                "failed": self.failed, "reading": dict(self.reading), "duration_s": self.duration,
                "stop_note": self.stop_note}


@dataclass
class Run:
    handle: str
    bottles: dict[str, Bottle]
    state: str = "starting"      # starting / running / done / failed
    started_ok: bool = False     # 启动完整做完了（找回作业只认这种）
    fault: str = "none"
    reason: str = ""             # 提前停的原因：被终止、网关停止服务、网关重启……
    error: str = ""
    lock: threading.RLock = field(default_factory=threading.RLock)
    wake: threading.Event = field(default_factory=threading.Event)
    over: threading.Event = field(default_factory=threading.Event)
    halt: bool = False
    thread: threading.Thread | None = None

    def public(self) -> dict[str, Any]:
        with self.lock:
            return {"handle": self.handle, "state": self.state, "started_ok": self.started_ok, "fault": self.fault,
                    "reason": self.reason, "error": self.error,
                    "bottles": {well: bottle.public() for well, bottle in self.bottles.items()}}


def _where(well: str) -> str:
    return f"孔位 {well} " if well else ""


def _round(value: float | None, digits: int = 1) -> float | None:
    return None if value is None else round(float(value), digits)


class Station(Device):
    def __init__(self, config: Config, plates: dict[str, Hotplate], *, state_dir: str | Path | None = None,
                 faults=None):
        missing = [key for key in config.keys if key not in plates]
        if missing:
            raise ValueError(f"位置 {', '.join(missing)} 没有接口")
        self.config = config
        self.plates = plates
        self.faults = faults  # 只有模拟模式才有：统一控制口的故障注入
        self.lock = threading.RLock()
        self.owner: dict[str, str] = {}      # 位置 → 占着它的作业号
        self.settled: dict[str, float] = {}  # 位置 → 本网关最近一次停下它的时刻
        self.runs: dict[str, Run] = {}
        self.store = Path(state_dir) / "runs" if state_dir else None
        if self.store:
            self.store.mkdir(parents=True, exist_ok=True)
            self._recover()

    # ---------- 身份 ----------

    def identity(self) -> dict[str, Any]:
        rows, problems = [], []
        for key, position in self.config.positions.items():
            try:
                reported = self.plates[key].name()
                rows.append({"position": key, "name": position.name, "model": reported, "reachable": True})
            except (LinkError, NamurError) as exc:
                rows.append({"position": key, "name": position.name, "reachable": False, "error": str(exc)})
                problems.append(f"{position.label()}：{exc}")
        if len(problems) == len(rows):
            raise RuntimeError("一块加热板都连不上：" + "；".join(problems))
        interlock = bool(self.faults.interlock) if self.faults else False
        return {
            "device_id": self.config.device_id, "serial": self.config.device_id,
            "model": self.config.model or next((row["model"] for row in rows if row.get("model")), ""),
            "vendor": self.config.vendor, "firmware": "",
            "methods": [{"program": code, "name": name, "capability": self.config.capability}
                        for code, name in self.config.programs.items()],
            "positions": rows, "channels": len(rows), "interlock": interlock, "accepts_commands": not interlock,
            "simulator": self.faults is not None,
            # 加热板不做保持，也就没有续跑
            "commands": ["dispatch", "retry", "abort", "query"],
        }

    # ---------- 启动 ----------

    def start(self, job: Job) -> str:
        if job.capability != self.config.capability:
            raise Rejected("unsupported", f"这台设备只做 {self.config.capability}，不做 {job.capability}")
        code = job.program or self.config.default_program
        if code not in self.config.programs:
            raise Rejected("invalid", f"没有登记程序 {code or '（未指定）'}；可选 {', '.join(self.config.programs)}")
        if (job.material or {}).get("name"):
            raise Rejected("invalid", f"搅拌不投料：这一步带了物料 {job.material['name']}，检查流程")
        rows = self._rows(job.params)
        for well, values in rows.items():
            self._check(well, values)
        if self.faults:
            self.faults.check_start()
        layout = self._assign(rows)
        handle = job.command_id
        bottles = {well: Bottle(well=well, position=key, temp=float(rows[well]["temp"]),
                                time=float(rows[well]["time"]), rpm=float(rows[well]["rpm"]),
                                heat=float(rows[well]["temp"]) > self.config.ambient_c)
                   for well, key in layout.items()}
        try:
            self._preflight(bottles)
        except Rejected:
            raise
        except Exception as exc:  # noqa: BLE001  还没发启动命令：读不到就是没动，不是结果未知
            raise Rejected("busy", f"读不到加热板状态，设备未接受作业：{exc}") from exc
        run = Run(handle=handle, bottles=bottles)
        with self.lock:
            for bottle in bottles.values():
                self.owner[bottle.position] = handle
            self.runs[handle] = run
        try:
            self._save(run, strict=True)  # 先落盘再动设备：此刻崩掉，重启后也知道这几块板可能被启动了
        except OSError as exc:
            self._withdraw(run, save=False)
            raise Rejected("busy", f"作业记录写不进去（{exc}），设备未接受作业：没有记录就不动设备") from exc
        try:
            self._setpoints(bottles)
        except Exception as exc:  # noqa: BLE001  只写了设定值、没发启动命令：设备没动
            self._withdraw(run)
            if isinstance(exc, Rejected):
                raise
            raise Rejected("busy", f"设定值写不进去，设备未接受作业：{exc}") from exc
        mode = self._run_up(run)
        run.fault = mode
        with run.lock:
            run.state, run.started_ok = "running", True
        # 先起计时线程再写记录：板子已经在转，这之后出什么错都不能让它没人计时
        run.thread = threading.Thread(target=self._supervise, args=(run,), daemon=True, name=f"stir-{handle}")
        run.thread.start()
        self._save(run)
        if mode == "slow_submit":
            time.sleep(self.faults.parameter)
        if mode == "lost_receipt":
            raise ReceiptLost(handle)
        return handle

    def _rows(self, params: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """{孔位: 参数}。不带 wells 时是一瓶（孔位键为空）；孔位没写的参数用顶层的。"""
        unknown = sorted(set(params) - set(PARAMETERS) - {"wells"})
        if unknown:
            raise Rejected("invalid", f"网关不接受参数 {', '.join(unknown)}；只认 temp（℃）、time（s）、rpm、position 与 wells")
        defaults = {key: value for key, value in params.items() if key != "wells"}
        raw = params.get("wells")
        if raw is None:
            return {SINGLE: defaults}
        if not isinstance(raw, dict) or not raw:
            raise Rejected("invalid", "wells 要写成 {孔位: {temp, time, rpm, position}}，至少一瓶")
        rows: dict[str, dict[str, Any]] = {}
        for well, values in raw.items():
            if not str(well).strip() or not isinstance(values, dict):
                raise Rejected("invalid", f"孔位 {well!r} 的参数要写成对象，如 {{\"position\": 2}}")
            extra = sorted(set(values) - set(PARAMETERS))
            if extra:
                raise Rejected("invalid", f"孔位 {well} 带了网关不接受的参数 {', '.join(extra)}；每个孔位只认 "
                                          "temp、time、rpm、position")
            rows[str(well)] = {**defaults, **values}
        return rows

    def _check(self, well: str, values: dict[str, Any], position: Position | None = None) -> None:
        """参数核对。不带 position 时只查和位置无关的；带了再查这个位置的极限。"""
        where = _where(well)
        for name, unit in (("temp", "℃"), ("time", "s"), ("rpm", "rpm")):
            value = values.get(name)
            # JSON 里可以写 NaN / Infinity：它们和什么比都不成立，会混过下面的范围检查，先挡掉
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise Rejected("invalid", f"{where}{name} = {value!r} 不是数（{unit}）" if value is not None
                               else f"{where}缺 {name}（{unit}）")
        temp, seconds, rpm = (float(values[name]) for name in ("temp", "time", "rpm"))
        ambient = self.config.ambient_c
        if seconds <= 0:
            raise Rejected("invalid", f"{where}time = {seconds:g} s：搅拌时长要是正数")
        if temp < ambient:
            raise Rejected("invalid", f"{where}要 {temp:g} ℃：这台加热板不能制冷，最低只到室温 {ambient:g} ℃"
                                      "（制冷要用带冷却的工位）")
        if rpm < 0:
            raise Rejected("invalid", f"{where}rpm = {rpm:g}：转速不能是负数")
        if rpm == 0 and temp <= ambient:
            raise Rejected("invalid", f"{where}不加热（{temp:g} ℃ 不高于室温）也不搅拌（0 rpm）：这一步设备什么都不做，检查参数")
        if position is None:
            return
        label = position.label()
        if temp > position.max_temp_c:
            raise Rejected("invalid", f"{where}要 {temp:g} ℃，{label} 最高 {position.max_temp_c:g} ℃")
        if rpm > position.max_rpm:
            raise Rejected("invalid", f"{where}要 {rpm:g} rpm，{label} 最高 {position.max_rpm:g} rpm")
        if 0 < rpm < position.min_rpm:
            raise Rejected("invalid", f"{where}要 {rpm:g} rpm，{label} 最低 {position.min_rpm:g} rpm（0 表示不搅拌）")

    def _resolve(self, value: Any) -> str:
        keys = self.config.keys
        if isinstance(value, bool):
            raise Rejected("invalid", f"position = {value!r} 不是位置")
        if isinstance(value, (int, float)) and float(value).is_integer() and 1 <= int(value) <= len(keys):
            return keys[int(value) - 1]
        if isinstance(value, str) and value.strip() in self.config.positions:
            return value.strip()
        raise Rejected("invalid", f"position = {value!r} 不是登记的位置：写 1–{len(keys)} 的序号或 {', '.join(keys)}")

    def _assign(self, rows: dict[str, dict[str, Any]]) -> dict[str, str]:
        """每瓶落到哪个位置：指定的先核对（登记过、不重复、参数在这个位置的极限里、空闲），
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
            self._check(well, rows[well], self.config.positions[key])
        with self.lock:
            taken = set(self.owner)
        busy = [key for key in layout.values() if key in taken]
        if busy:
            raise Rejected("busy", f"位置 {', '.join(busy)} 上还有作业，设备未接受作业")
        missing = [well for well in rows if well not in layout]
        if missing:
            if not self.config.auto_position:
                where = f"（孔位 {', '.join(missing)}）" if missing != [SINGLE] else ""
                raise Rejected("invalid", f"指令没带位置（position）{where}：瓶子放在哪块加热板上要由 ILCS 指定")
            free = [key for key in self.config.keys if key not in owner and key not in taken]
            if len(free) < len(missing):
                raise Rejected("busy", f"空闲的位置只有 {len(free)} 个，这条指令要 {len(missing)} 个，设备未接受作业")
            for well, key in zip(missing, free):
                self._check(well, rows[well], self.config.positions[key])
                layout[well] = key
        return {well: layout[well] for well in rows}

    def _preflight(self, bottles: dict[str, Bottle]) -> None:
        """动设备之前：每块板读得到、搅拌子没在转（不是本网关刚停下的）、外置探头有读数。"""
        now = time.monotonic()
        for bottle in bottles.values():
            position = self.config.positions[bottle.position]
            plate = self.plates[bottle.position]
            try:
                speed = plate.speed()
                temp = plate.temperature(position.channel)
            except (LinkError, NamurError) as exc:
                raise Rejected("busy", f"读不到{position.label()}（{exc}），设备未接受作业") from exc
            if speed > IDLE_RPM and now - self.settled.get(bottle.position, -SETTLE_SEC) >= SETTLE_SEC:
                raise Rejected("busy", f"{position.label()} 的搅拌子在转（实测 {speed:g} rpm），不是本网关启动的：现场可能有人"
                                       "在用，设备未接受作业")
            if position.channel == TEMP_EXTERNAL and not PROBE_RANGE[0] <= temp <= PROBE_RANGE[1]:
                raise Rejected("invalid", f"{position.label()} 外置探头读数 {temp:g} ℃ 不像真的：没插 PT1000 探头？"
                                          "设备未动作")

    def _setpoints(self, bottles: dict[str, Bottle]) -> None:
        """写设定值、回读核对（还没发启动命令，设备没动）。看门狗打开时先设好安全值、开始喂。"""
        for bottle in bottles.values():
            plate = self.plates[bottle.position]
            if self.config.watchdog_sec:
                plate.arm_watchdog(self.config.watchdog_sec, self.config.watchdog_temp_c, self.config.watchdog_rpm)
            problem = self._apply(bottle, write=True)
            if problem:
                raise Rejected("invalid", f"{problem}：设备没动")

    def _apply(self, bottle: Bottle, *, write: bool) -> str:
        """（`write` 时先写设定值）回读，不对重发一次；还不对返回问题描述。"""
        plate = self.plates[bottle.position]
        label = self.config.positions[bottle.position].label()
        checks = []
        if bottle.rpm > 0:
            checks.append((4, bottle.rpm, SPEED_TOLERANCE, plate.set_speed, "转速", "rpm"))
        if bottle.heat:
            checks.append((1, bottle.temp, TEMP_TOLERANCE, plate.set_temperature, "温度", "℃"))
        for channel, wanted, tolerance, setter, name, unit in checks:
            if write:
                setter(wanted)
            actual = plate.setpoint(channel)
            if abs(actual - wanted) > tolerance:
                setter(wanted)
                actual = plate.setpoint(channel)
            if abs(actual - wanted) > tolerance:
                return f"{_where(bottle.well)}{label} 的{name}设定值回读 {actual:g} {unit}，要求 {wanted:g} {unit}（重发一次也不对）"
        return ""

    def _run_up(self, run: Run) -> str:
        """发启动命令。返回动作那一刻的故障模式（模拟设备）。第一条启动命令就没写出去：设备没动，拒绝；
        已经启动了几块后面出了错：把启动了的停下，按结果未知抛出。"""
        mode = "none"
        current: Bottle | None = None
        try:
            for bottle in run.bottles.values():
                current = bottle
                plate = self.plates[bottle.position]
                for command in ([plate.start_motor] if bottle.rpm > 0 else []) + ([plate.start_heater] if bottle.heat else []):
                    try:
                        command()
                    except LinkError as exc:
                        bottle.moved = bottle.moved or exc.sent
                        raise
                    except Exception:
                        bottle.moved = True
                        raise
                    bottle.moved = True
                bottle.started, bottle.wall_started = time.monotonic(), time.time()
                if self.faults:
                    moved = self.faults.moved()
                    mode = moved if mode == "none" else mode
                # 有的型号 START_1 之后设定值会复位：回读，不对重发一次（读得到也说明启动命令送到了）
                problem = self._apply(bottle, write=False)
                if problem:
                    raise _StartFailed(problem)
        except Exception as exc:  # noqa: BLE001
            if not any(bottle.moved for bottle in run.bottles.values()):
                self._withdraw(run)
                raise Rejected("busy", f"启动命令没发出去（{exc}），设备未接受作业") from exc
            raise self._abandon(run, current, exc) from exc
        return mode

    def _withdraw(self, run: Run, *, save: bool = True) -> None:
        """设备没动：撤掉占位与记录，关掉已经打开的看门狗。"""
        for bottle in run.bottles.values():
            if self.config.watchdog_sec:
                try:
                    self.plates[bottle.position].clear_watchdog()
                except (LinkError, NamurError) as exc:
                    log.warning("位置 %s 关看门狗没成功：%s", bottle.position, exc)
        with self.lock:
            for bottle in run.bottles.values():
                if self.owner.get(bottle.position) == run.handle:
                    self.owner.pop(bottle.position)
            self.runs.pop(run.handle, None)
        with run.lock:
            run.state, run.error = "failed", "设备明确拒绝，没有动作"
        if save:
            self._save(run)
        run.over.set()

    def _abandon(self, run: Run, current: Bottle | None, exc: Exception) -> RuntimeError:
        """只启动了一部分：把启动了的板停下，返回要抛出的异常（结果未知，交人核查）。停不下的交给计时线程一直重试。"""
        started = [self.config.positions[b.position].label() for b in run.bottles.values() if b.moved and b is not current]
        label = self.config.positions[current.position].label() if current is not None else "后面的位置"
        with run.lock:
            run.reason = "启动只做了一部分"
            for bottle in run.bottles.values():
                if bottle.moved:
                    bottle.stopping, bottle.failed = True, "启动只做了一部分，已回退"
                else:
                    bottle.finished, bottle.failed = True, "没有启动"
        for bottle in run.bottles.values():
            if not bottle.moved:
                with self.lock:
                    if self.owner.get(bottle.position) == run.handle:
                        self.owner.pop(bottle.position)
                if self.config.watchdog_sec:  # 没启动的板只写过设定值、开过看门狗：关掉
                    try:
                        self.plates[bottle.position].clear_watchdog()
                    except (LinkError, NamurError) as exc:
                        log.warning("位置 %s 关看门狗没成功：%s", bottle.position, exc)
        # 每块都要停一次，不能停到第一块没确认就不管后面的
        try:
            results = [self._stop_bottle(run, bottle) for bottle in run.bottles.values() if not bottle.finished]
        except Exception:  # noqa: BLE001  停的途中出了意外：交给计时线程接着停，不能扔下不管
            log.exception("作业 %s 回退时出错", run.handle)
            results = [False]
        confirmed = all(results)
        if confirmed:
            self._finish(run)
        else:
            run.thread = threading.Thread(target=self._supervise, args=(run,), daemon=True, name=f"stir-{run.handle}")
            run.thread.start()
        done = f"{'、'.join(started)} 已经启动，" if started else ""
        stopped = "已把这条指令启动了的板都停下（停止已确认）" if confirmed else \
            "已发停止、还没确认停下（网关在重试，板子可能还在加热搅拌）"
        return RuntimeError(f"{done}{label} 启动出错（{exc}）：这条指令只做了一部分；{stopped}，请到现场核查")

    # ---------- 计时线程 ----------

    def _supervise(self, run: Run) -> None:
        """到点停、采样、喂看门狗；每块板的停止都确认了才出结论。"""
        while not run.halt:
            run.wake.clear()
            try:
                now = time.monotonic()
                active = [bottle for bottle in run.bottles.values() if not bottle.finished]
                if not active:
                    break
                for bottle in active:
                    if run.halt:
                        return
                    with run.lock:
                        due = run.fault != "stuck" and now >= bottle.deadline
                        if not bottle.stopping and (run.reason or due):
                            bottle.stopping = True
                    if bottle.stopping:
                        if now >= bottle.retry_at:
                            self._stop_bottle(run, bottle)
                        continue
                    if now >= bottle.next_sample:
                        self._sample(run, bottle)
                    if self.config.watchdog_sec and now >= bottle.next_feed and not bottle.stopping:
                        self._feed(run, bottle)
                wait = self._idle(run)
            except Exception:  # noqa: BLE001  计时线程不能悄悄死掉：板子可能还在动，提前停下、接着重试
                log.exception("作业 %s 的计时线程出错", run.handle)
                with run.lock:
                    run.reason = run.reason or "网关计时线程出错，提前停下"
                wait = RETRY_SEC
            run.wake.wait(wait)
        if not run.halt:
            self._finish(run)

    def _idle(self, run: Run) -> float:
        """下一件事还要等多久。"""
        now = time.monotonic()
        moments = []
        for bottle in run.bottles.values():
            if bottle.finished:
                continue
            if bottle.stopping:
                moments.append(bottle.retry_at)
                continue
            moments.append(bottle.next_sample)
            if run.fault != "stuck":
                moments.append(bottle.deadline)
            if self.config.watchdog_sec:
                moments.append(bottle.next_feed)
        return min(self.config.poll_sec, max(0.01, min(moments, default=now) - now))

    def _sample(self, run: Run, bottle: Bottle) -> None:
        position = self.config.positions[bottle.position]
        plate = self.plates[bottle.position]
        try:
            reading = {"temp": plate.temperature(position.channel), "rpm": plate.speed()}
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
                bottle.reading, bottle.misses, bottle.last_contact = reading, 0, time.monotonic()
        bottle.next_sample = time.monotonic() + self.config.poll_sec

    def _feed(self, run: Run, bottle: Bottle) -> None:
        try:
            self.plates[bottle.position].feed_watchdog(self.config.watchdog_sec)
        except (LinkError, NamurError) as exc:
            log.warning("位置 %s 喂看门狗没成功：%s", bottle.position, exc)
            with run.lock:
                bottle.misses += 1
        bottle.next_feed = time.monotonic() + self.config.watchdog_sec / 3

    def _stop_bottle(self, run: Run, bottle: Bottle) -> bool:
        """停一块板：先读一次实测值，再关搅拌、关加热，紧跟一条读命令确认送到了。没确认返回 False（稍后重试）。"""
        position = self.config.positions[bottle.position]
        plate = self.plates[bottle.position]
        if not bottle.read_final:
            bottle.read_final = True
            try:
                reading = {"temp": plate.temperature(position.channel), "rpm": plate.speed()}
                with run.lock:
                    bottle.reading = reading
            except (LinkError, NamurError) as exc:
                log.warning("位置 %s 停之前读不到实测值：%s", bottle.position, exc)
        try:
            plate.stop_motor()
            plate.stop_heater()
            plate.confirm()
        except (LinkError, NamurError) as exc:
            with run.lock:
                bottle.retry_at = time.monotonic() + RETRY_SEC
                bottle.stop_note = (f"{_where(bottle.well)}{position.label()} 的停止还没确认（{exc}）："
                                    "网关在重试，板子可能还在加热搅拌")
            log.warning("作业 %s %s", run.handle, bottle.stop_note)
            return False
        if self.config.watchdog_sec:
            try:
                plate.clear_watchdog()
            except (LinkError, NamurError) as exc:  # 板子已经停了：看门狗之后触发也只是改设定值
                log.warning("位置 %s 关看门狗没成功：%s", bottle.position, exc)
        now = time.monotonic()
        with run.lock:
            bottle.finished, bottle.stop_note = True, ""
            if bottle.started:
                bottle.duration = round(now - bottle.started, 1)
            if run.reason and not bottle.failed and bottle.started and now < bottle.deadline - 0.05:
                bottle.failed = f"{_where(bottle.well)}{position.label()}：{run.reason}"
        with self.lock:
            if self.owner.get(bottle.position) == run.handle:
                self.owner.pop(bottle.position)
            self.settled[bottle.position] = now
        return True

    def _finish(self, run: Run) -> None:
        with run.lock:
            errors = [bottle.failed for bottle in run.bottles.values() if bottle.failed and bottle.moved]
            if run.fault == "fail" and run.started_ok:
                errors.append("模拟故障：加热板报错（ER 4，温度传感器），这一步判失败")
            if run.reason and not errors:
                errors.append(run.reason)
            run.state = "failed" if errors or not run.started_ok else "done"
            run.error = "；".join(dict.fromkeys(errors))
        self._save(run)
        run.over.set()
        if self.store:  # 结论已经在盘上：内存里只留最近的，旧的查询从记录里读
            with self.lock:
                done = [handle for handle, item in self.runs.items() if item.over.is_set()]
                for handle in done[: max(0, len(done) - 200)]:
                    self.runs.pop(handle, None)

    # ---------- 状态 ----------

    def status(self, job: Job) -> Status:
        run = self.runs.get(job.handle)
        data = run.public() if run is not None else self._load(job.handle)
        if data is None:
            # 网关的记录里没有：不知道这条作业怎样了，交网关照报原状态、人工核查
            raise RuntimeError(f"作业 {job.handle} 不在网关的记录里：不知道它在加热板上怎样了")
        return self._status(data)

    def _status(self, data: dict[str, Any]) -> Status:
        bottles = data["bottles"]
        single = set(bottles) == {SINGLE}
        telemetry = []
        for well, bottle in bottles.items():
            reading = bottle.get("reading") or {}
            suffix = f"@{well}" if well else ""
            if isinstance(reading.get("temp"), (int, float)):
                telemetry.append({"metric": f"temp{suffix}", "value": round(float(reading["temp"]), 2),
                                  "setpoint": bottle["temp"] if bottle["heat"] else None})
            if isinstance(reading.get("rpm"), (int, float)):
                telemetry.append({"metric": f"rpm{suffix}", "value": round(float(reading["rpm"]), 1),
                                  "setpoint": bottle["rpm"]})
        if data["state"] not in TERMINAL:
            notes = [bottle.get("stop_note") or bottle.get("failed") for bottle in bottles.values()
                     if (bottle.get("stop_note") or bottle.get("failed")) and bottle.get("moved")]
            return Status("running", telemetry=telemetry, error="；".join(notes))
        rows = {well: self._actuals(bottle) for well, bottle in bottles.items() if bottle.get("moved")}
        actuals = rows.get(SINGLE, {}) if single else {"wells": rows}
        return Status(data["state"], actuals=actuals, telemetry=telemetry, error=data.get("error") or "")

    def _actuals(self, bottle: dict[str, Any]) -> dict[str, Any]:
        position = self.config.positions.get(bottle["position"])
        reading = bottle.get("reading") or {}
        row: dict[str, Any] = {"position": bottle["position"], "sensor": position.sensor if position else ""}
        if isinstance(reading.get("temp"), (int, float)):
            row["temp"] = _round(reading["temp"])
        if isinstance(reading.get("rpm"), (int, float)):
            row["rpm"] = _round(reading["rpm"], 0)
        if bottle.get("duration_s") is not None:
            row["duration_s"] = bottle["duration_s"]
        if bottle.get("failed"):
            row["error"] = bottle["failed"]
        return row

    # ---------- 终止、找回、退出 ----------

    def abort(self, job: Job) -> None:
        run = self.runs.get(job.handle)
        if run is None or run.over.is_set():
            return  # 已经结束（或网关重启前就结束了）：板子都停了
        with run.lock:
            run.reason = run.reason or "被终止"
        run.wake.set()
        if not run.over.wait(ABORT_WAIT_SEC):
            notes = "；".join(bottle.stop_note for bottle in run.bottles.values() if bottle.stop_note)
            raise RuntimeError(f"发了停止，{ABORT_WAIT_SEC:g} 秒内还没确认每块板都停下（{notes or '还在停'}）："
                               "网关在重试，请到现场核查")

    def lookup(self, job: Job) -> str | None:
        """启动没拿到应答：只认启动完整做完了的作业。只做了一部分的（已回退）不认，留给人核查。"""
        run = self.runs.get(job.command_id)
        data = run.public() if run is not None else self._load(job.command_id)
        return job.command_id if data is not None and data.get("started_ok") else None

    def close(self, *, stop: bool = True, timeout: float = 5.0) -> None:
        """网关退出。`stop` 时先把在跑的板都停下（等不到确认的，下次启动时接着停）；`stop=False` 只是让计时线程
        退出、不动设备（测试里模拟进程崩掉）。"""
        active = [run for run in list(self.runs.values()) if not run.over.is_set() and not run.halt]
        for run in active:
            if stop:
                with run.lock:
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
        for plate in self.plates.values():
            plate.link.close()

    def fault_target(self):
        return self.faults

    # ---------- 作业记录 ----------

    def _path(self, handle: str) -> Path | None:
        # 文件名取作业号的摘要：作业号里可能有文件名不收的字符
        return self.store / f"{hashlib.sha256(handle.encode('utf-8')).hexdigest()[:24]}.json" if self.store else None

    def _save(self, run: Run, *, strict: bool = False) -> None:
        """写作业记录（临时文件 + 原子替换）。板子动了之后写不进去只记日志、不抛：不能因为记录让计时停下。"""
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
        """网关重启：还没结束的作业不续时，把它的板停下、判失败。旧的结论只留最近 KEEP 条。"""
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
        reason = "网关重启时作业还没结束" if data.get("started_ok") else "网关在启动途中重启"
        bottles = {}
        for well, item in (data.get("bottles") or {}).items():
            key = str(item.get("position"))
            if key not in self.plates:
                log.error("作业 %s 的位置 %s 已经不在配置里：请到现场核查这块板停了没有", data.get("handle"), key)
                continue
            bottle = Bottle(well=well, position=key, temp=float(item["temp"]), time=float(item["time"]),
                            rpm=float(item["rpm"]), heat=bool(item["heat"]), moved=True,
                            wall_started=float(item.get("wall_started") or 0), reading=dict(item.get("reading") or {}))
            finished = bool(item.get("finished"))
            bottle.finished = finished
            bottle.duration = item.get("duration_s")
            if not finished:
                elapsed = f"，开始了约 {time.time() - bottle.wall_started:.0f} s" if bottle.wall_started else ""
                bottle.stopping = True
                bottle.failed = (f"{_where(well)}{self.config.positions[key].label()}：{reason}（计划 {bottle.time:g} s{elapsed}）："
                                 "已停下加热搅拌，这一瓶没做完；网关不在的这段时间板子转没转、温度怎样都不知道")
            bottles[well] = bottle
        run = Run(handle=str(data["handle"]), bottles=bottles, state="running", started_ok=bool(data.get("started_ok")),
                  reason=reason)
        with self.lock:
            for bottle in bottles.values():
                if not bottle.finished:
                    self.owner[bottle.position] = run.handle
            self.runs[run.handle] = run
        log.warning("作业 %s %s：先把它的板停下", run.handle, reason)
        run.thread = threading.Thread(target=self._supervise, args=(run,), daemon=True, name=f"stir-{run.handle}")
        run.thread.start()
