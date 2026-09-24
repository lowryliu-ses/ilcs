"""给运行中的 HTTPS 网关模拟设备注入故障、查看状态。在模拟器容器里执行，沿用容器的 SIM_* 配置、证书与令牌：

    docker compose exec gateway-sim-coater python simulators/http_gateway/fault.py state
    docker compose exec gateway-sim-coater python simulators/http_gateway/fault.py lost_receipt
    docker compose exec gateway-sim-coater python simulators/http_gateway/fault.py offline 30
    docker compose exec gateway-sim-coater python simulators/http_gateway/fault.py none

调的是模拟器专有的 POST /simulator/fault 与 GET /simulator/state，带同一个 Bearer 令牌。
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import ssl
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from simulators.common.device import FAULT_MODES  # noqa: E402


def main() -> int:
    env = os.environ.get
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=["state", *FAULT_MODES])
    parser.add_argument("parameter", nargs="?", default="0", help="秒数等参数，按模式解释")
    parser.add_argument("--port", type=int, default=int(env("SIM_PORT", "8443")))
    parser.add_argument("--host-name", default=env("SIM_HOST_NAME", "localhost"))
    parser.add_argument("--device-id", default=env("SIM_DEVICE_ID", "SIM-DEVICE-01"))
    parser.add_argument("--cert-dir", default=env("SIM_CERT_DIR", "./gateway-certs"))
    parser.add_argument("--path-prefix", default=env("SIM_PATH_PREFIX", "/api/v1"))
    parser.add_argument("--insecure", action="store_true", default=env("SIM_INSECURE", "0") == "1")
    args = parser.parse_args()

    directory = Path(args.cert_dir)
    token = (directory / f"{args.device_id}.token").read_text().strip()
    scheme, context = "http", None
    if not args.insecure:
        scheme = "https"
        context = ssl.create_default_context(cafile=str(directory / f"{args.device_id}.crt"))
        context.check_hostname = False  # 容器内按 127.0.0.1 连，证书里是服务名；已钉住证书本身
    base = f"{scheme}://127.0.0.1:{args.port}{args.path_prefix.rstrip('/')}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if args.mode == "state":
        request = urllib.request.Request(f"{base}/simulator/state", headers=headers)
    else:
        body = json.dumps({"mode": args.mode, "parameter": float(args.parameter)}).encode()
        request = urllib.request.Request(f"{base}/simulator/fault", data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=10, context=context) as response:
        print(json.dumps(json.loads(response.read()), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
