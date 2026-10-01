"""真实接口：把 BTS 的调用包成 `ilcs_gateway.Device`。去重、台账、查询、令牌、TLS 都由 SDK 负责。

一条 ILCS 指令 = 在一个白名单通道上按一个工步文件跑一次测试。判断规则：

- 能力不是 cap.test、工步没登记或文件不在、带了网关不认识的参数、通道不在白名单：`Rejected("invalid")`，没动；
  工步（倍率、截止电压……）都在 BTS 工步文件里定，**指令带来的工艺参数一律拒绝**，不悄悄忽略；
- 通道上还有测试在跑（或刚由本网关启动、BTS 还没刷新出来）：`Rejected("busy")`；
- BTS 明确拒绝（`BtsRefused`）：`Rejected("invalid")`，没动；连不上 BTS、启动命令没发出去（`BtsOffline`）：`Rejected("busy")`；
- 其他异常（超时、断线）原样抛出：网关按「结果未知」处理，绝不重发。

作业靠条码认：启动时条码写 `ILCS-<指令号摘要>`，查状态、找回作业、终止都先核对通道上的条码是不是这条指令的。
条码对不上就不认（通道上跑的是别的测试），**终止也不去停它**。
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import time
from typing import Any

from ilcs_gateway import Device, Job, Rejected, Status

from .bts_api import Bts, BtsOffline, BtsRefused
from .config import Config

CAPABILITY = "cap.test"
# 指令里网关认的参数：只有通道。通道写白名单里的序号（1 起）或通道号本身
PARAMETERS = ("channel",)
IDLE = {"finish", "stop", "protect"}
ACTIVE = {"working", "pause"}
STATES = {"working": "running", "pause": "held", "finish": "done", "stop": "failed", "protect": "failed"}
# 本网关刚启动的通道，BTS 多久还没把新条码刷出来就不再当它占着（之后按 BTS 报的状态算）
CLAIM_SEC = 60


def barcode_of(command_id: str) -> str:
    """指令号 → 写进 BTS 的条码。取摘要：指令号可能很长、带 BTS 不收的字符；同一指令号总是同一个条码。"""
    return "ILCS-" + hashlib.sha256(command_id.encode("utf-8")).hexdigest()[:12].upper()


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
        }

    # ---------- 启动 ----------

    def start(self, job: Job) -> str:
        if job.capability != CAPABILITY:
            raise Rejected("unsupported", f"这台设备只做 {CAPABILITY}，不做 {job.capability}")
        unknown = sorted(set(job.params) - set(PARAMETERS))
        if unknown:
            raise Rejected("invalid", f"工步在 BTS 工步文件里定，网关不接受参数 {', '.join(unknown)}；只认 channel")
        code = job.program or self.config.default_program
        program = self.config.programs.get(code)
        if program is None:
            raise Rejected("invalid", f"没有登记工步 {code or '（未指定）'}；可选 {', '.join(self.config.programs)}")
        if not Path(program.file).is_file():
            raise Rejected("invalid", f"工步 {code} 的文件 {program.file} 不在网关这台机器上")
        try:
            if self.bts.info().get("interlock"):
                raise Rejected("interlocked", "安全联锁触发，设备未动作")
            pipeline = self._pipeline(job.params.get("channel"))
        except Rejected:
            raise
        except Exception as exc:  # noqa: BLE001  启动命令还没发：读不到 BTS 就是没动，不是结果未知
            raise Rejected("busy", f"读不到 BTS 状态，设备未接受作业：{exc}") from exc
        barcode = barcode_of(job.command_id)
        try:
            self.bts.start(pipeline, barcode, program.file, self.config.data_dir)
        except BtsOffline as exc:
            raise Rejected("busy", f"{exc}；设备未接受作业") from exc
        except BtsRefused as exc:
            raise Rejected("invalid", f"BTS 拒绝在 {pipeline} 上启动：{exc}") from exc
        self.claimed[pipeline] = (barcode, time.monotonic())
        return pipeline

    def _pipeline(self, value: Any) -> str:
        channels = self.config.channels
        if value is None:
            if not self.config.auto_channel:
                raise Rejected("invalid", "指令没带通道（channel）：电池装在哪个通道要由 ILCS 指定")
            rows = self.bts.channels(list(channels))
            free = [pipeline for pipeline in channels if self._idle(pipeline, rows[pipeline])]
            if not free:
                raise Rejected("busy", "白名单通道都在使用，设备未接受作业")
            return free[0]
        if isinstance(value, bool):
            raise Rejected("invalid", f"channel = {value!r} 不是通道")
        if isinstance(value, (int, float)) and float(value).is_integer() and 1 <= int(value) <= len(channels):
            pipeline = channels[int(value) - 1]
        elif isinstance(value, str) and value in channels:
            pipeline = value
        else:
            raise Rejected("invalid", f"channel = {value!r} 不在白名单里：写 1–{len(channels)} 的序号或 {', '.join(channels)}")
        if not self._idle(pipeline, self.bts.channels([pipeline])[pipeline]):
            raise Rejected("busy", f"通道 {pipeline} 上还有测试，设备未接受作业")
        return pipeline

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

    def _row(self, job: Job) -> dict[str, Any]:
        """通道上这条指令的那一行；条码对不上（还没刷新出来，或通道已经跑了别的测试）就读不到，抛异常交网关照报原状态。"""
        pipeline = job.handle
        if pipeline not in self.config.channels:
            raise RuntimeError(f"作业号 {pipeline!r} 不是白名单通道")
        row = self.bts.channels([pipeline])[pipeline]
        if str(row.get("barcode")) != barcode_of(job.command_id):
            raise RuntimeError(f"通道 {pipeline} 上的条码是 {row.get('barcode')!r}，不是本作业的：读不到本作业的状态")
        return row

    def status(self, job: Job) -> Status:
        row = self._row(job)
        workstatus = str(row.get("workstatus") or "")
        mapped = STATES.get(workstatus)
        if mapped is None:
            raise RuntimeError(f"BTS 通道状态 {workstatus!r} 没有映射")  # 读不懂不等于失败：网关照报原状态
        actuals = {"channel": job.handle, "bts_barcode": barcode_of(job.command_id),
                   **{key: row.get(key) for key in ("cycle", "step", "step_type", "capacity", "energy")
                      if row.get(key) is not None}}
        telemetry = [{"metric": key, "value": float(row[key]), "setpoint": None}
                     for key in ("voltage", "current") if isinstance(row.get(key), (int, float))]
        error = {"stop": "在 BTS 上被停止", "protect": f"BTS 保护停机（log_code {row.get('log_code')}）"}.get(workstatus, "")
        return Status(mapped, actuals=actuals, telemetry=telemetry, error=error)

    # ---------- 终止、找回 ----------

    def abort(self, job: Job) -> None:
        try:
            row = self._row(job)
        except RuntimeError as exc:
            raise Rejected("invalid", f"没有停：{exc}") from exc
        if row.get("workstatus") not in ACTIVE:
            return  # 已经停了：设备在安全状态
        try:
            self.bts.stop(job.handle)
        except BtsRefused as exc:
            raise Rejected("invalid", f"BTS 拒绝停止 {job.handle}：{exc}") from exc

    def lookup(self, job: Job) -> str | None:
        barcode = barcode_of(job.command_id)
        rows = self.bts.channels(list(self.config.channels))
        return next((pipeline for pipeline, row in rows.items() if str(row.get("barcode")) == barcode), None)

    def fault_target(self):
        # 模拟接口的假 BTS 带故障状态；真 BTS 没有，统一控制口就不开
        return getattr(self.bts, "faults", None)
