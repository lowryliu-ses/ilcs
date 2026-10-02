"""环境读数连接器：按周期读手套箱（及温湿度、露点等）传感器，经 ILCS 的 `POST /api/runtime/environment` 上报。

ILCS 侧的步骤可以声明环境要求（如 `{"metric": "h2o_ppm", "max": 1, "zone": "配液段手套箱"}`）：开跑检查与每步投递前
按区域 × 指标的最新读数核对，没有读数或读数超过 30 min 没更新都会挡住投递。本连接器就是把读数送过去的那一端。

读法按点配置，和厂家无关——手套箱控制器（MBraun、米开罗那、Vigor、Etelux、Jacomex……）一般给 Modbus TCP、OPC UA 或
串口 / 网口文本命令，地址或命令按厂家手册 / 集成商填：

- `modbus`：`{"table": "input"|"holding", "address": 0 基地址, "type": "float32"|"uint16"|"int16"|"uint32"|"int32",
  "word_order": "big"|"little", "scale": 0.1, "offset": 0}`；
- `opcua`：`{"node": "ns=2;s=Box1.O2"}`（读节点的值，有源时间戳就用它）；
- `line`：`{"send": "O2?", "pattern": "^(?P<value>[-\\d.Ee+]+)"}`（发一行、按正则取数）。

每个点写区域 `zone`、指标 `metric`、单位 `unit`，可选 `valid: [下限, 上限]`：读出来超出合理范围（传感器故障码、
负的 ppm）就丢掉并记日志，**不上报**——宁可让 ILCS 判读数过期挡住投递，也不报一个错的数让它放行。
读不到的点同样不上报。按区域分批上报：一个区域没授权（服务身份的 `environment_zones`）不影响别的区域。

    python devices/connectors/environment/poller.py --config environment.json          # 常驻，按 poll_sec 轮询
    python devices/connectors/environment/poller.py --config environment.json --once   # 读一轮就退出（排障用）
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import logging
import math
import os
from pathlib import Path
import re
import signal
import socket
import struct
import threading
import time
import urllib.error
import urllib.request
from typing import Any

log = logging.getLogger("ilcs.environment")
TYPES = {"uint16": (1, ">H"), "int16": (1, ">h"), "uint32": (2, ">I"), "int32": (2, ">i"), "float32": (2, ">f")}


class SourceError(Exception):
    """这一路传感器这一轮读不到（连不上、超时、应答不对）。"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def decode_registers(registers: list[int], kind: str, word_order: str = "big") -> float:
    """寄存器（16 位一个）→ 数。32 位的值两个寄存器拼起来：big 高位字在前（Modbus 惯例），little 低位字在前。"""
    width, fmt = TYPES[kind]
    if len(registers) != width:
        raise SourceError(f"{kind} 要 {width} 个寄存器，读到 {len(registers)} 个")
    words = list(registers) if word_order == "big" else list(reversed(registers))
    return float(struct.unpack(fmt, b"".join(struct.pack(">H", word & 0xFFFF) for word in words))[0])


class Point:
    def __init__(self, raw: dict[str, Any], source: str):
        self.zone = str(raw.get("zone") or "").strip()
        self.metric = str(raw.get("metric") or "").strip()
        if not self.zone or not self.metric:
            raise ValueError(f"{source} 的读数点要写 zone 与 metric")
        if len(self.zone) > 64 or len(self.metric) > 32:
            raise ValueError(f"{source} 的 {self.metric}：zone 最长 64 个字、metric 最长 32 个字（ILCS 的限制）")
        self.unit = str(raw.get("unit") or "")
        self.scale = float(raw.get("scale", 1))
        self.offset = float(raw.get("offset", 0))
        valid = raw.get("valid")
        if valid is not None and not (isinstance(valid, list) and len(valid) == 2 and valid[0] <= valid[1]):
            raise ValueError(f"{source} 的 {self.metric}：valid 要写成 [下限, 上限]")
        self.valid = tuple(valid) if valid is not None else None
        self.raw = raw

    def value(self, number: float) -> float:
        return number * self.scale + self.offset


