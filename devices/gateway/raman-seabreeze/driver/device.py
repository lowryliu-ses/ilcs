"""真实接口：把光谱仪包成 `ilcs_gateway.Device`，做拉曼采谱。去重、台账、查询、令牌、TLS 都由 SDK 负责。

一条 ILCS 指令 = 在测量位上采 `repeats` 张谱、逐像素取平均，回报一条拉曼谱（拉曼位移 cm-1 → 计数）：

- 积分时间：指令带的 `integration_ms` 优先，其次是设备方法的程序（`programs`）里写的，再次是配置的 `integration_ms`；
- **一次一瓶**：光谱仪只有一个测量位（一个探头 / 样品池）。指令不带 `wells`，或 `wells` 里只有一瓶（孔位里的参数
  覆盖顶层的）；多瓶要按瓶分开下发；
- 参数只认 `repeats`、`integration_ms`（孔位里也只认这两个），**带别的参数一律拒绝**，不悄悄忽略。

判断规则：

- 能力不对：`Rejected("unsupported")`；程序没登记、参数非法、积分时间超出配置或光谱仪的范围、位移范围里没有像素：
  `Rejected("invalid")`；都没动；
- 上一次采谱还没结束（单测量位）、读不到光谱仪（没插、被 OceanView 占着）：`Rejected("busy")`，没动；
- 采谱在后台线程里做，`start` 立刻返回；采谱时光谱仪报错判失败（采谱不消耗样品，可以重测）；
- **饱和**（任一像素到满量程的 98%）照样完成，回报 `saturated: true` 与说明：峰顶可能被削平。错误栏只给失败用，
  ILCS 侧按输出项 `max_counts` 的上限打标、交审核；
- 终止：不再采下一张，这次的谱不回报；正在读出的那一张停不下来（最长一个积分时间），读完测量位才空闲；
- 结论写进状态目录，网关重启后照样按作业号答得上来；重启前还没采完的作业判失败（进程没了采谱也就停了，没有谱图）。
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

from .config import Config
from .spectro_api import Spectrometer, SpectrometerError
from .spectrum import SpectrumError, average, coverage, process

# 指令里网关认的参数：采几张谱取平均 repeats、积分时间 integration_ms（毫秒）与逐孔参数 wells
PARAMETERS = ("repeats", "integration_ms", "wells")
WELL_PARAMETERS = ("repeats", "integration_ms")
# 任一像素到满量程的这个比例就算饱和
SATURATION = 0.98
# 终止时等采谱线程停下来最多多久（秒）：正在读出的那张停不下来，等不到也确认终止（读完才空闲）
ABORT_WAIT_SEC = 3.0
# 内存与状态目录里各留多少次采谱的结论（与 SDK 台账留的条数一致）
KEEP = 500
# 不带 wells 的指令在作业里的孔位键
SINGLE = ""
log = logging.getLogger("ilcs.gateway.raman")


class Cancelled(Exception):
    """被终止。"""


@dataclass
class Run:
    handle: str
    program: str
    well: str
    repeats: int
    integration_ms: float
    cancel: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    scans: int = 0
    max_counts: float | None = None
    # 结论 {state, delivered, error, max_counts}：落盘、放开测量位之后一次写上；还在采时是 None
    outcome: dict[str, Any] | None = None


def _where(well: str) -> str:
    return f"孔位 {well} " if well else ""


class Instrument(Device):
    def __init__(self, spectrometer: Spectrometer, config: Config, *, state_dir: str | Path | None = None):
        self.spectrometer = spectrometer
        self.config = config
        # 只有模拟接口（假光谱仪）带故障状态：统一控制口的故障注入；真光谱仪没有，控制口就不开
        self.faults = getattr(spectrometer, "faults", None)
        self.runs: dict[str, Run] = {}
        self.current: Run | None = None
        self.busy = threading.Lock()
        self.store = Path(state_dir) / "runs" if state_dir else None
        if self.store:
            self.store.mkdir(parents=True, exist_ok=True)

    # ---------- 身份 ----------

    def identity(self) -> dict[str, Any]:
        info = self.spectrometer.identity()
        interlock = bool(info.get("interlock"))
        return {
            "device_id": self.config.device_id, "serial": str(info.get("serial") or self.config.device_id),
            "model": self.config.model or str(info.get("model") or ""), "vendor": self.config.vendor,
            "firmware": str(info.get("firmware") or ""),
            "methods": [{"program": code, "name": program.name, "capability": self.config.capability}
                        for code, program in self.config.programs.items()],
            # 光谱仪自报的型号、像素数与满量程（ILCS 设备方法里 max_counts 的上限按满量程的 98% 设）
            "spectrometer_model": str(info.get("model") or ""), "pixels": info.get("pixels"),
            "max_intensity": info.get("max_intensity"),
            "laser": {"kind": "external", "wavelength_nm": self.config.laser_nm},
            "interlock": interlock, "accepts_commands": not interlock, "simulator": bool(info.get("simulator")),
            # 采谱停不在半截：不做保持，也就没有续跑
            "commands": ["dispatch", "retry", "abort", "query"],
        }

    # ---------- 启动 ----------

    def start(self, job: Job) -> str:
        if job.capability != self.config.capability:
            raise Rejected("unsupported", f"这台设备只做 {self.config.capability}，不做 {job.capability}")
        code = job.program or self.config.default_program
        program = self.config.programs.get(code)
        if program is None:
            raise Rejected("invalid", f"没有登记采谱程序 {code or '（未指定）'}；可选 {', '.join(self.config.programs)}")
        well, values = self._request(job.params)
        repeats = self._repeats(values.get("repeats"), well)
        integration_ms = (self._integration(values.get("integration_ms"), well) or program.integration_ms
                          or self.config.integration_ms)
        if self.faults:
            self.faults.check_start()  # 模拟：联锁、忙
        if not self.busy.acquire(blocking=False):
            raise Rejected("busy", self._busy_reason())
        started = False
        try:
            self._preflight(integration_ms)
            run = Run(handle=job.command_id, program=code, well=well, repeats=repeats, integration_ms=integration_ms)
            mode = self.faults.moved() if self.faults else "none"
            run.thread = threading.Thread(target=self._work, args=(run, mode), daemon=True,
                                          name=f"raman-{job.command_id}")
            self._remember(run)
            try:
                run.thread.start()
            except RuntimeError as exc:
                self.runs.pop(run.handle, None)
                raise Rejected("busy", f"网关起不了采谱线程，设备未接受作业：{exc}") from exc
            started = True
        finally:
            if not started:
                self.busy.release()
        if mode == "slow_submit":
            time.sleep(self.faults.parameter)
        if mode == "lost_receipt":
            raise ReceiptLost(run.handle)
        return run.handle

    def _request(self, params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """(孔位, 这一瓶的参数)。孔位里的参数覆盖顶层的（ILCS 里步骤固定参数是逐孔参数的缺省值）。"""
        unknown = sorted(set(params) - set(PARAMETERS))
        if unknown:
            raise Rejected("invalid", f"网关不接受参数 {', '.join(unknown)}；只认 repeats、integration_ms 与 wells")
        defaults = {key: value for key, value in params.items() if key != "wells"}
        raw = params.get("wells")
        if raw is None:
            return SINGLE, defaults
        if not isinstance(raw, dict) or not raw:
            raise Rejected("invalid", "wells 要写成 {孔位: {参数: 值}}，至少一瓶")
        if len(raw) > 1:
            raise Rejected("unsupported", f"一次只能测一瓶（光谱仪只有一个测量位），这条指令有 {len(raw)} 瓶"
                                          f"（{', '.join(map(str, raw))}）：按瓶分开下发")
        well, values = next(iter(raw.items()))
        if not str(well).strip() or not isinstance(values, dict):
            raise Rejected("invalid", f"孔位 {well!r} 的参数要写成对象，如 {{\"repeats\": 3}}")
        extra = sorted(set(values) - set(WELL_PARAMETERS))
        if extra:
            raise Rejected("invalid", f"孔位 {well} 带了网关不接受的参数 {', '.join(extra)}；每瓶只认 repeats 与 integration_ms")
        return str(well), {**defaults, **values}

    def _repeats(self, value: Any, well: str) -> int:
        if value is None:
            return 1
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not float(value).is_integer():
            raise Rejected("invalid", f"{_where(well)}repeats = {value!r} 不是整数（采几张谱取平均）")
        if not 1 <= int(value) <= self.config.max_repeats:
            raise Rejected("invalid", f"{_where(well)}repeats = {int(value)} 超出 1–{self.config.max_repeats} 次")
        return int(value)

    def _integration(self, value: Any, well: str) -> float | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise Rejected("invalid", f"{_where(well)}integration_ms = {value!r} 不是正数（积分时间，毫秒）")
        if value > self.config.max_integration_ms:
            raise Rejected("invalid", f"{_where(well)}integration_ms = {value:g} 超过网关允许的 "
                                      f"{self.config.max_integration_ms:g} ms")
        return float(value)

    def _preflight(self, integration_ms: float) -> None:
        """采谱之前读一次光谱仪：读不到就是没动（busy，不是结果未知）；积分时间超出设备范围、位移范围里没有像素是 invalid。"""
        try:
            info = self.spectrometer.identity()
            low, high = self.spectrometer.integration_limits_us()
            wavelengths = self.spectrometer.wavelengths()
        except Exception as exc:  # noqa: BLE001  还没开始采谱
            raise Rejected("busy", f"读不到光谱仪，设备未接受作业：{exc}") from exc
        if info.get("interlock"):
            raise Rejected("interlocked", "安全联锁触发，设备未动作")
        us = round(integration_ms * 1000)
        if not low <= us <= high:
            raise Rejected("invalid", f"积分时间 {integration_ms:g} ms 超出这台光谱仪的范围 {low / 1000:g}–{high / 1000:g} ms")
        low_cm, high_cm = self.config.shift_range
        try:
            covered = coverage(wavelengths, self.config.laser_nm, low_cm, high_cm)
        except SpectrumError as exc:
            raise Rejected("invalid", f"{exc}；设备未动作") from exc
        if covered < 2:
            raise Rejected("invalid", f"光谱仪的波长范围换算成拉曼位移后，{low_cm:g}–{high_cm:g} cm-1 里只有 {covered} 个像素："
                                      f"核对激光波长（{self.config.laser_nm:g} nm）与 shift_range_cm1")

    def _busy_reason(self) -> str:
        run = self.current
        if run is not None and run.cancel.is_set():
            return "上一次采谱已终止、光谱仪还在读出最后一张（最长一个积分时间），设备未接受作业"
        return "上一次采谱还没结束（只有一个测量位，一次测一瓶），设备未接受作业"

    def _remember(self, run: Run) -> None:
        finished = [handle for handle, item in self.runs.items() if item.outcome is not None]
        for handle in finished[: max(0, len(self.runs) + 1 - KEEP)]:
            del self.runs[handle]  # 结论在状态目录里还有
        self.runs[run.handle] = run
        self.current = run

    # ---------- 后台采谱 ----------

    def _work(self, run: Run, mode: str) -> None:
        outcome: dict[str, Any] = {"state": "failed", "delivered": {}, "error": "", "max_counts": None}
        try:
            outcome = self._acquire(run, mode)
        except Cancelled:
            outcome["error"] = "被终止：这次的谱图不回报"
        except SpectrometerError as exc:
            outcome["error"] = f"光谱仪报错，采谱中止（已采 {run.scans} / {run.repeats} 张）：{exc}"
        except SpectrumError as exc:
            outcome["error"] = f"谱图不成立：{exc}"
        except Exception as exc:  # noqa: BLE001  后台线程不能把异常吞掉不报：判失败并写明
            outcome["error"] = f"采谱出错：{exc}"
        finally:
            if outcome.get("max_counts") is None and run.max_counts is not None:
                outcome["max_counts"] = round(run.max_counts, 1)
            try:
                self._save(run.handle, outcome)
            except Exception as exc:  # noqa: BLE001  结论还在内存里，只是网关重启后答不上来；测量位照样要放开
                log.warning("采谱结论写不进状态目录 %s：%s", self.store, exc)
            # 先放开测量位、再写结论：网关看到「完成」时下一条指令一定进得来
            self.busy.release()
            run.outcome = outcome

    def _acquire(self, run: Run, mode: str) -> dict[str, Any]:
        spectrometer, config = self.spectrometer, self.config
        spectrometer.set_integration_us(round(run.integration_ms * 1000))
        wavelengths = spectrometer.wavelengths()
        ceiling = spectrometer.max_intensity()
        scans: list[list[float]] = []
        for _ in range(run.repeats):
            if run.cancel.is_set():
                raise Cancelled()
            counts = spectrometer.intensities(config.correct_dark_counts, config.correct_nonlinearity)
            if len(counts) != len(wavelengths):
                raise SpectrumError(f"一张谱有 {len(counts)} 个像素，波长表有 {len(wavelengths)} 个，对不上")
            scans.append(counts)
            # 饱和看每一张的原始读数（平均会把削顶的峰抹平），整个探测器上任一像素都算
            peak = max(counts)
            run.max_counts = peak if run.max_counts is None else max(run.max_counts, peak)
            run.scans += 1
        if run.cancel.is_set():
            raise Cancelled()  # 最后一张读出时被终止：不回报
        if mode == "stuck":
            run.cancel.wait()  # 模拟：一直不结束，直到被终止
            raise Cancelled()
        if mode == "fail":
            raise SpectrometerError("模拟故障：光谱仪 USB 读出出错")
        spectrum = process(wavelengths, average(scans), laser_nm=config.laser_nm, shift_range=config.shift_range,
                           max_points=config.max_points)
        max_counts = round(run.max_counts, 1)
        saturated = run.max_counts >= SATURATION * ceiling
        row: dict[str, Any] = {
            "spectrum": spectrum, "integration_ms": run.integration_ms, "repeats": run.repeats,
            "laser_nm": config.laser_nm, "max_counts": max_counts, "saturated": saturated, "program": run.program,
        }
        if saturated:
            row["note"] = (f"饱和：最高计数 {max_counts:g} 到了满量程 {ceiling:g} 的 {SATURATION:.0%}，峰顶可能被削平；"
                           "缩短积分时间（integration_ms 或换程序）重测")
        delivered = row if run.well == SINGLE else {"wells": {run.well: row}}
        return {"state": "done", "delivered": delivered, "error": "", "max_counts": max_counts}

    # ---------- 状态 ----------

    def status(self, job: Job) -> Status:
        run = self.runs.get(job.handle)
        outcome = run.outcome if run is not None else self._load(job.handle)
        if run is not None and outcome is None:
            return Status("running", telemetry=self._telemetry(run.max_counts))
        if outcome is None:
            # 网关重启前这次采谱还没出结论：进程没了采谱也就停了，没有谱图。采谱不消耗样品，判失败、可以重测
            return Status("failed", error="网关重启前这次采谱还没完成，没有谱图：没测成，可以重测")
        return Status(outcome["state"], actuals=dict(outcome.get("delivered") or {}),
                      telemetry=self._telemetry(outcome.get("max_counts")), error=str(outcome.get("error") or ""))

    @staticmethod
    def _telemetry(max_counts: float | None) -> list[dict[str, Any]]:
        if max_counts is None:
            return []
        return [{"metric": "max_counts", "value": float(max_counts), "setpoint": None}]

    def _path(self, handle: str) -> Path | None:
        # 文件名取作业号（ILCS 指令号）的摘要：不同平台对文件名的字符限制不一样
        return self.store / f"{hashlib.sha256(handle.encode('utf-8')).hexdigest()[:24]}.json" if self.store else None

    def _save(self, handle: str, outcome: dict[str, Any]) -> None:
        path = self._path(handle)
        if path is None:
            return
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"handle": handle, **outcome}, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)
        files = sorted(self.store.glob("*.json"), key=lambda item: item.stat().st_mtime)
        for stale in files[: max(0, len(files) - KEEP)]:
            stale.unlink(missing_ok=True)  # 早就进了台账的老结论

    def _load(self, handle: str) -> dict[str, Any] | None:
        path = self._path(handle)
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path is not None and path.exists() else None
        except (OSError, ValueError):
            return None
        valid = isinstance(data, dict) and data.get("handle") == handle and data.get("state") in {"done", "failed"}
        return data if valid else None

    # ---------- 终止、找回 ----------

    def abort(self, job: Job) -> None:
        run = self.runs.get(job.handle)
        if run is None or run.outcome is not None:
            return  # 已经结束（或网关重启前就结束了）：没有在采谱
        run.cancel.set()
        if run.thread is not None:
            run.thread.join(timeout=ABORT_WAIT_SEC)
        # 没等到也确认终止：不再采下一张、这次的谱不回报。光谱仪本身是被动的（激光由外部控制），
        # 正在读出的那一张读完测量位才空闲，这期间新指令报忙

    def lookup(self, job: Job) -> str | None:
        """启动没拿到应答：作业号就是指令号，本进程里有、或状态目录里有它的结论就认。"""
        if job.command_id in self.runs or self._load(job.command_id) is not None:
            return job.command_id
        return None

    def fault_target(self):
        return self.faults

    def close(self) -> None:
        """网关退出：停掉在采的、放开 USB。"""
        run = self.current
        if run is not None and run.outcome is None:
            run.cancel.set()
            if run.thread is not None:
                run.thread.join(timeout=ABORT_WAIT_SEC)
        self.spectrometer.close()
