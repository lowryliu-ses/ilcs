"""真实接口：把 BTS 的调用包成 `ilcs_gateway.Device`。去重、台账、查询、令牌、TLS 都由 SDK 负责。

一条 ILCS 指令 = 一个工步文件，在一个或几个白名单通道上各跑一次测试：

- 不带 `wells`：一颗电芯，通道写在 `channel`；
- 带 `wells`（ILCS 的逐孔参数，一个孔位一颗电芯）：`{孔位: {"channel": 通道}}`，每个孔位各启动一个通道。
  ILCS 一个批次的一步只发一条指令，电芯在哪个通道通常由上柜的人工步骤按样本记下、前馈到这一步。
  孔位没写通道时用指令顶层的 `channel`（ILCS 里步骤固定参数是逐孔参数的缺省值）；两个孔位落到同一个通道就拒绝。

判断规则：

- 能力不是 cap.test、工步没登记或文件不在、带了网关不认识的参数、通道不在白名单：`Rejected("invalid")`，没动；
  工步（倍率、截止电压……）都在 BTS 工步文件里定，**指令带来的工艺参数一律拒绝**，不悄悄忽略；
- 要用的通道上还有测试在跑（或刚由本网关启动、BTS 还没刷新出来）：`Rejected("busy")`，一个都不启动；
- 第一个通道 BTS 就明确拒绝（`BtsRefused`）：`Rejected("invalid")`；连不上 BTS（`BtsOffline`）：`Rejected("busy")`；
- **已经启动了几个、后面的通道被拒**：只做了一部分，抛异常按结果未知处理，交人到 BTS 核查，不自动停已启动的；
- 其他异常（超时、断线）原样抛出：网关按「结果未知」处理，绝不重发。

作业靠条码认：每个孔位的条码是 `ILCS-<指令号与孔位的摘要>`，查状态、找回作业、终止都先核对通道上的条码。
条码对不上就不认（通道上跑的是别的测试），**终止也不去停它**。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time
from typing import Any

from ilcs_gateway import Device, Job, ReceiptLost, Rejected, Status

from .bts_api import Bts, BtsOffline, BtsRefused
from .config import Config

CAPABILITY = "cap.test"
# 指令里网关认的参数：通道 channel（白名单里的序号，1 起；或通道号本身）与逐孔参数 wells；孔位里只认 channel
PARAMETERS = ("channel", "wells")
WELL_PARAMETERS = ("channel",)
IDLE = {"finish", "stop", "protect"}
ACTIVE = {"working", "pause"}
STATES = {"working": "running", "pause": "held", "finish": "done", "stop": "failed", "protect": "failed"}
READINGS = ("cycle", "step", "step_type", "capacity", "energy")
# 本网关刚启动的通道，BTS 多久还没把新条码刷出来就不再当它占着（之后按 BTS 报的状态算）
CLAIM_SEC = 60
# 单电芯指令（不带 wells）在作业里的孔位键
SINGLE = ""


def barcode_of(command_id: str, well: str = SINGLE) -> str:
    """指令号（+ 孔位）→ 写进 BTS 的条码。取摘要：指令号可能很长、带 BTS 不收的字符；同一输入总是同一个条码。"""
    key = f"{command_id}#{well}" if well else command_id
    return "ILCS-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12].upper()


def _handle(layout: dict[str, str]) -> str:
    """{孔位: 通道} → 作业号。单电芯就是通道号本身；多电芯是紧凑 JSON。"""
    if set(layout) == {SINGLE}:
        return layout[SINGLE]
    return json.dumps(layout, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _layout(handle: str) -> dict[str, str]:
    if not handle.startswith("{"):
        return {SINGLE: handle}
    try:
        data = json.loads(handle)
    except ValueError as exc:
        raise RuntimeError(f"作业号 {handle!r} 读不懂") from exc
    return {str(well): str(pipeline) for well, pipeline in data.items()}


def _where(well: str) -> str:
    return f"孔位 {well} " if well else ""


class Instrument(Device):
    def __init__(self, bts: Bts, config: Config):
        self.bts = bts
        self.config = config
        # 本网关启动过、BTS 可能还没刷新出来的通道：{通道号: (条码, 启动时刻)}
        self.claimed: dict[str, tuple[str, float]] = {}

    def identity(self) -> dict[str, Any]:
        info = self.bts.info()
        missing = [pipeline for pipeline in self.config.channels if pipeline not in set(info.get("pipelines") or [])]
        if missing:
            raise RuntimeError(f"配置的通道 {', '.join(missing)} 不在 BTS 上：核对网关配置的 channels")
        interlock = bool(info.get("interlock"))
        return {
            "device_id": self.config.device_id, "serial": self.config.device_id, "model": self.config.model,
            "vendor": self.config.vendor, "firmware": str(info.get("version") or ""),
            "methods": [{"program": code, "name": program.name, "capability": CAPABILITY}
                        for code, program in self.config.programs.items()],
            "channels": len(self.config.channels), "interlock": interlock, "accepts_commands": not interlock,
            "simulator": bool(info.get("simulator")),
            # BTS 接口不做保持，也就没有续跑
            "commands": ["dispatch", "retry", "abort", "query"],
        }

    # ---------- 启动 ----------

    def start(self, job: Job) -> str:
        if job.capability != CAPABILITY:
            raise Rejected("unsupported", f"这台设备只做 {CAPABILITY}，不做 {job.capability}")
        unknown = sorted(set(job.params) - set(PARAMETERS))
        if unknown:
            raise Rejected("invalid", f"工步在 BTS 工步文件里定，网关不接受参数 {', '.join(unknown)}；只认 channel 与 wells")
        code = job.program or self.config.default_program
        program = self.config.programs.get(code)
        if program is None:
            raise Rejected("invalid", f"没有登记工步 {code or '（未指定）'}；可选 {', '.join(self.config.programs)}")
        if not Path(program.file).is_file():
            raise Rejected("invalid", f"工步 {code} 的文件 {program.file} 不在网关这台机器上")
        wells = self._wells(job.params)
        try:
            if self.bts.info().get("interlock"):
                raise Rejected("interlocked", "安全联锁触发，设备未动作")
            layout = self._assign(wells)
        except Rejected:
            raise
        except Exception as exc:  # noqa: BLE001  启动命令还没发：读不到 BTS 就是没动，不是结果未知
            raise Rejected("busy", f"读不到 BTS 状态，设备未接受作业：{exc}") from exc
        started: list[str] = []
        lost = False
        for well, pipeline in layout.items():
            barcode = barcode_of(job.command_id, well)
            try:
                self.bts.start(pipeline, barcode, program.file, self.config.data_dir)
            except ReceiptLost:
                lost = True  # 模拟设备：通道已经启动，应答要丢掉；其余孔位照常启动
            except (BtsOffline, BtsRefused) as exc:
                if not started and not lost:
                    if isinstance(exc, BtsOffline):
                        raise Rejected("busy", f"{exc}；设备未接受作业") from exc
                    raise Rejected("invalid", f"BTS 拒绝在 {_where(well)}{pipeline} 上启动：{exc}") from exc
                raise RuntimeError(
                    f"{'、'.join(started) or '前面的通道'} 已经启动，{_where(well)}{pipeline} 没能启动（{exc}）："
                    "这条指令只做了一部分，请到 BTS 核查"
                ) from exc
            self.claimed[pipeline] = (barcode, time.monotonic())
            started.append(f"{_where(well)}{pipeline}".strip())
        handle = _handle(layout)
        if lost:
            raise ReceiptLost(handle)
        return handle

    def _wells(self, params: dict[str, Any]) -> dict[str, Any]:
        """{孔位: 指定的通道（没指定是 None）}。不带 wells 时是一颗电芯（孔位键为空）。"""
        raw = params.get("wells")
        if raw is None:
            return {SINGLE: params.get("channel")}
        if not isinstance(raw, dict) or not raw:
            raise Rejected("invalid", "wells 要写成 {孔位: {channel: 通道}}，至少一个孔位")
        wells: dict[str, Any] = {}
        for well, values in raw.items():
            if not str(well).strip() or not isinstance(values, dict):
                raise Rejected("invalid", f"孔位 {well!r} 的参数要写成对象，如 {{\"channel\": 3}}")
            unknown = sorted(set(values) - set(WELL_PARAMETERS))
            if unknown:
                raise Rejected("invalid", f"孔位 {well} 带了网关不接受的参数 {', '.join(unknown)}："
                                          "工步在 BTS 工步文件里定，每个孔位只认 channel")
            wells[str(well)] = values.get("channel", params.get("channel"))
        return wells

    def _resolve(self, value: Any) -> str:
        channels = self.config.channels
        if isinstance(value, bool):
            raise Rejected("invalid", f"channel = {value!r} 不是通道")
        if isinstance(value, (int, float)) and float(value).is_integer() and 1 <= int(value) <= len(channels):
            return channels[int(value) - 1]
        if isinstance(value, str) and value in channels:
            return value
        raise Rejected("invalid", f"channel = {value!r} 不在白名单里：写 1–{len(channels)} 的序号或 {', '.join(channels)}")

    def _assign(self, wells: dict[str, Any]) -> dict[str, str]:
        """每个孔位落到哪个通道：指定的先核对（白名单、不重复、空闲），没指定的按白名单顺序挑空闲的（要打开 auto_channel）。"""
        channels = self.config.channels
        layout: dict[str, str] = {}
        owner: dict[str, str] = {}
        for well, value in wells.items():
            if value is None:
                continue
            pipeline = self._resolve(value)
            if pipeline in owner:
                raise Rejected("invalid", f"孔位 {owner[pipeline]} 和 {well} 都要用通道 {pipeline}：一个通道只能放一颗电芯")
            layout[well], owner[pipeline] = pipeline, well
        rows = self.bts.channels(list(channels))
        busy = [pipeline for pipeline in layout.values() if not self._idle(pipeline, rows[pipeline])]
        if busy:
            raise Rejected("busy", f"通道 {', '.join(busy)} 上还有测试，设备未接受作业")
        missing = [well for well, value in wells.items() if value is None]
        if missing:
            if not self.config.auto_channel:
                where = f"（孔位 {', '.join(missing)}）" if missing != [SINGLE] else ""
                raise Rejected("invalid", f"指令没带通道（channel）{where}：电池装在哪个通道要由 ILCS 指定")
            free = [pipeline for pipeline in channels if pipeline not in owner and self._idle(pipeline, rows[pipeline])]
            if len(free) < len(missing):
                raise Rejected("busy", f"空闲的白名单通道只有 {len(free)} 个，这条指令要 {len(missing)} 个，设备未接受作业")
            layout.update(zip(missing, free))
        return {well: layout[well] for well in wells}

    def _idle(self, pipeline: str, row: dict[str, Any]) -> bool:
        if row.get("workstatus") not in IDLE:
            return False
        claim = self.claimed.get(pipeline)
        if claim is None:
            return True
        barcode, since = claim
        # 刚启动、BTS 还没把新条码刷出来：通道其实已经被占了
        return str(row.get("barcode")) == barcode or time.monotonic() - since > CLAIM_SEC

    # ---------- 状态 ----------

    def _read(self, job: Job) -> dict[str, tuple[str, dict[str, Any], bool]]:
        """{孔位: (通道, BTS 那一行, 条码是不是本作业的)}。"""
        layout = _layout(job.handle)
        outside = [pipeline for pipeline in layout.values() if pipeline not in self.config.channels]
        if outside:
            raise RuntimeError(f"作业号里的通道 {', '.join(outside)} 不是白名单通道")
        rows = self.bts.channels(sorted(set(layout.values())))
        return {well: (pipeline, rows[pipeline], str(rows[pipeline].get("barcode")) == barcode_of(job.command_id, well))
                for well, pipeline in layout.items()}

    def status(self, job: Job) -> Status:
        rows = self._read(job)
        foreign = [f"{_where(well)}{pipeline} 上的条码是 {row.get('barcode')!r}"
                   for well, (pipeline, row, mine) in rows.items() if not mine]
        if foreign:
            # 还没刷新出来，或通道已经跑了别的测试：读不到本作业的状态，抛异常交网关照报原状态
            raise RuntimeError("、".join(foreign) + "，不是本作业的：读不到本作业的状态")
        states, errors = {}, []
        for well, (pipeline, row, _) in rows.items():
            workstatus = str(row.get("workstatus") or "")
            if workstatus not in STATES:
                raise RuntimeError(f"{_where(well)}BTS 通道状态 {workstatus!r} 没有映射")  # 读不懂不等于失败
            states[well] = STATES[workstatus]
            reason = {"stop": "在 BTS 上被停止", "protect": f"BTS 保护停机（log_code {row.get('log_code')}）"}.get(workstatus)
            if reason:
                errors.append(f"{_where(well)}{pipeline} {reason}" if well else reason)
        values = set(states.values())
        state = next(name for name in ("running", "held", "failed", "done") if name in values)
        if set(rows) == {SINGLE}:
            pipeline, row, _ = rows[SINGLE]
            actuals = {"channel": pipeline, "bts_barcode": barcode_of(job.command_id), **self._readings(row)}
            telemetry = self._telemetry(row)
        else:
            actuals = {"wells": {well: {"channel": pipeline, "bts_barcode": barcode_of(job.command_id, well),
                                        "workstatus": row.get("workstatus"), **self._readings(row)}
                                 for well, (pipeline, row, _) in rows.items()}}
            telemetry = [point for well, (_, row, _) in rows.items() for point in self._telemetry(row, well)]
        return Status(state, actuals=actuals, telemetry=telemetry, error="；".join(errors))

    @staticmethod
    def _readings(row: dict[str, Any]) -> dict[str, Any]:
        return {key: row.get(key) for key in READINGS if row.get(key) is not None}

    @staticmethod
    def _telemetry(row: dict[str, Any], well: str = SINGLE) -> list[dict[str, Any]]:
        return [{"metric": f"{key}@{well}" if well else key, "value": float(row[key]), "setpoint": None}
                for key in ("voltage", "current") if isinstance(row.get(key), (int, float))]

    # ---------- 终止、找回 ----------

    def abort(self, job: Job) -> None:
        try:
            rows = self._read(job)
        except RuntimeError as exc:
            raise Rejected("invalid", f"没有停：{exc}") from exc
        mine = {well: (pipeline, row) for well, (pipeline, row, own) in rows.items() if own}
        if not mine:
            raise Rejected("invalid", "没有停：通道上的条码都不是本作业的（通道已经跑了别的测试，或 BTS 还没刷新出来）")
        stopped, refused = [], []
        for well, (pipeline, row) in mine.items():
            if row.get("workstatus") not in ACTIVE:
                continue  # 已经停了：这个通道在安全状态
            try:
                self.bts.stop(pipeline)
                stopped.append(pipeline)
            except BtsRefused as exc:
                refused.append(f"{_where(well)}{pipeline}（{exc}）")
        if refused:
            done = f"已停 {', '.join(stopped)}；" if stopped else ""
            raise Rejected("invalid", f"{done}BTS 拒绝停止 {'、'.join(refused)}")

    def lookup(self, job: Job) -> str | None:
        """启动没拿到应答：按条码在白名单通道里找回每个孔位；有一个找不到就不认（只做了一部分要人核查）。"""
        raw = job.params.get("wells")
        wells = [str(well) for well in raw] if isinstance(raw, dict) and raw else [SINGLE]
        rows = self.bts.channels(list(self.config.channels))
        by_barcode = {str(row.get("barcode")): pipeline for pipeline, row in rows.items()}
        layout = {}
        for well in wells:
            pipeline = by_barcode.get(barcode_of(job.command_id, well))
            if pipeline is None:
                return None
            layout[well] = pipeline
        return _handle(layout)

    def fault_target(self):
        # 模拟接口的假 BTS 带故障状态；真 BTS 没有，统一控制口就不开
        return getattr(self.bts, "faults", None)
