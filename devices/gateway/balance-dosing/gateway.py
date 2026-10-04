#!/usr/bin/env python
"""设备模块入口：起一个 ILCS 网关（`http_json_v1` 契约），后面接一台天平和可选的加粉 / 加液装置。

    # 开发机上：--simulate 在本进程里起假天平（MT-SICS + Quantos）和假注射泵（Cavro DT），真实接口照常连它们
    python gateway.py --simulate --insecure --port 8443

    # 现场：HTTPS + 令牌，证书与令牌首次启动生成在 --secrets 目录
    python gateway.py --config /etc/ilcs-gateway/balance.json --secrets /etc/ilcs-gateway \\
        --state-dir /var/lib/ilcs-gateway --host-name balance-gw.lab.internal

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
# SDK（ilcs_gateway）在模块的上一级目录；模块单独挪走时用 PYTHONPATH 指过去（镜像里就是这样）
for path in (HERE, HERE.parent):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from ilcs_gateway import serve  # noqa: E402

from driver.config import Config  # noqa: E402
from driver.device import Station  # noqa: E402


def build(simulate: bool, config_file: str, state_dir: str) -> tuple[Station, object | None]:
    """返回 (站, 模拟设备)。模拟设备在进程退出前要停掉。"""
    if simulate:
        from simulator import simulated_station

        return simulated_station(config_file or None, state_dir=state_dir)
    if not config_file:
        raise SystemExit("接真机要给 --config（网关配置，样例见 config.example.json），或加 --simulate 用模拟设备")
    from driver.cavro import CavroPump
    from driver.sics import Balance, Quantos

    config = Config.load(config_file)
    balance = Balance(config.balance)
    quantos = Quantos(balance) if config.solid else None
    pump = CavroPump(config.liquid) if config.liquid else None
    return Station(config, balance, quantos, pump, state_dir=state_dir), None


def parse(argv=None) -> argparse.Namespace:
    env = os.environ.get
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--simulate", action="store_true", default=env("GATEWAY_SIMULATE", "0") == "1")
    parser.add_argument("--config", default=env("GATEWAY_CONFIG", ""), help="网关配置 JSON（样例 config.example.json）")
    parser.add_argument("--address", default=env("GATEWAY_ADDRESS", ""),
                        help="监听地址；缺省 HTTPS 监听所有地址，--insecure 只监听 127.0.0.1")
    parser.add_argument("--port", type=int, default=int(env("GATEWAY_PORT", "8443")))
    parser.add_argument("--prefix", default=env("GATEWAY_PREFIX", "/api/v1"))
    parser.add_argument("--state-dir", default=env("GATEWAY_STATE_DIR", "./state"), help="作业台账目录，必须在持久盘上")
    parser.add_argument("--secrets", default=env("GATEWAY_SECRETS", "./secrets"), help="令牌与证书目录")
    parser.add_argument("--host-name", default=env("GATEWAY_HOST_NAME", "localhost"), help="写进证书的主机名")
    parser.add_argument("--insecure", action="store_true", default=env("GATEWAY_INSECURE", "0") == "1",
                        help="明文 HTTP，只限本机联调")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    logging.basicConfig(level=os.environ.get("GATEWAY_LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    args = parse(argv)
    try:
        station, simulation = build(args.simulate, args.config, args.state_dir)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(str(exc)) from exc
    device_id = station.config.device_id
    secrets = Path(args.secrets)
    server = serve(
        station, device_id=device_id, state_dir=args.state_dir, address=args.address or None, port=args.port,
        prefix=args.prefix, token_file=secrets / f"{device_id}.token", cert=secrets / f"{device_id}.crt",
        key=secrets / f"{device_id}.key", host_name=args.host_name, insecure=args.insecure,
    )
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    stop.wait()
    server.stop()
    if simulation is not None:
        simulation.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
