#!/usr/bin/env python
"""设备模块入口：起一个 ILCS 网关（`http_json_v1` 契约）。--simulate 用模拟接口，否则连真实的厂家 SDK。

    # 开发机上对着模拟接口调（明文 HTTP，只限本机）
    python gateway.py --simulate --insecure --port 8443

    # 现场：HTTPS + 令牌，证书与令牌首次启动生成在 --secrets 目录，ILCS 侧的 ca_file / credential_ref 指向它们
    VENDOR_SDK_MODULE=vendor_cycler python gateway.py --device-id CYC-0231 --secrets /etc/ilcs-gateway \\
        --host-name cycler-gw.lab.internal --state-dir /var/lib/ilcs-gateway

ILCS 侧：导入 profile.json 成设备接入模板、发布；工位套用模板，连接参数填本网关的地址、证书与设备编号。
"""
from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
import signal
import sys
import threading

HERE = Path(__file__).resolve().parent
# SDK：模块放在 ILCS 仓库的 devices/modules/ 下时自动找到；别处用 ILCS_REPO 或 PYTHONPATH 指过去
REPO = Path(os.environ["ILCS_REPO"]) if os.environ.get("ILCS_REPO") else HERE.parents[2]
for path in (HERE, REPO / "devices" / "sdk"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from ilcs_gateway import serve  # noqa: E402

from driver.device import Instrument  # noqa: E402


def build(simulate: bool, device_id: str, run_seconds: float) -> Instrument:
    if simulate:
        from simulator.fake_sdk import FakeVendorSdk

        return Instrument(FakeVendorSdk(device_id, run_seconds=run_seconds))
    from driver.vendor_sdk import load_sdk

    return Instrument(load_sdk())


def parse(argv=None) -> argparse.Namespace:
    env = os.environ.get
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--simulate", action="store_true", default=env("GATEWAY_SIMULATE", "0") == "1")
    parser.add_argument("--device-id", default=env("GATEWAY_DEVICE_ID", "SIM-CYC-M1"))
    parser.add_argument("--address", default=env("GATEWAY_ADDRESS", ""),
                        help="监听地址；缺省 HTTPS 监听所有地址，--insecure 只监听 127.0.0.1")
    parser.add_argument("--port", type=int, default=int(env("GATEWAY_PORT", "8443")))
    parser.add_argument("--prefix", default=env("GATEWAY_PREFIX", "/api/v1"))
    parser.add_argument("--state-dir", default=env("GATEWAY_STATE_DIR", "./state"), help="作业台账目录，必须在持久盘上")
    parser.add_argument("--secrets", default=env("GATEWAY_SECRETS", "./secrets"), help="令牌与证书目录")
    parser.add_argument("--host-name", default=env("GATEWAY_HOST_NAME", "localhost"), help="写进证书的主机名")
    parser.add_argument("--insecure", action="store_true", default=env("GATEWAY_INSECURE", "0") == "1",
                        help="明文 HTTP，只限本机联调")
    parser.add_argument("--run-seconds", type=float, default=float(env("GATEWAY_RUN_SECONDS", "5")),
                        help="模拟接口：一个作业跑多久")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    logging.basicConfig(level=os.environ.get("GATEWAY_LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    args = parse(argv)
    secrets = Path(args.secrets)
    server = serve(
        build(args.simulate, args.device_id, args.run_seconds), device_id=args.device_id, state_dir=args.state_dir,
        address=args.address or None, port=args.port, prefix=args.prefix, token_file=secrets / f"{args.device_id}.token",
        cert=secrets / f"{args.device_id}.crt", key=secrets / f"{args.device_id}.key", host_name=args.host_name,
        insecure=args.insecure,
    )
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    stop.wait()
    server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