class ModbusSource:
    def __init__(self, raw: dict[str, Any]):
        self.name = str(raw.get("name") or "modbus")
        self.host, self.port = str(raw.get("host") or ""), int(raw.get("port") or 502)
        if not self.host:
            raise ValueError(f"{self.name} 要写 host")
        self.unit = int(raw.get("unit", 1))
        self.timeout = float(raw.get("timeout_sec") or 3)
        self.points = [Point(item, self.name) for item in raw.get("readings") or []]
        for point in self.points:
            if point.raw.get("table", "input") not in {"input", "holding"} or point.raw.get("type", "float32") not in TYPES:
                raise ValueError(f"{self.name} 的 {point.metric}：table 只能是 input / holding，type 只能是 {', '.join(TYPES)}")
            if not isinstance(point.raw.get("address"), int) or point.raw["address"] < 0:
                raise ValueError(f"{self.name} 的 {point.metric}：address 要写 0 基的寄存器地址")
        self.client = None

    def _client(self):
        if self.client is None:
            from pymodbus.client import ModbusTcpClient

            client = ModbusTcpClient(self.host, port=self.port, timeout=self.timeout, retries=1)
            if not client.connect():
                raise SourceError(f"连不上 Modbus 设备 {self.host}:{self.port}")
            self.client = client
        return self.client

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
            self.client = None

    def read(self) -> list[tuple[Point, float | None, datetime | None, str]]:
        rows = []
        try:
            client = self._client()
        except SourceError as exc:
            return [(point, None, None, str(exc)) for point in self.points]
        for point in self.points:
            kind = point.raw.get("type", "float32")
            width = TYPES[kind][0]
            reader = client.read_input_registers if point.raw.get("table", "input") == "input" else client.read_holding_registers
            try:
                response = reader(point.raw["address"], count=width, slave=self.unit)
            except Exception as exc:  # noqa: BLE001  连接断了：这一轮这一路都算读不到，下轮重连
                self.close()
                rows.extend((rest, None, None, f"Modbus 读出错：{exc}") for rest in self.points[len(rows):])
                return rows
            if response.isError():
                rows.append((point, None, None, f"Modbus 异常应答：{response}"))
                continue
            try:
                number = decode_registers(list(response.registers), kind, point.raw.get("word_order", "big"))
            except SourceError as exc:
                rows.append((point, None, None, str(exc)))
                continue
            rows.append((point, point.value(number), None, ""))
        return rows


class OpcUaSource:
    def __init__(self, raw: dict[str, Any]):
        self.name = str(raw.get("name") or "opcua")
        self.endpoint = str(raw.get("endpoint") or "")
        if not self.endpoint.startswith("opc.tcp://"):
            raise ValueError(f"{self.name} 的 endpoint 要写 opc.tcp://主机:端口/路径")
        self.username = str(raw.get("username") or "")
        self.password_file = str(raw.get("password_file") or "")
        self.timeout = float(raw.get("timeout_sec") or 5)
        self.points = [Point(item, self.name) for item in raw.get("readings") or []]
        for point in self.points:
            if not str(point.raw.get("node") or "").strip():
                raise ValueError(f"{self.name} 的 {point.metric} 要写 node（节点 ID）")

    def close(self) -> None:
        pass

    def read(self) -> list[tuple[Point, float | None, datetime | None, str]]:
        from asyncua.sync import Client

        client = Client(self.endpoint, timeout=self.timeout)
        if self.username:
            client.set_user(self.username)
            client.set_password(Path(self.password_file).read_text(encoding="utf-8").strip() if self.password_file else "")
        try:
            client.connect()
        except Exception as exc:  # noqa: BLE001
            return [(point, None, None, f"连不上 OPC UA {self.endpoint}：{exc}") for point in self.points]
        rows = []
        try:
            for point in self.points:
                try:
                    data = client.get_node(point.raw["node"]).read_data_value()
                    stamp = data.SourceTimestamp
                    if stamp is not None and stamp.tzinfo is None:
                        stamp = stamp.replace(tzinfo=timezone.utc)
                    rows.append((point, point.value(float(data.Value.Value)), stamp, ""))
                except Exception as exc:  # noqa: BLE001  一个节点读不了不影响别的节点
                    rows.append((point, None, None, f"OPC UA 节点 {point.raw['node']} 读不了：{exc}"))
        finally:
            try:
                client.disconnect()
            except Exception:  # noqa: BLE001
                pass
        return rows


