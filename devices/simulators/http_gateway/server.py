"""ILCS HTTPS JSON 网关模拟设备。

模拟「厂商 SDK / 私有协议 → 现场网关 → HTTPS JSON」这一类接入。网关本身就是设备模块用的网关 SDK
（`devices/sdk/ilcs_gateway`）：按指令号去重与回放、先落盘的作业台账、按指令号查询、回执丢了宁可不回、令牌与 TLS、
`/simulator/*` 控制接口，和交付给现场的网关是同一份代码；这里只把设备行为模型（`devices/simulators/common/device.py`）
当成「厂家 SDK」接上去。系统侧用 `http_json_v1` 驱动接入，和接一台真网关走同一条路。

    python devices/simulators/http_gateway/server.py --device-id SIM-COAT-01 --port 8443 \\
        --cert-dir /run/secrets/ilcs/gateway --host-name gateway-sim-coater

首次启动在 --cert-dir 生成自签证书 `<设备ID>.crt/.key` 与访问令牌 `<设备ID>.token`：系统侧 ca_file 指向证书，
credential_ref 指向令牌文件。每个请求都要带 `Authorization: Bearer <令牌>`，否则 401。
--insecure 用明文 HTTP，仅用于本机测试（系统侧要显式 allow_insecure_http）。

状态码与驱动的判定规则一一对应（由 SDK 保证）：参数非法 / 不支持 422、联锁 / 忙 423（明确拒绝，设备没动），
查不到指令 404；回执丢失时直接断开连接不回任何响应（驱动判结果未知）。

去重在网关台账里做，所以 `no_dedup` 故障在这里不会让设备重复动作；回执时间由网关给出，`clock_skew` 也不生效。
设备行为模型在内存里、进程重启就清空，台账缺省跟着放在临时目录（`--state-dir` 可指定）。
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import logging
import os
from pathlib import Path
import shutil
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT, ROOT / "sdk"):  # 直接运行（容器）时也能找到 simulators 包与网关 SDK
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from ilcs_gateway import Device, Job, ReceiptLost, Rejected, Status, build_server  # noqa: E402

from simulators.common.control import DeviceTarget, start_control  # noqa: E402
from simulators.common.device import DeviceRejected, ReceiptLost as LostOnDevice, SimulatedDevice  # noqa: E402
from simulators.common.runtime import (  # noqa: E402
    build_device, cert_dir_default, configure_logging, device_arguments, serve_forever,
)

# 设备行为模型的拒绝类别 → 网关 SDK 的拒绝类别
REJECTIONS = {"InvalidParameters": "invalid", "NotSupported": "unsupported", "Interlocked": "interlocked",
              "DeviceBusy": "busy"}
# 设备侧任务阶段 → 网关 SDK 的作业状态
PHASES = {"accepted": "running", "running": "running", "held": "held", "done": "done", "failed": "failed",
          "aborted": "failed"}
UNSUPPORTED = {
    "no_dedup": "网关按指令号去重：设备侧不去重也不会让设备重复动作",
    "clock_skew": "回执时间由网关给出，设备时钟偏差不进回执",
}
log = logging.getLogger("ilcs.gateway-sim")


class SimulatedGateway(Device):
    """网关 SDK 的 `Device`：底下是设备行为模型。设备认 ILCS 指令号，作业号就用指令号。"""

    def __init__(self, device: SimulatedDevice):
        self.device = device

    def identity(self) -> dict:
        return self.device.identity()

    def start(self, job: Job) -> str:
        context = {"batch_id": job.batch_id, "step_index": job.step_index, "step_id": job.step_id}
        with _rejections():
            try:
                self.device.submit(job.command_id, job.type, job.capability, job.params, context)
            except LostOnDevice as lost:
                raise ReceiptLost(job.command_id) from lost
        return job.command_id

    def status(self, job: Job) -> Status:
        receipt = self.device.query(job.handle)
        if receipt["state"] == "not_found":
            raise LookupError(f"设备侧没有作业 {job.handle}")  # 读不到状态：SDK 照报台账里的状态，不猜
        return Status(PHASES[receipt["phase"]], actuals=receipt["delivered"], telemetry=receipt["telemetry"],
                      error=receipt["error"])

    def hold(self, job: Job) -> None:
        with _rejections():
            self.device.hold("", job.handle)

    def resume(self, job: Job) -> None:
        with _rejections():
            self.device.resume(job.handle)

    def abort(self, job: Job) -> None:
        with _rejections():
            self.device.abort("", job.handle)

    def lookup(self, job: Job) -> str | None:
        return job.command_id if self.device.query(job.command_id)["state"] != "not_found" else None

    def fault_target(self) -> "SimulatedGateway":
        return self

    def set_fault(self, mode: str, parameter: float = 0.0) -> None:
        self.device.set_fault(mode, parameter)

    def fault_state(self) -> dict:
        raw = self.device.state()
        return {"device_id": raw["device_id"], "fault": raw["fault"], "fault_parameter": raw["fault_parameter"],
                "tasks": raw["tasks"], "motions": sum(raw["executions"].values()), "unsupported": dict(UNSUPPORTED)}


@contextmanager
def _rejections():
    """设备行为模型的明确拒绝换成 SDK 的 `Rejected`（设备没动）；其他异常原样抛出（SDK 判结果未知）。"""
    try:
        yield
    except DeviceRejected as error:
        raise Rejected(REJECTIONS[error.identifier], error.message) from error


class SimulatorRunner:
    def __init__(self, args: argparse.Namespace, device: SimulatedDevice):
        self.args = args
        self.device = device
        self._temporary_state = None if args.state_dir else tempfile.mkdtemp(prefix=f"ilcs-gateway-sim-{args.device_id}-")
        directory = Path(args.cert_dir)
        self.server = build_server(
            SimulatedGateway(device), device_id=args.device_id, state_dir=args.state_dir or self._temporary_state,
            address=args.address, port=args.port, prefix=args.path_prefix,
            token_file=directory / f"{args.device_id}.token", cert=directory / f"{args.device_id}.crt",
            key=directory / f"{args.device_id}.key", host_name=args.host_name, insecure=args.insecure,
        )

    def start(self) -> None:
        self.server.start()

    def stop(self) -> None:
        self.server.stop()
        if self._temporary_state:
            shutil.rmtree(self._temporary_state, ignore_errors=True)

    def control_target(self) -> DeviceTarget:
        """统一控制口（devices/simulators/common/control.py）。网关自己的 API 上也有同样的 /simulator/*，带网关令牌。"""
        return DeviceTarget(self.device, self.go_offline, unsupported=UNSUPPORTED)

    def go_offline(self, seconds: float) -> None:
        self.server.go_offline(seconds)


def parse(argv=None) -> argparse.Namespace:
    env = os.environ.get
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    device_arguments(parser, default_port=8443)
    parser.add_argument("--cert-dir", default=env("SIM_CERT_DIR", cert_dir_default("gateway")))
    parser.add_argument("--state-dir", default=env("SIM_STATE_DIR", ""), help="网关作业台账目录；缺省用临时目录")
    parser.add_argument("--path-prefix", default=env("SIM_PATH_PREFIX", "/api/v1"))
    parser.add_argument("--insecure", action="store_true", default=env("SIM_INSECURE", "0") == "1")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    configure_logging()
    args = parse(argv)
    device = build_device(args)
    runner = SimulatorRunner(args, device)
    runner.start()
    control = start_control(runner.control_target())
    serve_forever(device, args.tick_seconds, runner.stop)
    if control is not None:
        control.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
