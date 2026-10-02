#!/usr/bin/env python
"""设备模块入口：起一个 ILCS 网关（`http_json_v1` 契约），后面接一台电化学工作站（PalmSens MethodSCRIPT：EmStat4 /
EmStat Pico / Nexus，USB 虚拟串口或串口服务器）。--simulate 用本进程里的假 MethodSCRIPT 仪器（走真的协议代码）。

    # 开发机上对着假仪器调（明文 HTTP，只限本机；不给 --config 就用模拟配置）
    python gateway.py --simulate --insecure --port 8443 --time-scale 0.05

    # 现场：网关跑在工作站 USB 插着的那台电脑上，HTTPS + 令牌；先只读地问一遍仪器
    python gateway.py --config C:\\ilcs-gateway\\echem.json --check
    python gateway.py --config C:\\ilcs-gateway\\echem.json --secrets C:\\ilcs-gateway\\secrets \\
        --state-dir C:\\ilcs-gateway\\state --host-name echem-gw.lab.internal

ILCS 侧：导入 profile.json 成设备接入模板、发布；工位套用模板，连接参数填本网关的地址、证书与设备编号。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import signal
import sys
import threading

HERE = Path(__file__).resolve().parent
# SDK：模块放在 ILCS 仓库的 devices/gateway/ 下时自动找到；别处用 ILCS_REPO 或 PYTHONPATH 指过去
REPO = Path(os.environ["ILCS_REPO"]) if os.environ.get("ILCS_REPO") else HERE.parents[2]
for path in (HERE, REPO / "devices" / "gateway"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from ilcs_gateway import serve  # noqa: E402

from driver.config import Config  # noqa: E402
from driver.device import Instrument  # noqa: E402
from driver.palmsens import MethodScript  # noqa: E402


def build(simulate: bool, config_file: str, state_dir: str, time_scale: float):
    """(网关设备, 模拟)；接真机时模拟是 None。"""
    if simulate:
        from simulator import simulated_instrument

        return simulated_instrument(config_file or None, state_dir=state_dir, time_scale=time_scale)
    if not config_file:
        raise SystemExit("接真机要给 --config（网关配置，样例见 config.example.json），或加 --simulate 用假仪器")
    config = Config.load(config_file)
    backend = MethodScript(config.link, timeout=config.timeout_sec, model=config.model)
    return Instrument(backend, config, state_dir=state_dir), None


def parse(argv=None) -> argparse.Namespace:
    env = os.environ.get
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--simulate", action="store_true", default=env("GATEWAY_SIMULATE", "0") == "1")
    parser.add_argument("--config", default=env("GATEWAY_CONFIG", ""), help="网关配置 JSON（样例 config.example.json）")
    parser.add_argument("--check", action="store_true", help="只读地问一遍仪器（固件、序列号、型号核对）就退出")
    parser.add_argument("--address", default=env("GATEWAY_ADDRESS", ""),
                        help="监听地址；缺省 HTTPS 监听所有地址，--insecure 只监听 127.0.0.1")
    parser.add_argument("--port", type=int, default=int(env("GATEWAY_PORT", "8443")))
    parser.add_argument("--prefix", default=env("GATEWAY_PREFIX", "/api/v1"))
    parser.add_argument("--state-dir", default=env("GATEWAY_STATE_DIR", "./state"),
                        help="作业台账与测量结论的目录，必须在持久盘上")
    parser.add_argument("--secrets", default=env("GATEWAY_SECRETS", "./secrets"), help="令牌与证书目录")
    parser.add_argument("--host-name", default=env("GATEWAY_HOST_NAME", "localhost"), help="写进证书的主机名")
    parser.add_argument("--insecure", action="store_true", default=env("GATEWAY_INSECURE", "0") == "1",
                        help="明文 HTTP，只限本机联调")
    parser.add_argument("--time-scale", type=float, default=float(env("GATEWAY_TIME_SCALE", "1")),
                        help="假仪器：出点的实际间隔 = 测量本身的间隔 × 这个系数")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    logging.basicConfig(level=os.environ.get("GATEWAY_LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    args = parse(argv)
    try:
        instrument, simulation = build(args.simulate, args.config, args.state_dir, args.time_scale)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(str(exc)) from exc
    if args.check:
        try:
            print(json.dumps(instrument.identity(), ensure_ascii=False, indent=2))
        finally:
            instrument.close()
            if simulation is not None:
                simulation.stop()
        return 0
    device_id = instrument.config.device_id
    secrets = Path(args.secrets)
    server = serve(
        instrument, device_id=device_id, state_dir=args.state_dir, address=args.address or None, port=args.port,
        prefix=args.prefix, token_file=secrets / f"{device_id}.token", cert=secrets / f"{device_id}.crt",
        key=secrets / f"{device_id}.key", host_name=args.host_name, insecure=args.insecure,
    )
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    stop.wait()
    server.stop()
    instrument.close()  # 停掉在测的（仪器断开电池）、放开串口：下次启动（或 PSTrace）才打得开
    if simulation is not None:
        simulation.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
