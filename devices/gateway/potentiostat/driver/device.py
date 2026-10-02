"""真实接口：把一台电化学工作站（一个通道、接一个电池）包成 `ilcs_gateway.Device`。去重、台账、查询、令牌、TLS 由 SDK 负责。

一条 ILCS 指令 = 在接着的电池上做一次测量（设备方法的「程序」选技术与参数，见 driver/techniques.py），回报曲线与派生指标：

- **一次一个电池**：一个通道只接一个电池。指令不带 `wells`，或 `wells` 里只有一瓶（孔位里的参数覆盖顶层的）；
  多瓶回 NotSupported「按电池分开下发」。上一次测量没结束时新指令报忙；
- 参数只认网关配置 `params` 里登记的（且这个程序的技术用得上），**带别的参数一律拒绝**，不悄悄忽略；
- 程序没登记、参数非法、超出网关或仪器的极限、配置的型号和仪器自报的对不上：`Rejected("invalid")`；仪器拒收脚本
  按错误码判 invalid / unsupported / busy；读不到仪器：`Rejected("busy")`——都没动；
- 开始命令（MethodSCRIPT 的 `r`）写出去了却没拿到确认：结果未知（抛普通异常，SDK 记成待查、绝不重发）；
- 测量在后台线程里读，`start` 立刻返回；`status` 不碰仪器，只看后台线程收到了什么；
- **终止**：发仪器的终止命令（`Z`），等仪器确认结束；这次的数据不回报，判失败「被终止」。终止到达之前已经测完的，
  如实拒绝终止（「来不及终止」），原作业照报完成；`abort_timeout_sec` 内仪器没结束回结果未知；
- 测量途中链路断了：断线期间的数据收不回来，网关一直重连，连上后先停掉仪器上的测量（电池断开）再判失败；
  重连之前这次作业一直报在测（并写明在重连），不让 ILCS 以为电池已经断开；
- 结论写进状态目录（`<state-dir>/runs/`），网关重启后照样按作业号答得上；重启前还没测完的，连上仪器、停掉仪器上
  可能还在跑的测量之后判失败（数据收不回来，可以重测）；连上之前不下结论。
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import logging
from pathlib import Path
import threading
import time
from typing import Any

from ilcs_gateway import Device, Job, ReceiptLost, Rejected, Status

from . import methodscript as ms
from .backend import BackendError, BackendRejected, Plan, Point, Potentiostat
from .config import Config
from .palmsens import expected_points
from .techniques import TECHNIQUE_NAMES, Cell, PlanError, Program, ResultError, instrument_problems, make_plan, \
    summarize, telemetry

# 内存与状态目录里各留多少次测量的结论（与 SDK 台账留的条数一致）
KEEP = 500
# 不带 wells 的指令在作业里的孔位键
SINGLE = ""
RECOVER_FIRST_DELAY = 0.2
RECOVER_MAX_DELAY = 10.0
log = logging.getLogger("ilcs.gateway.echem")


@dataclass
class Run:
    handle: str
    program: Program
    plan: Plan
    cell: Cell
    well: str
    expected: int | None = None
    cancel: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    points: list[Point] = field(default_factory=list)
    script_active: bool = False   # 仪器上的测量在跑（从开始命令到读完输出）
    note: str = ""                # 进行中的说明（链路中断、正在重连）
    # 结论 {state, delivered, error}：落盘、放开通道之后一次写上；还在测时是 None
    outcome: dict[str, Any] | None = None

    def add(self, point: Point) -> None:
        self.points.append(point)


def _where(well: str) -> str:
    return f"孔位 {well} " if well else ""


class Instrument(Device):
    def __init__(self, backend: Potentiostat, config: Config, *, state_dir: str | Path | None = None, faults=None):
        self.backend = backend
        self.config = config
        # 只有模拟模式才有：统一控制口的故障注入（假仪器那一侧是真的 MethodSCRIPT 协议）
        self.faults = faults
        self.runs: dict[str, Run] = {}
        self.current: Run | None = None
        self.busy = threading.Lock()
        self.closing = threading.Event()
        self.store = Path(state_dir) / "runs" if state_dir else None
        if self.store:
            self.store.mkdir(parents=True, exist_ok=True)

    # ---------- 身份 ----------

    def identity(self) -> dict[str, Any]:
        info = self.backend.identity()
        interlock = bool(self.faults.interlock) if self.faults else False
        mismatch = self._mismatch(info)
        identity = {
            "device_id": self.config.device_id, "serial": str(info.get("serial") or self.config.device_id),
            "model": self.config.model or str(info.get("model") or ""), "vendor": self.config.vendor,
            "firmware": str(info.get("firmware") or ""),
            "methods": [{"program": code, "name": program.name, "capability": self.config.capability,
                         "technique": program.technique} for code, program in self.config.programs.items()],
            # 仪器自报的型号、设备类型、MethodSCRIPT 版本（核对配置写的型号；不同型号的电位范围不一样）
            "instrument_model": str(info.get("model") or ""), "device_type": str(info.get("device_type") or ""),
            "script_version": str(info.get("script_version") or ""),
            "interlock": interlock, "accepts_commands": not interlock and not mismatch,
            "simulator": bool(info.get("simulator")) or self.faults is not None,
            # 测量停不在半截：不做保持，也就没有续跑
            "commands": ["dispatch", "retry", "abort", "query"],
        }
        if mismatch:
            identity["warning"] = mismatch
        return identity

    def _mismatch(self, info: dict[str, Any]) -> str:
        device_type = str(info.get("device_type") or "")
        if ms.model_matches(self.config.model, device_type):
            return ""
        return (f"配置写的型号是 {self.config.model}，仪器自报 {info.get('model') or device_type}：核对网关配置"
                "（不同型号的电位、电流范围不一样）")

    # ---------- 启动 ----------

    def start(self, job: Job) -> str:
        if job.capability != self.config.capability:
            raise Rejected("unsupported", f"这台设备只做 {self.config.capability}，不做 {job.capability}")
        code = job.program or self.config.default_program
        program = self.config.programs.get(code)
        if program is None:
            raise Rejected("invalid", f"没有登记测量程序 {code or '（未指定）'}；可选 {', '.join(self.config.programs)}")
        well, values = self._request(job.params)
        try:
            plan, cell = make_plan(program, values, allowed=self.config.params, e_limits=self.config.e_limits)
        except PlanError as exc:
            raise Rejected("invalid", f"{_where(well)}{exc}") from exc
        if self.faults:
            self.faults.check_start()  # 模拟：联锁、忙
        if not self.busy.acquire(blocking=False):
            raise Rejected("busy", "上一次测量还没结束（一个通道接一个电池，一次测一个），仪器未接受作业")
        started = False
        run = Run(handle=job.command_id, program=program, plan=plan, cell=cell, well=well,
                  expected=expected_points(plan))
        try:
            self._preflight(plan)
            try:
                self.backend.prepare(plan)
            except BackendRejected as exc:
                raise Rejected(exc.kind, f"{exc.message}；仪器未动作") from exc
            except BackendError as exc:
                raise Rejected("busy", f"读不到仪器，仪器未动作：{exc}") from exc
            try:
                self.backend.begin()
            except BackendRejected as exc:
                raise Rejected(exc.kind, f"{exc.message}；仪器未动作") from exc
            except BackendError as exc:
                raise Rejected("busy", f"开始命令没发出去，仪器未动作：{exc}") from exc
            # 到这里仪器已经在测（StartUnknown 照原样抛出去：结果未知）
            run.script_active = True
            mode = self.faults.moved() if self.faults else "none"
            run.thread = threading.Thread(target=self._work, args=(run, mode), daemon=True,
                                          name=f"echem-{job.command_id}")
            self._remember(run)
            try:
                run.thread.start()
            except RuntimeError as exc:
                self.runs.pop(run.handle, None)
                self.backend.abort()
                raise RuntimeError(f"仪器已经开始测量，网关却起不了读数线程（已发终止）：{exc}") from exc
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
        """(孔位, 这个电池的参数)。孔位里的参数覆盖顶层的（ILCS 里步骤固定参数是逐孔参数的缺省值）。"""
        accepted = sorted(self.config.params)
        hint = f"只认 {'、'.join(accepted)} 与 wells" if accepted else "这台网关不接受工艺参数（测量参数都在程序里），只认 wells"
        unknown = sorted(set(params) - set(accepted) - {"wells"})
        if unknown:
            raise Rejected("invalid", f"网关不接受参数 {', '.join(unknown)}；{hint}")
        defaults = {key: value for key, value in params.items() if key != "wells"}
        raw = params.get("wells")
        if raw is None:
            return SINGLE, defaults
        if not isinstance(raw, dict) or not raw:
            raise Rejected("invalid", "wells 要写成 {孔位: {参数: 值}}，至少一个电池")
        if len(raw) > 1:
            raise Rejected("unsupported", f"一次只能测一个电池（一个通道只接一个电池），这条指令有 {len(raw)} 个"
                                          f"（{', '.join(map(str, raw))}）：按电池分开下发")
        well, values = next(iter(raw.items()))
        if not str(well).strip() or not isinstance(values, dict):
            raise Rejected("invalid", f"孔位 {well!r} 的参数要写成对象（可以是空对象 {{}}）")
        extra = sorted(set(values) - set(accepted))
        if extra:
            raise Rejected("invalid", f"孔位 {well} 带了网关不接受的参数 {', '.join(extra)}；{hint}")
        return str(well), {**defaults, **values}

    def _preflight(self, plan: Plan) -> None:
        """开始之前读一次仪器：读不到就是没动（busy，不是结果未知）；型号对不上、超出仪器极限是 invalid。"""
        try:
            info = self.backend.identity()
        except Exception as exc:  # noqa: BLE001  还没开始：读不到仪器就是没动
            raise Rejected("busy", f"读不到仪器，仪器未接受作业：{exc}") from exc
        mismatch = self._mismatch(info)
        if mismatch:
            raise Rejected("invalid", f"{mismatch}；仪器未动作")
        problems = instrument_problems(plan, self.backend.limits(plan.technique))
        if problems:
            raise Rejected("invalid", f"{'；'.join(problems)}；仪器未动作")

    def _remember(self, run: Run) -> None:
        finished = [handle for handle, item in self.runs.items() if item.outcome is not None]
        for handle in finished[: max(0, len(self.runs) + 1 - KEEP)]:
            del self.runs[handle]  # 结论在状态目录里还有
        self.runs[run.handle] = run
        self.current = run

    # ---------- 后台读数 ----------

    def _work(self, run: Run, mode: str) -> None:
        outcome: dict[str, Any] = {"state": "failed", "delivered": {}, "error": ""}
        try:
            outcome = self._measure(run, mode)
        except Exception as exc:  # noqa: BLE001  后台线程不能把异常吞掉不报：判失败并写明
            log.exception("测量线程出错")
            outcome["error"] = f"测量出错：{exc}"
        finally:
            run.script_active = False
            try:
                self._save(run.handle, outcome)
            except Exception as exc:  # noqa: BLE001  结论还在内存里，只是网关重启后答不上来；通道照样要放开
                log.warning("测量结论写不进状态目录 %s：%s", self.store, exc)
            # 先放开通道、再写结论：网关看到「完成」时下一条指令一定进得来
            self.busy.release()
            run.outcome = outcome

    def _measure(self, run: Run, mode: str) -> dict[str, Any]:
        failed = {"state": "failed", "delivered": {}, "error": ""}
        try:
            finish = self.backend.stream(run.add)
        except BackendError as exc:
            run.script_active = False
            run.note = f"测量途中和仪器的链路断了（{exc}），正在重连、停掉仪器上的测量"
            log.warning("%s：%s", run.handle, run.note)
            if not self._recover(run):
                return {**failed, "error": f"测量途中链路断了（{exc}），网关退出前没能重连：仪器上的测量可能还在跑，"
                                          "请到现场核查电池是否断开"}
            return {**failed, "error": f"测量途中链路断了（{exc}，已收到 {len(run.points)} 个点）：重连后已停掉"
                                      "仪器上的测量，这次的数据收不全、不回报，可以重测"}
        run.script_active = False
        if finish.state == "aborted":
            return {**failed, "error": "被终止：这次测量的数据不回报"}
        if finish.state == "error":
            safe = "" if finish.safe else "；补发的 cell_off 没有确认，请到现场核查电池是否断开"
            return {**failed, "error": f"仪器报错，测量中止（已收到 {len(run.points)} 个点）：{finish.error}{safe}"}
        if mode == "stuck":
            run.cancel.wait()  # 模拟：一直不结束，直到被终止
            return {**failed, "error": "被终止：这次测量的数据不回报"}
        if mode == "fail":
            return {**failed, "error": "模拟故障：仪器报电池严重过载（!0032），测量中止"}
        try:
            row = summarize(run.program, run.plan, run.cell, run.points, max_points=self.config.max_points)
        except ResultError as exc:
            return {**failed, "error": f"测完了，但{exc}"}
        if finish.notes:
            log.info("%s 仪器输出：%s", run.handle, "；".join(finish.notes))
        delivered = row if run.well == SINGLE else {"wells": {run.well: row}}
        return {"state": "done", "delivered": delivered, "error": ""}

    def _recover(self, run: Run) -> bool:
        delay = RECOVER_FIRST_DELAY
        while not self.closing.is_set():
            try:
                self.backend.recover()
                return True
            except Exception as exc:  # noqa: BLE001  连不上：隔一会儿再试，一直试到连上（或网关退出）
                run.note = f"测量途中链路断了，重连失败（{exc}），{delay:g} 秒后再试：仪器上的测量可能还在跑"
                self.closing.wait(delay)
                delay = min(delay * 2, RECOVER_MAX_DELAY)
        return False

    # ---------- 状态 ----------

    def status(self, job: Job) -> Status:
        run = self.runs.get(job.handle)
        outcome = run.outcome if run is not None else self._load(job.handle)
        if run is not None and outcome is None:
            last = run.points[-1] if run.points else None
            return Status("running", telemetry=telemetry(run.plan, last, len(run.points), run.expected),
                          error=run.note)
        if outcome is None:
            if not getattr(self.backend, "ready", False):
                # 网关重启后还没连上仪器：仪器上那次测量可能还在跑，不下结论（SDK 照报台账里的状态）
                raise RuntimeError(f"作业 {job.handle} 在网关重启前没有出结论，网关还没连上仪器确认它停了")
            return Status("failed", error="网关重启前这次测量还没完成：重启后连上仪器时已停掉仪器上的测量，"
                                          "数据收不回来，可以重测")
        return Status(outcome["state"], actuals=dict(outcome.get("delivered") or {}),
                      error=str(outcome.get("error") or ""))

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
        if run is None:
            return  # 网关重启前就结束了（或从没开始）：仪器上没有这次测量
        if run.outcome is not None:
            if run.outcome["state"] == "done":
                raise Rejected("invalid", f"来不及终止：这次{TECHNIQUE_NAMES[run.plan.technique]}已经测完（数据照常回报）")
            return  # 已经失败结束：仪器不在测
        run.cancel.set()
        if run.script_active:
            self.backend.abort()  # 写不出去抛 BackendError：不知道停没停，SDK 报结果未知
        if run.thread is not None:
            run.thread.join(timeout=self.config.abort_timeout_sec)
        if run.outcome is None:
            raise RuntimeError(f"发了终止，{self.config.abort_timeout_sec:g} 秒内仪器还没结束测量：请到现场核查")
        if run.outcome["state"] == "done":
            # 终止命令到之前已经测完：如实拒绝终止，原作业照报完成
            raise Rejected("invalid", f"来不及终止：这次{TECHNIQUE_NAMES[run.plan.technique]}已经测完（数据照常回报）")

    def lookup(self, job: Job) -> str | None:
        """启动没拿到应答：作业号就是指令号，本进程里有、或状态目录里有它的结论就认。"""
        if job.command_id in self.runs or self._load(job.command_id) is not None:
            return job.command_id
        return None

    def fault_target(self):
        return self.faults

    def close(self) -> None:
        """网关退出：停掉在测的（仪器断开电池）、放开串口。"""
        self.closing.set()
        run = self.current
        if run is not None and run.outcome is None:
            run.cancel.set()
            try:
                if run.script_active:
                    self.backend.abort()
            except Exception:  # noqa: BLE001  写不出去：仪器上的测量可能还在跑，下次连上时同步会停掉它
                log.warning("退出时终止命令没写出去：下次连上仪器时会先停掉仪器上的测量")
            if run.thread is not None:
                run.thread.join(timeout=self.config.abort_timeout_sec)
        self.backend.close()
