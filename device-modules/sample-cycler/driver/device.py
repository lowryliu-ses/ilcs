"""真实接口：把厂家 SDK 的调用包成 `ilcs_gateway.Device`。去重、台账、查询、令牌、TLS 都由 SDK 负责。

这一层只回答四个问题：怎么让设备开始、怎么读状态、怎么停、设备是谁。判断规则写清楚：
- 参数不在设备允许范围、程序不存在、通道占满、急停按下：`Rejected`，设备没动；
- 厂家 SDK 报的明确错误（`SdkError`）：同样是设备没动；
- 其他异常（超时、断线）原样抛出：网关按「结果未知」处理，绝不重发。
"""
from __future__ import annotations

from typing import Any

from ilcs_gateway import Device, Job, Rejected, Status

from .vendor_sdk import SdkError, VendorSdk

CAPABILITY = "cap.test"
# 设备允许的参数范围（与 ILCS 工位能力极限核对）；缺省程序与可选程序
PARAMETERS = {"rate": (0.01, 10.0), "vmax": (2.0, 5.0)}
PROGRAMS = {"CC-CV": "恒流恒压循环", "GITT": "恒电流间歇滴定"}
DEFAULT_PROGRAM = "CC-CV"
STATES = {"RUNNING": "running", "PAUSED": "held", "FINISHED": "done", "ERROR": "failed", "STOPPED": "failed"}


class Instrument(Device):
    def __init__(self, sdk: VendorSdk):
        self.sdk = sdk

    def identity(self) -> dict[str, Any]:
        info = self.sdk.info()
        return {
            "device_id": info["serial"], "serial": info["serial"], "model": info.get("model", ""),
            "vendor": info.get("vendor", ""), "firmware": info.get("firmware", ""),
            "methods": [{"program": program, "name": name, "capability": CAPABILITY} for program, name in PROGRAMS.items()],
            "channels": info.get("channels", 1), "interlock": bool(info.get("estop")),
            "accepts_commands": not info.get("estop"), "simulator": bool(info.get("simulator")),
        }

    def start(self, job: Job) -> str:
        if job.capability != CAPABILITY:
            raise Rejected("unsupported", f"这台设备只做 {CAPABILITY}，不做 {job.capability}")
        program = job.program or DEFAULT_PROGRAM
        if program not in PROGRAMS:
            raise Rejected("invalid", f"设备上没有程序 {program}；可选 {', '.join(PROGRAMS)}")
        settings = {}
        for name, (low, high) in PARAMETERS.items():
            value = job.params.get(name)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not low <= value <= high:
                raise Rejected("invalid", f"{name} = {value!r} 不在设备允许的 {low}–{high} 之间")
            settings[name] = float(value)
        if self.sdk.info().get("estop"):
            raise Rejected("interlocked", "急停按下，设备未动作")
        channels = self.sdk.free_channels()
        if not channels:
            raise Rejected("busy", "通道都在使用，设备未接受作业")
        try:
            return self.sdk.start_program(channels[0], program, settings, tag=f"ILCS {job.command_id}")
        except SdkError as exc:
            raise Rejected("invalid", f"厂家 SDK 拒绝：{exc}") from exc

    def status(self, job: Job) -> Status:
        state = self.sdk.run_state(job.handle)
        mapped = STATES.get(str(state.get("state") or ""))
        if mapped is None:
            raise RuntimeError(f"设备状态 {state.get('state')!r} 没有映射")  # 读不懂不等于失败：网关照报原状态
        actuals = {"cycles_completed": int(state.get("cycles") or 0),
                   "discharge_capacity_mAh": float(state.get("capacity_mAh") or 0)}
        telemetry = [{"metric": "voltage", "value": float(state["voltage"]), "setpoint": job.params.get("vmax")}] \
            if state.get("voltage") is not None else []
        return Status(mapped, actuals=actuals, telemetry=telemetry,
                      error=str(state.get("alarm") or ("被终止" if state.get("state") == "STOPPED" else "")))

    def hold(self, job: Job) -> None:
        self._call(self.sdk.pause, job)

    def resume(self, job: Job) -> None:
        self._call(self.sdk.resume, job)

    def abort(self, job: Job) -> None:
        self._call(self.sdk.stop, job)

    def lookup(self, job: Job) -> str | None:
        return self.sdk.find_run(f"ILCS {job.command_id}")

    def fault_target(self):
        # 模拟接口的假 SDK 带故障状态；真 SDK 没有，统一控制口就不开
        return getattr(self.sdk, "faults", None)

    @staticmethod
    def _call(action, job: Job) -> None:
        try:
            action(job.handle)
        except SdkError as exc:
            raise Rejected("invalid", f"厂家 SDK 拒绝：{exc}") from exc
