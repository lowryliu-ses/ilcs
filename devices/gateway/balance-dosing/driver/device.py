"""真实接口：一台天平（梅特勒 MT-SICS）加可选的加粉（Quantos）、加液（Cavro 协议注射泵 + 分配阀）装置，包成
`ilcs_gateway.Device`。去重、台账、查询、令牌、TLS 由 SDK 负责。

三个动作（ILCS 能力 id 在配置里对应）：

- `weigh` 称量：读秤上容器的稳定净重；
- `dose_solid` 固体称量加料：Quantos 按目标质量加粉，加样头上的物质必须是指令要的料；
- `dose_liquid` 液体称量加注：去皮 → 按密度换算体积、先加九成 → 天平读数 → 补到目标，天平定量。

规则：

- **加的料要对**：指令的 `material`（ILCS 投料步骤带的物料名）对不上加样头 / 阀端口登记的料就拒绝，设备不动；
  没带物料的指令（ILCS 接入验收）用 `acceptance_material` 里指定的料核对。
- **一次一瓶**：天平上只有一个位置。指令的 `wells` 只能有一瓶；多瓶要由搬运按瓶分开下发。
- **报实际量**：加完一律回报天平称出来的量（`mass`）与 `delivered.materials`；偏离目标不判失败——ILCS 按实际量入账、
  超过偏差阈值报警待复核。设备本身出错（加样头用完、管路不出液、泵报错）才判失败，错误里写明已经加进去多少。
- 加料在后台线程里做，`start` 立刻返回；结论写进状态目录，网关重启后照样按作业号答得上来。
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import itertools
import json
from pathlib import Path
import threading
import time
from typing import Any

from ilcs_gateway import Device, Job, ReceiptLost, Rejected, Status

from .cavro import PumpError
from .config import Config
from .errors import DeviceBusy, DeviceError
from .link import LinkError

DEFAULT_PROGRAMS = {
    "WEIGH": {"action": "weigh", "name": "称量（读稳定净重）"},
    "DOSE-POWDER": {"action": "dose_solid", "name": "固体称量加料（Quantos）"},
    "DOSE-LIQUID": {"action": "dose_liquid", "name": "液体称量加注（注射泵 + 天平）"},
}
SINGLE = ""


DeviceFailed = DeviceError  # 设备明确报错、这次动作失败（加样头用完、管路不出液、泵报错……）


class Cancelled(Exception):
    """被终止。"""


@dataclass
class Run:
    handle: str
    action: str
    material: str
    targets: dict[str, float]
    state: str = "running"
    actuals: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    mass: float | None = None
    cancel: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    powder_started: bool = False

    def public(self) -> dict[str, Any]:
        return {"handle": self.handle, "action": self.action, "material": self.material, "targets": self.targets,
                "state": self.state, "actuals": self.actuals, "error": self.error}


def _where(well: str) -> str:
    return f"孔位 {well} " if well else ""


class Station(Device):
    def __init__(self, config: Config, balance, quantos=None, pump=None, *, state_dir: str | Path | None = None,
                 faults=None, programs: dict[str, dict] | None = None, head_loader=None):
        self.config = config
        self.balance = balance
        self.quantos = quantos
        self.pump = pump
        self.faults = faults  # 只有模拟模式才有：统一控制口的故障注入
        # 只有模拟模式才有：代替配粉模组把要用的加样头装上 Quantos（真机上换头由搬运 / 人完成，网关只核对）
        self.head_loader = head_loader
        self.programs = {code: spec for code, spec in (programs or config.programs or DEFAULT_PROGRAMS).items()
                         if spec.get("action") in config.capabilities}
        self.runs: dict[str, Run] = {}
        self.counter = itertools.count(1)
        self.store = Path(state_dir) / "runs" if state_dir else None
        if self.store:
            self.store.mkdir(parents=True, exist_ok=True)
        self.busy_lock = threading.Lock()
        self.known: dict[str, Any] | None = None

    # ---------- 身份 ----------

    def identity(self) -> dict[str, Any]:
        # 加料进行中不去问天平：Quantos 加粉时同一条链路上还挂着「加完再回」的命令，插一条查询会把它的结论搅乱
        if self.busy_lock.locked() and self.known is not None:
            info = self.known
        else:
            info = self.known = self.balance.identity()
        interlock = bool(self.faults.interlock) if self.faults else False
        return {
            "device_id": self.config.device_id, "serial": info.get("serial") or self.config.device_id,
            "model": self.config.model or info.get("model", ""), "vendor": self.config.vendor,
            "firmware": info.get("firmware", ""),
            "methods": [{"program": code, "name": spec.get("name") or code,
                         "capability": self.config.capabilities[spec["action"]]} for code, spec in self.programs.items()],
            "interlock": interlock, "accepts_commands": not interlock, "simulator": bool(info.get("simulator")),
            "commands": ["dispatch", "retry", "abort", "query"],
        }

    # ---------- 启动 ----------

    def start(self, job: Job) -> str:
        action = self.config.action_of(job.capability)
        if action is None:
            raise Rejected("unsupported", f"这台站只做 {', '.join(self.config.capabilities.values())}，不做 {job.capability}")
        code = job.program or next((c for c, s in self.programs.items() if s["action"] == action), "")
        spec = self.programs.get(code)
        if spec is None or spec["action"] != action:
            raise Rejected("invalid", f"设备上没有 {job.capability} 的程序 {code or '（未指定）'}；可选 "
                                      + ", ".join(c for c, s in self.programs.items() if s["action"] == action))
        targets = self._targets(action, job.params)
        material = ""
        if action != "weigh":
            material = str((job.material or {}).get("name") or self.config.acceptance_material.get(action) or "")
            if not material:
                raise Rejected("invalid", "指令没带物料（material）：不知道要加哪种料，不加")
        if self.faults:
            self.faults.check_start()
        if not self.busy_lock.acquire(blocking=False):
            raise Rejected("busy", "天平上一次动作还没结束，设备未接受作业")
        handle = f"{job.command_id}#{next(self.counter)}"
        run = Run(handle=handle, action=action, material=material, targets=targets)
        started = False
        try:
            try:
                self._preflight(action, material)
                if action == "dose_solid":
                    self._begin_powder(run, job, spec)
            except Rejected:
                raise
            except DeviceBusy as exc:
                raise Rejected("busy", f"{exc}；设备未接受作业") from exc
            except DeviceError as exc:
                raise Rejected("invalid", f"{exc}；没有加料") from exc
            except LinkError as exc:
                if action == "dose_solid" and exc.sent and getattr(self.quantos, "start_sent", False):
                    raise  # 开始加粉的命令发出去了却没回：不知道开没开始，结果未知、不重发
                raise Rejected("busy", f"读不到设备状态，设备未接受作业：{exc}") from exc
            except Exception as exc:  # noqa: BLE001  还没开始加：读不到状态就是没动，不是结果未知
                raise Rejected("busy", f"读不到设备状态，设备未接受作业：{exc}") from exc
            mode = self.faults.moved() if self.faults else "none"
            run.thread = threading.Thread(target=self._work, args=(run, spec, mode), daemon=True,
                                          name=f"dose-{job.command_id}")
            self.runs[handle] = run
            run.thread.start()
            started = True
        finally:
            if not started:
                self.busy_lock.release()
        if mode == "slow_submit":
            time.sleep(self.faults.parameter)
        if mode == "lost_receipt":
            raise ReceiptLost(handle)
        return handle

    def _begin_powder(self, run: Run, job: Job, spec: dict) -> None:
        """Quantos：门、锁头、目标、容差、开始——回了「已收下」才算开始；之前出错一律是没加粉。"""
        well, target = next(iter(run.targets.items()))
        if target <= 0:
            return  # 这瓶不加这种料：不动 Quantos
        tolerance = float(spec.get("tolerance_pct") or self.config.solid.get("tolerance_pct") or 2)
        sample = "ILCS-" + hashlib.sha256(f"{job.command_id}#{well}".encode("utf-8")).hexdigest()[:12].upper()
        self.quantos.begin(target, tolerance, sample)
        run.powder_started = True

    def _targets(self, action: str, params: dict[str, Any]) -> dict[str, float]:
        """{孔位: 目标质量 g}；称量没有目标（值为 0）。指令只能有一瓶。"""
        allowed = {"wells"} | ({self.config.mass_param} if action != "weigh" else set())
        unknown = sorted(set(params) - allowed)
        if unknown:
            raise Rejected("invalid", f"网关不接受参数 {', '.join(unknown)}；{'称量不带参数' if action == 'weigh' else '只认 ' + self.config.mass_param}")
        raw = params.get("wells")
        if raw is None:
            rows = {SINGLE: params}
        elif not isinstance(raw, dict) or not raw:
            raise Rejected("invalid", "wells 要写成 {孔位: {参数: 值}}，至少一瓶")
        else:
            rows = {str(well): {**{k: v for k, v in params.items() if k != "wells"}, **(values or {})}
                    for well, values in raw.items() if isinstance(values, dict)}
            if len(rows) != len(raw):
                raise Rejected("invalid", "wells 里每个孔位的参数要写成对象")
        if len(rows) > 1:
            raise Rejected("unsupported", f"天平上一次只能放一瓶，这条指令有 {len(rows)} 瓶（{', '.join(rows)}）：按瓶分开下发")
        targets: dict[str, float] = {}
        for well, values in rows.items():
            extra = sorted(set(values) - allowed)
            if extra:
                raise Rejected("invalid", f"{_where(well)}带了网关不接受的参数 {', '.join(extra)}")
            if action == "weigh":
                targets[well] = 0.0
                continue
            value = values.get(self.config.mass_param)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise Rejected("invalid", f"{_where(well)}{self.config.mass_param} = {value!r} 不是非负的质量（g）")
            targets[well] = float(value)
        return targets

    def _preflight(self, action: str, material: str) -> None:
        if action == "dose_solid":
            if self.quantos is None:
                raise Rejected("unsupported", "这台站没有加粉装置")
            wanted = (self.config.solid.get("substances") or {}).get(material, material)
            if self.head_loader is not None:
                self.head_loader(wanted)
            head = self.quantos.head()
            if not head:
                raise Rejected("invalid", f"Quantos 上没有装加样头：要加 {material}")
            if str(head.get("substance") or "") != wanted:
                raise Rejected("invalid", f"装的加样头是 {head.get('substance') or '（未登记物质）'}，指令要 {material}"
                                          f"（加样头物质 {wanted}）：不加错料")
            remaining = head.get("remaining_doses")
            if isinstance(remaining, int) and remaining <= 0:
                raise Rejected("invalid", f"加样头 {head.get('substance')} 的剂次已用完")
        elif action == "dose_liquid":
            if self.pump is None:
                raise Rejected("unsupported", "这台站没有加液装置")
            if material not in self.config.liquids:
                raise Rejected("invalid", f"液体 {material} 没有登记阀端口：可加 {', '.join(self.config.liquids)}")
            self.pump.check_ready()
        self.balance.ensure_ready()

    # ---------- 后台执行 ----------

    def _work(self, run: Run, spec: dict, mode: str) -> None:
        try:
            for well, target in run.targets.items():
                if run.cancel.is_set():
                    raise Cancelled()
                if run.action == "weigh":
                    mass = self.balance.stable_weight(self.config.stable_timeout_sec)
                    if run.cancel.is_set():
                        raise Cancelled()  # 称量什么都没改变：终止了就不报这个读数
                    run.actuals[well] = {"mass": round(mass, 5)}
                    continue
                if target <= 0:
                    run.actuals[well] = {"mass": 0.0, "target": 0.0}  # 这瓶不加这种料
                    continue
                if run.action == "dose_solid":
                    mass = self.quantos.finish(run.cancel)
                else:
                    mass = self._dose_liquid(run, target, spec)
                run.actuals[well] = {"mass": round(mass, 5), "target": target}
            if mode == "stuck":
                run.cancel.wait()
                raise Cancelled()
            if mode == "fail":
                raise DeviceFailed("模拟故障：天平报过载，作业中止")
            run.state = "done"
        except (Cancelled, InterruptedError) as exc:
            detail = str(exc) if isinstance(exc, InterruptedError) and str(exc) else "被终止"
            run.state, run.error = "failed", detail + ("" if "已加" in detail else self._so_far(run))
        except (PumpError, LinkError) as exc:
            run.state, run.error = "failed", str(exc) + self._so_far(run)
        except DeviceError as exc:
            run.state, run.error = "failed", str(exc)  # 加料的错误消息里已经写明加了多少
        except Exception as exc:  # noqa: BLE001  后台线程不能把异常吞掉不报：判失败并写明
            run.state, run.error = "failed", f"执行出错：{exc}"
        finally:
            self._save(run)
            self.busy_lock.release()

    @staticmethod
    def _so_far(run: Run) -> str:
        """失败时写明这一瓶已经加进去多少（天平最后一次稳定读数）：没进账的量要人补录。"""
        return f"（这一瓶已加 {run.mass:.4f} g，未入账）" if run.mass else ""

    def _dose_liquid(self, run: Run, target: float, spec: dict) -> float:
        """去皮 → 先加九成 → 读数 → 补加，直到进入容差；每一轮都按天平实际读数算还差多少。"""
        liquid = self.config.liquids[run.material]
        settings = {**self.config.liquid, **{k: v for k, v in spec.items() if k != "action"}}
        tolerance = float(settings.get("tolerance_g") or 0.01)
        first = float(settings.get("first_fraction") or 0.9)
        rounds = int(settings.get("max_rounds") or 6)
        syringe = float(settings["syringe_ul"])
        min_ul = float(settings.get("min_dose_ul") or 2)
        self.balance.tare(self.config.stable_timeout_sec)
        dispensed, stalled = 0.0, 0
        for attempt in range(rounds):
            remaining = target - dispensed
            if remaining <= tolerance:
                return dispensed
            volume = remaining * (first if attempt == 0 else 1.0) / liquid.density * 1000
            volume = max(min_ul, volume)
            while volume > 0:
                if run.cancel.is_set():
                    self.pump.stop()
                    raise Cancelled()
                portion = min(volume, syringe)
                self.pump.transfer(liquid.port, int(settings["output_port"]), portion, run.cancel)
                volume -= portion
            before = dispensed
            dispensed = self.balance.stable_weight(self.config.stable_timeout_sec)
            run.mass = dispensed
            if dispensed - before < max(tolerance / 2, 0.001):
                stalled += 1
                if stalled >= 2:
                    raise DeviceFailed(f"连续两次加液天平读数没有变化（已加 {dispensed:.4f} g）：管路堵塞、气泡或 {run.material} 用完")
            else:
                stalled = 0
        if target - dispensed > tolerance:
            raise DeviceFailed(f"{rounds} 轮补加后仍差 {target - dispensed:.4f} g（已加 {dispensed:.4f} g，目标 {target:g} g）")
        return dispensed

    # ---------- 状态 ----------

    def _path(self, handle: str) -> Path | None:
        # 文件名取作业号的摘要：作业号里有「#」，不同平台对文件名的字符限制不一样
        return self.store / f"{hashlib.sha256(handle.encode('utf-8')).hexdigest()[:24]}.json" if self.store else None

    def _save(self, run: Run) -> None:
        path = self._path(run.handle)
        if path is None:
            return
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(run.public(), ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)

    def _load(self, handle: str) -> dict[str, Any] | None:
        path = self._path(handle)
        try:
            return json.loads(path.read_text(encoding="utf-8")) if path is not None and path.exists() else None
        except (OSError, ValueError):
            return None

    def status(self, job: Job) -> Status:
        run = self.runs.get(job.handle)
        data = run.public() if run is not None else self._load(job.handle)
        if data is None:
            # 网关重启前这次动作还没出结论：不知道加了多少，交网关照报原状态、人工核查
            raise RuntimeError(f"作业 {job.handle} 在网关重启前没有出结论：不知道实际加了多少")
        actuals = data["actuals"]
        if data["state"] == "running":
            mass = run.mass if run is not None else None
            telemetry = [{"metric": "mass", "value": float(mass), "setpoint": None}] if mass is not None else []
            return Status("running", telemetry=telemetry)
        delivered = self._delivered(data)
        telemetry = [{"metric": "mass", "value": float(row["mass"]), "setpoint": row.get("target")}
                     for row in actuals.values() if isinstance(row.get("mass"), (int, float))]
        return Status(data["state"], actuals=delivered, telemetry=telemetry[:1] if SINGLE in actuals else [],
                      error=data["error"])

    @staticmethod
    def _delivered(data: dict[str, Any]) -> dict[str, Any]:
        actuals = data["actuals"]
        delivered: dict[str, Any] = dict(actuals[SINGLE]) if set(actuals) == {SINGLE} else {"wells": actuals}
        if data["action"] != "weigh":
            total = sum(float(row.get("mass") or 0) for row in actuals.values())
            delivered["material"] = data["material"]
            if total > 0:
                delivered["materials"] = [{"material": data["material"], "unit": "g", "quantity": round(total, 5)}]
        return delivered

    # ---------- 终止 ----------

    def abort(self, job: Job) -> None:
        run = self.runs.get(job.handle)
        if run is None or run.state != "running":
            return  # 已经结束（或网关重启前就结束了）：设备在安全状态
        run.cancel.set()
        # 只停这次动作用的那台：称量没有可停的；同一条链路上给 Quantos 发停止会插进天平正在答的读数
        device = {"dose_solid": self.quantos, "dose_liquid": self.pump}.get(run.action)
        if device is not None:
            try:
                device.stop()
            except Exception:  # noqa: BLE001  停的命令发不出去：下面等线程结束时照实报
                pass
        if run.thread is not None:
            run.thread.join(timeout=30)
        if run.state == "running":
            raise RuntimeError("发了停止，但 30 秒内加料线程还没停下：请到现场核查")
        if run.state == "done":
            # 停止命令到之前已经做完：料确实加进去了。如实拒绝终止，原作业照报完成、按实际量入账
            masses = "、".join(f"{_where(well)}{row.get('mass')} g".strip() for well, row in run.actuals.items())
            raise Rejected("invalid", f"来不及终止：这次{'称量' if run.action == 'weigh' else '加料'}已经做完（{masses}）")

    def fault_target(self):
        return self.faults
