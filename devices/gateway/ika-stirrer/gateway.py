#!/usr/bin/env python
"""设备模块入口：起一个 ILCS 网关（`http_json_v1` 契约），后面接一个工位上的几块 IKA 磁力加热搅拌器（NAMUR 串口命令）。

    # 开发机上：--simulate 在本进程里每个位置起一块假板（TCP 上说 NAMUR），真实接口照常连它们
    python gateway.py --simulate --insecure --port 8443

    # 现场：网关跑在接加热板的那台电脑上（USB / 串口，或串口服务器），HTTPS + 令牌
    python gateway.py --config C:\\ilcs-gateway\\stirrer.json --secrets C:\\ilcs-gateway\\secrets \\
        --state-dir C:\\ilcs-gateway\\state --host-name ika-stirrer-gw.lab.internal

    # 接好线以后、起网关之前：只读地问一遍每块板（不发写命令）
    python gateway.py --config C:\\ilcs-gateway\\stirrer.json --check

ILCS 侧：导入 profile.json 成设备接入模板、发布；工位套用模板，连接参数填本网关的地址、证书与设备编号。
网关退出（SIGTERM / Ctrl-C）时先把还在跑的板都停下：计时在网关里，网关不在就没人到点停。
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
    """返回 (工位, 模拟设备)。模拟设备在进程退出前要停掉。"""
    if simulate:
        from simulator import simulated_station

        return simulated_station(config_file or None, state_dir=state_dir)
    if not config_file:
        raise SystemExit("接真机要给 --config（网关配置，样例见 config.example.json），或加 --simulate 用假加热板")
    from driver.link import Link
    from driver.namur import Hotplate

    config = Config.load(config_file)
    plates = {key: Hotplate(Link(position.link)) for key, position in config.positions.items()}
    return Station(config, plates, state_dir=state_dir), None


def check(config_file: str) -> int:
    """只读地问一遍每块板：型号、温度、转速。不起网关、不发任何写命令（也不碰作业记录）。"""
    if not config_file:
        raise SystemExit("--check 要给 --config")
    from driver.link import Link, LinkError
    from driver.namur import Hotplate, NamurError

    config = Config.load(config_file)
    failures = 0
    for position in config.positions.values():
        plate = Hotplate(Link(position.link))
        try:
            print(f"{position.label()}  {plate.describe()}  型号 {plate.name()}  "
                  f"温度 {plate.temperature(position.channel):g} ℃（{position.sensor}）  转速 {plate.speed():g} rpm")
        except (LinkError, NamurError) as exc:
            failures += 1
            print(f"{position.label()}  {plate.describe()}  读不到：{exc}")
        finally:
            plate.link.close()
    return 1 if failures else 0


def parse(argv=None) -> argparse.Namespace:
    env = os.environ.get
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--simulate", action="store_true", default=env("GATEWAY_SIMULATE", "0") == "1")
    parser.add_argument("--config", default=env("GATEWAY_CONFIG", ""), help="网关配置 JSON（样例 config.example.json）")
    parser.add_argument("--address", default=env("GATEWAY_ADDRESS", ""),
                        help="监听地址；缺省 HTTPS 监听所有地址，--insecure 只监听 127.0.0.1")
    parser.add_argument("--port", type=int, default=int(env("GATEWAY_PORT", "8443")))
    parser.add_argument("--prefix", default=env("GATEWAY_PREFIX", "/api/v1"))
    parser.add_argument("--state-dir", default=env("GATEWAY_STATE_DIR", "./state"),
                        help="作业台账与作业记录目录，必须在持久盘上")
    parser.add_argument("--secrets", default=env("GATEWAY_SECRETS", "./secrets"), help="令牌与证书目录")
    parser.add_argument("--host-name", default=env("GATEWAY_HOST_NAME", "localhost"), help="写进证书的主机名")
    parser.add_argument("--insecure", action="store_true", default=env("GATEWAY_INSECURE", "0") == "1",
                        help="明文 HTTP，只限本机联调")
    parser.add_argument("--check", action="store_true", help="只读地问一遍配置里的每块板（型号、温度、转速）就退出")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    logging.basicConfig(level=os.environ.get("GATEWAY_LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    args = parse(argv)
    if args.check:
        try:
            return check(args.config)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise SystemExit(str(exc)) from exc
    try:
        station, simulation = build(args.simulate, args.config, args.state_dir)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(str(exc)) from exc
    device_id = station.config.device_id
    secrets = Path(args.secrets)
    try:
        server = serve(
            station, device_id=device_id, state_dir=args.state_dir, address=args.address or None, port=args.port,
            prefix=args.prefix, token_file=secrets / f"{device_id}.token", cert=secrets / f"{device_id}.crt",
            key=secrets / f"{device_id}.key", host_name=args.host_name, insecure=args.insecure,
        )
    except BaseException:
        station.close()
        if simulation is not None:
            simulation.stop()
        raise
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    stop.wait()
    server.stop()
    station.close()  # 还在跑的板先停下；等不到确认的，下次启动时接着停
    if simulation is not None:
        simulation.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
