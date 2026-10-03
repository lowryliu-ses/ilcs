"""驱动宿主命令行。

    python -m ilcs_host --site sites/<现场>            起服务，Ctrl-C / SIGTERM 退出
    python -m ilcs_host --site sites/<现场> --check    只检查配置：每台设备的插件、端口、特性与配置摘要，不连设备
"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading

from .plugins import PLUGINS
from .site import SiteError, load_site


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="ilcs_host", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--site", required=True, help="现场配置目录（含 host.json 与 devices/）")
    parser.add_argument("--check", action="store_true", help="只检查配置，打印每台设备的配置摘要")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # sila2 每次属性读取、每次命令都按 INFO 记三行（请求、令牌检查、返回值）；ILCS 每台设备每个探测周期都读，日志会被它淹没
    logging.getLogger("sila2").setLevel(logging.WARNING)

    from .server import features_of, prepare, start, stop

    try:
        site = load_site(args.site, set(PLUGINS))
        runtimes = prepare(site)
    except (SiteError, ValueError, RuntimeError) as exc:
        print(f"配置不对，没有启动：{exc}", file=sys.stderr)
        return 2
    if args.check:
        for runtime in runtimes:
            entry = runtime.entry
            print(json.dumps({
                "device": entry.key, "plugin": entry.plugin, "port": entry.port, "simulator": entry.simulator,
                "features": [f.fully_qualified_identifier.split("/")[2] for f in features_of(runtime)],
                "config_version": entry.config_version, "config_digest": entry.digest,
            }, ensure_ascii=False))
        return 0
    servers = start(site, runtimes)
    done = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: done.set())
    done.wait()
    stop(servers)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