class LineSource:
    """串口 / 网口文本命令：发一行、按正则取数（正则里要有命名组 value）。"""

    def __init__(self, raw: dict[str, Any]):
        self.name = str(raw.get("name") or "line")
        self.link = raw.get("link") or {}
        if self.link.get("kind", "tcp") not in {"tcp", "serial"}:
            raise ValueError(f"{self.name} 的 link.kind 只能是 tcp 或 serial")
        self.terminator = str(raw.get("write_terminator", "\r\n")).encode()
        self.read_terminator = str(raw.get("read_terminator", "\n")).encode()
        self.timeout = float(raw.get("timeout_sec") or 3)
        self.points = [Point(item, self.name) for item in raw.get("readings") or []]
        for point in self.points:
            try:
                pattern = re.compile(str(point.raw.get("pattern") or ""))
            except re.error as exc:
                raise ValueError(f"{self.name} 的 {point.metric}：pattern 不是合法的正则：{exc}") from exc
            if "value" not in pattern.groupindex or not point.raw.get("send"):
                raise ValueError(f"{self.name} 的 {point.metric} 要写 send 与带命名组 (?P<value>...) 的 pattern")
            point.pattern = pattern

    def close(self) -> None:
        pass

    def _open(self):
        if self.link.get("kind", "tcp") == "tcp":
            conn = socket.create_connection((self.link["host"], int(self.link["port"])), timeout=self.timeout)
            return conn, conn.sendall, lambda: conn.recv(1024)
        import serial

        conn = serial.serial_for_url(self.link["port"], baudrate=int(self.link.get("baudrate") or 9600),
                                     bytesize=int(self.link.get("bytesize") or 8), parity=str(self.link.get("parity") or "N"),
                                     stopbits=float(self.link.get("stopbits") or 1), timeout=self.timeout)
        return conn, conn.write, lambda: conn.read(conn.in_waiting or 1)

    def read(self) -> list[tuple[Point, float | None, datetime | None, str]]:
        try:
            conn, write, recv = self._open()
        except (OSError, ValueError) as exc:
            return [(point, None, None, f"连不上 {self.name}：{exc}") for point in self.points]
        rows = []
        try:
            for point in self.points:
                try:
                    write(str(point.raw["send"]).encode() + self.terminator)
                    buffer, deadline = b"", time.monotonic() + self.timeout
                    while self.read_terminator not in buffer:
                        if time.monotonic() > deadline:
                            raise SourceError("应答超时")
                        chunk = recv()
                        if not chunk and self.link.get("kind", "tcp") == "tcp":
                            raise SourceError("对端关了连接")
                        buffer += chunk
                    line = buffer.split(self.read_terminator, 1)[0].decode(errors="replace").strip()
                    match = point.pattern.search(line)
                    if not match:
                        rows.append((point, None, None, f"应答 {line!r} 对不上 pattern"))
                        continue
                    rows.append((point, point.value(float(match.group("value"))), None, ""))
                except (OSError, SourceError, ValueError) as exc:
                    rows.append((point, None, None, f"{point.raw['send']}：{exc}"))
        finally:
            conn.close()
        return rows


