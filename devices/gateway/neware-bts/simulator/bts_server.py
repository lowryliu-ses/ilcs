#!/usr/bin/env python
"""假 BTS 的 TCP 服务：把 `FakeBts` 套上 BTS 8.0 的 XML 接口（connect、getdevinfo、inquire、start、stop）。

用来测真实接口（driver/bts.py + aurora-neware）走的那条线：拼命令、解析应答、断线、回执丢失。
报文格式照 aurora-neware 测试里录下的 BTS 应答；没实现的命令回 `<result>unsupported</result>`。

    # 现场没有 BTS 时，起一个假 BTS，再让网关（不带 --simulate）连它
    python simulator/bts_server.py --port 5502
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import socketserver
import sys
import threading
from typing import Any
from xml.etree import ElementTree
from xml.sax.saxutils import quoteattr

HERE = Path(__file__).resolve().parents[1]
REPO = Path(os.environ["ILCS_REPO"]) if os.environ.get("ILCS_REPO") else HERE.parents[2]
for path in (HERE, REPO / "devices" / "gateway"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from ilcs_gateway import ReceiptLost, Rejected  # noqa: E402

from driver.bts_api import BtsRefused  # noqa: E402
from simulator.fake_bts import FakeBts  # noqa: E402

TERMINATION = b"\n\n#\r\n"
DEVTYPE = "27"


def _attrs(**values: Any) -> str:
    return " ".join(f"{key}={quoteattr('--' if value is None else str(value))}" for key, value in values.items())


def _reply(cmd: str, body: str) -> bytes:
    return (f'<?xml version="1.0" encoding="UTF-8"?>\r\n<bts version="1.0">\r\n  <cmd>{cmd}_resp</cmd>\r\n{body}'
            "</bts>").encode("utf-8") + TERMINATION


def _pipeline(element: ElementTree.Element) -> str:
    return f"{element.get('devid')}-{element.get('subdevid')}-{element.get('chlid')}"


class _Lost(Exception):
    """启动已经生效，应答要丢掉：直接断开连接。"""


class BtsServer:
    def __init__(self, bts: FakeBts, host: str = "127.0.0.1", port: int = 0):
        self.bts = bts
        server = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                buffer = b""
                while True:
                    chunk = self.request.recv(4096)
                    if not chunk:
                        return
                    buffer += chunk
                    while TERMINATION in buffer:
                        message, buffer = buffer.split(TERMINATION, 1)
                        try:
                            self.request.sendall(server.answer(message.decode("utf-8")))
                        except _Lost:
                            return

        self.server = socketserver.ThreadingTCPServer((host, port), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def answer(self, text: str) -> bytes:
        root = ElementTree.fromstring(text)
        cmd = root.findtext("cmd") or ""
        items = list(root.find("list")) if root.find("list") is not None else []
        if cmd == "connect":
            return _reply(cmd, "  <result>ok</result>\r\n")
        if cmd == "getdevinfo":
            rows = "".join(
                f"    <channel {_attrs(ip='127.0.0.1', devtype=DEVTYPE, devid=d, subdevid=s, Channelid=c)}>true</channel>\r\n"
                for d, s, c in (pipeline.split("-") for pipeline in self.bts.pipelines))
            return _reply(cmd, f'  <serverip count="1">\r\n    <server ip="127.0.0.1" port="3306" />\r\n  </serverip>\r\n'
                               f'  <middle count="{len(self.bts.pipelines)}">\r\n{rows}</middle>\r\n')
        if cmd == "inquire":
            rows = self.bts.channels([_pipeline(item) for item in items])
            body = "".join(
                f"    <inquire {_attrs(dev=f'{DEVTYPE}-{pipeline}-0', cycle_id=row['cycle'], step_id=row['step'], step_type=row['step_type'], workstatus=row['workstatus'], barcode=row['barcode'], current=row['current'], voltage=row['voltage'], capacity=row['capacity'], energy=row['energy'], totaltime=0, relativetime=0, open_or_close=0, log_code=row['log_code'])} />\r\n"
                for pipeline, row in rows.items())
            return _reply(cmd, f'  <list count="{len(rows)}">\r\n{body}  </list>\r\n')
        if cmd in {"start", "stop"}:
            backup = root.find("list/backup")
            results = []
            for item in items:
                if item.tag != cmd:
                    continue
                pipeline = _pipeline(item)
                try:
                    if cmd == "start":
                        self.bts.start(pipeline, item.get("barcode") or "", (item.text or "").strip(),
                                       backup.get("backupdir", "") if backup is not None else "")
                    else:
                        self.bts.stop(pipeline)
                    outcome = "ok"
                except ReceiptLost as lost:
                    raise _Lost() from lost
                except (BtsRefused, Rejected):
                    outcome = "false"
                results.append(f"    <{cmd} {_attrs(ip='127.0.0.1', devtype=DEVTYPE, devid=item.get('devid'), subdevid=item.get('subdevid'), chlid=item.get('chlid'))}>{outcome}</{cmd}>\r\n")
            return _reply(cmd, f'  <list count="{len(results)}">\r\n{"".join(results)}  </list>\r\n')
        return _reply(cmd, "  <result>unsupported</result>\r\n")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5502)
    parser.add_argument("--channels", type=int, default=8, help="通道数，通道号 1-1-1 起")
    parser.add_argument("--run-seconds", type=float, default=30, help="一个测试跑多久")
    args = parser.parse_args(argv)
    server = BtsServer(FakeBts([f"1-1-{i}" for i in range(1, args.channels + 1)], run_seconds=args.run_seconds),
                       host=args.host, port=args.port)
    print(f"假 BTS 在 {args.host}:{server.port}，通道 1-1-1 … 1-1-{args.channels}")
    try:
        server.thread.join()
    except KeyboardInterrupt:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