KINDS = {"modbus": ModbusSource, "opcua": OpcUaSource, "line": LineSource}


class UrllibHttp:
    def __init__(self, base_url: str, headers: dict, timeout: float = 15):
        self.base_url = base_url.rstrip("/")
        self.headers = headers
        self.timeout = timeout

    def post_json(self, path: str, payload: dict) -> tuple[int, Any]:
        request = urllib.request.Request(self.base_url + path, data=json.dumps(payload, ensure_ascii=False).encode(),
                                         method="POST", headers={**self.headers, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, json.loads(response.read() or b"null")
        except urllib.error.HTTPError as error:
            body = error.read()
            try:
                return error.code, json.loads(body)
            except ValueError:
                return error.code, body.decode(errors="replace")
        except (urllib.error.URLError, OSError) as exc:
            return 0, str(exc)


class Poller:
    def __init__(self, config: dict[str, Any], http=None):
        self.poll = float(config.get("poll_sec", 10))
        self.sources = []
        for raw in config.get("sources") or []:
            kind = str(raw.get("kind") or "")
            if kind not in KINDS:
                raise ValueError(f"读数源 kind 只能是 {', '.join(KINDS)}，不是 {kind!r}")
            self.sources.append(KINDS[kind](raw))
        if not any(source.points for source in self.sources):
            raise ValueError("至少要配置一个读数点（sources[].readings）")
        if http is None:
            secret = Path(config["secret_file"]).read_text(encoding="utf-8").strip()
            http = UrllibHttp(config["ilcs_url"], {"X-Service-Source": config["source"], "X-Service-Secret": secret})
        self.http = http

    def run_once(self) -> dict[str, Any]:
        """读一轮、按区域上报。返回 {posted, dropped, unreadable, rejected}（各自是读数点的说明）。"""
        outcome: dict[str, list] = {"posted": [], "dropped": [], "unreadable": [], "rejected": []}
        by_zone: dict[str, list[dict]] = {}
        polled_at = _now()
        for source in self.sources:
            for point, value, stamp, problem in source.read():
                label = f"{point.zone}/{point.metric}"
                if value is None:
                    outcome["unreadable"].append(f"{label}：{problem}")
                    continue
                if not math.isfinite(value) or (point.valid and not point.valid[0] <= value <= point.valid[1]):
                    outcome["dropped"].append(f"{label} = {value:g} 不在合理范围 {point.valid}（不上报）")
                    continue
                by_zone.setdefault(point.zone, []).append({
                    "zone": point.zone, "metric": point.metric, "value": round(value, 6), "unit": point.unit,
                    "measured_at": (stamp or polled_at).isoformat(timespec="seconds"),
                    "note": f"连接器 {source.name}",
                })
        for zone, readings in by_zone.items():
            status, body = self.http.post_json("/api/runtime/environment", {"readings": readings})
            labels = [f"{zone}/{row['metric']}" for row in readings]
            if 200 <= status < 300:
                outcome["posted"].extend(labels)
            else:
                detail = body.get("detail") if isinstance(body, dict) else body
                outcome["rejected"].extend(f"{label}：HTTP {status} {detail}" for label in labels)
        for key in ("dropped", "unreadable", "rejected"):
            for message in outcome[key]:
                log.warning("%s", message)
        return outcome

    def serve(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                outcome = self.run_once()
                log.info("本轮上报 %d 个读数", len(outcome["posted"]))
            except Exception:  # 一轮出错不能让连接器退出
                log.exception("读传感器 / 上报失败")
            stop.wait(self.poll)
        for source in self.sources:
            source.close()


def main(argv=None) -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=os.environ.get("ENVIRONMENT_CONFIG", "environment.json"))
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    poller = Poller(json.loads(Path(args.config).read_text(encoding="utf-8")))
    if args.once:
        print(json.dumps(poller.run_once(), ensure_ascii=False, indent=1))
        return 0
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    poller.serve(stop)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
