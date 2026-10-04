"""串口 / TCP 文本命令驱动（`line_command_v1`）。

面向「厂家给了一份命令手册」的仪器：RS232 / RS485（经串口服务器或本机串口）或 TCP 端口上一问一答的
文本协议，例如温控仪表、真空干燥箱、机械臂的仪表盘服务。驱动不猜厂家协议，命令、回复解析与状态映射
全部来自适配器配置：

- `transport`：`{"kind": "tcp", "host", "port"}`，或 `{"kind": "serial", "port": "/dev/ttyUSB0" |
  "rfc2217://host:port" | "socket://host:port", "baudrate", "bytesize", "parity", "stopbits"}`；
- `capabilities.<能力>.start`：启动这一步要依次发送的命令（`{"send": "SP {temp:.1f}", "expect": "^OK"}`），
  其中 `motion: true` 的那条（缺省是最后一条）才会让设备动作；
- `status`：查询命令 + 正则（命名组 `state`，可选 `detail`）+ 状态值到 idle/running/held/done/failed 的映射；
- `actuals`、`identity`、`ready`、`interlock`、`hold` / `resume` / `abort` / `acknowledge`：同样是命令 + 正则。
- `points`（只读写点位，可以不配 `capabilities` / `status`）：每个点一条查询命令 + 正则（命名组 `value`），
  `writable: true` 的点再配写命令 `write`（`{value}` 是要写的值，可带格式，如 `SP {value:.1f}`）：

  ```json
  "points": {"temp": {"send": "PV?", "pattern": "^(?P<value>[-\\d.]+)$", "unit": "℃"},
             "setpoint": {"send": "SP?", "pattern": "^(?P<value>[-\\d.]+)$", "writable": true, "min": 0, "max": 200,
                          "write": [{"send": "SP {value:.1f}", "expect": "^OK"}]}}
  ```

指令号、去重、重启后按原指令号查询由作业台账（`jobs.py`）负责。错误分类：
- 动作命令之前的命令被拒、没回复、回复不符合预期：启动命令还没发，设备没动 → `AdapterError`；
- 动作命令本身回复报错：设备拒绝启动 → `AdapterError`；动作命令没回复或回复看不懂 → 结果未知；
- 连不上设备 → `AdapterUnreachable`（与其他驱动一致，按结果未知处理）。
"""
from __future__ import annotations

from contextlib import contextmanager
import re
import socket
import time
from urllib.parse import urlparse

from ...core.config import settings
from ..base import AdapterError, AdapterIndeterminate, AdapterUnreachable
from ..jobs import BUILTINS, MappedJobAdapter, check_point_meta, placeholders, render

DRIVER = "line_command_v1"
MAX_LINE = 64 * 1024
LOCAL_SERIAL = re.compile(r"^(/dev/(tty[\w.-]+|serial/by-(id|path)/[\w.:+-]+)|COM\d{1,3})$")
STATE_NAMES = {"idle", "running", "held", "done", "failed"}


def _regex(value, label: str):
    if value in (None, ""):
        return None
    try:
        return re.compile(str(value))
    except re.error as exc:
        raise AdapterError(f"{label} 不是有效的正则：{exc}") from exc


class LineTransport:
    """一问一答的行协议通道。缺省每次操作开一次会话（连接 → 欢迎语 → 收发 → 关闭），不长期占着串口。"""

    def __init__(self, config: dict, *, encoding: str = "", write_terminator: str | None = None,
                 read_terminator: str | None = None):
        transport = config.get("transport") or {}
        if not isinstance(transport, dict):
            raise AdapterError("transport 必须是对象")
        self.kind = str(transport.get("kind") or "tcp")
        self.encoding = encoding or str(config.get("encoding") or "ascii")
        try:
            "x".encode(self.encoding)
        except LookupError as exc:
            raise AdapterError(f"encoding {self.encoding} 不受支持") from exc
        self.write_terminator = (config.get("write_terminator", "\r\n") if write_terminator is None
                                 else write_terminator).encode(self.encoding)
        self.read_terminator = (config.get("read_terminator", "\r\n") if read_terminator is None
                                else read_terminator).encode(self.encoding)
        if not self.read_terminator:
            raise AdapterError("read_terminator 不能为空：不知道一条回复在哪里结束")
        self.connect_timeout = self._positive(config, "connect_timeout_sec", 3.0)
        self.request_timeout = self._positive(config, "request_timeout_sec", 5.0)
        self.greeting = _regex(config.get("greeting"), "greeting")
        self.delay = float(config.get("inter_command_delay_ms") or 0) / 1000
        self.keep_open = bool(config.get("keep_open", False))
        self._channel = None
        if self.kind == "tcp":
            self.host = str(transport.get("host") or "")
            try:
                self.port = int(transport.get("port") or 0)
            except (TypeError, ValueError) as exc:
                raise AdapterError("transport.port 必须是整数") from exc
            if not self.host or not (0 < self.port < 65536):
                raise AdapterError("TCP 通道必须配置 transport.host 与 transport.port")
            if not settings.adapter_host_allowed(self.host):
                raise AdapterError(f"设备主机 {self.host} 不在 ILCS_ADAPTER_ALLOWED_HOSTS 白名单")
            self.label = f"{self.host}:{self.port}"
        elif self.kind == "serial":
            self.url = str(transport.get("port") or "")
            if "://" in self.url:
                parsed = urlparse(self.url)
                if parsed.scheme not in {"rfc2217", "socket"} or not parsed.hostname:
                    raise AdapterError("网络串口只支持 rfc2217://主机:端口 或 socket://主机:端口")
                if not settings.adapter_host_allowed(parsed.hostname):
                    raise AdapterError(f"串口服务器 {parsed.hostname} 不在 ILCS_ADAPTER_ALLOWED_HOSTS 白名单")
            elif not LOCAL_SERIAL.fullmatch(self.url):
                raise AdapterError("本机串口只允许 /dev/tty*、/dev/serial/by-id/*、/dev/serial/by-path/* 或 COMn")
            self.serial_options = {
                "baudrate": int(transport.get("baudrate") or 9600),
                "bytesize": int(transport.get("bytesize") or 8),
                "parity": str(transport.get("parity") or "N"),
                "stopbits": float(transport.get("stopbits") or 1),
                "xonxoff": bool(transport.get("xonxoff", False)),
                "rtscts": bool(transport.get("rtscts", False)),
            }
            if self.serial_options["parity"] not in {"N", "E", "O", "M", "S"}:
                raise AdapterError("parity 只能是 N / E / O / M / S")
            self.label = self.url
        else:
            raise AdapterError("transport.kind 只能是 tcp 或 serial")

    @staticmethod
    def _positive(config: dict, key: str, default: float) -> float:
        try:
            value = float(config.get(key, default))
        except (TypeError, ValueError) as exc:
            raise AdapterError(f"{key} 必须是正数") from exc
        if value <= 0 or value > 600:
            raise AdapterError(f"{key} 必须在 0–600 秒之间")
        return value

    # ---------- 底层通道 ----------

    def _open(self):
        try:
            if self.kind == "tcp":
                channel = _SocketChannel(socket.create_connection((self.host, self.port), timeout=self.connect_timeout))
            else:
                import serial

                channel = _SerialChannel(serial.serial_for_url(
                    self.url, timeout=self.request_timeout, write_timeout=self.request_timeout, **self.serial_options,
                ))
        except OSError as exc:
            raise AdapterUnreachable(f"设备 {self.label} 连不上：{exc.__class__.__name__}: {exc}") from exc
        except Exception as exc:  # pyserial 的 SerialException 等
            raise AdapterUnreachable(f"串口 {self.label} 打不开：{exc}") from exc
        channel.set_timeout(self.request_timeout)
        if self.greeting is not None:
            line = channel.read_line(self.read_terminator)
            text = line.decode(self.encoding, errors="replace") if line is not None else ""
            if line is None or not self.greeting.search(text):
                channel.close()
                raise AdapterUnreachable(f"设备 {self.label} 没有给出预期的欢迎语（收到 {text!r}）")
        return channel

    @contextmanager
    def session(self):
        if self.keep_open and self._channel is not None:
            channel = self._channel
        else:
            channel = self._open()
        failed = False
        try:
            yield _Session(self, channel)
        except BaseException:
            failed = True
            raise
        finally:
            if self.keep_open and not failed:
                self._channel = channel
            else:
                channel.close()
                if self._channel is channel:
                    self._channel = None

    def close(self) -> None:
        if self._channel is not None:
            self._channel.close()
            self._channel = None


class _Session:
    def __init__(self, transport: LineTransport, channel):
        self.transport = transport
        self.channel = channel

    def exchange(self, line: str, *, reply: bool = True, timeout: float | None = None) -> str | None:
        transport = self.transport
        if transport.delay:
            time.sleep(transport.delay)
        try:
            payload = line.encode(transport.encoding, errors="strict") + transport.write_terminator
        except UnicodeEncodeError as exc:
            raise AdapterError(f"命令 {line!r} 含 {transport.encoding} 编码不支持的字符，没有发出") from exc
        try:
            self.channel.write(payload)
        except Exception as exc:
            raise AdapterUnreachable(f"向 {transport.label} 发送 {line!r} 失败：{exc.__class__.__name__}") from exc
        if not reply:
            return None
        if timeout is not None:
            self.channel.set_timeout(timeout)
        try:
            raw = self.channel.read_line(transport.read_terminator)
        finally:
            if timeout is not None:
                self.channel.set_timeout(transport.request_timeout)
        if raw is None:
            raise AdapterUnreachable(f"{transport.label} 对 {line!r} 在超时内没有回复")
        return raw.decode(transport.encoding, errors="replace").strip()


class _SocketChannel:
    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.buffer = b""

    def set_timeout(self, seconds: float) -> None:
        self.sock.settimeout(seconds)

    def write(self, data: bytes) -> None:
        self.sock.sendall(data)

    def read_line(self, terminator: bytes) -> bytes | None:
        while terminator not in self.buffer:
            if len(self.buffer) > MAX_LINE:
                raise AdapterIndeterminate("设备回复超过 64 KiB 仍没有行结束符")
            try:
                chunk = self.sock.recv(4096)
            except (TimeoutError, socket.timeout):
                return None
            except OSError as exc:
                raise AdapterUnreachable(f"读取回复失败：{exc.__class__.__name__}") from exc
            if not chunk:
                return None  # 对端关闭：回复没到
            self.buffer += chunk
        line, _, self.buffer = self.buffer.partition(terminator)
        return line

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class _SerialChannel:
    def __init__(self, port):
        self.port = port

    def set_timeout(self, seconds: float) -> None:
        self.port.timeout = seconds

    def write(self, data: bytes) -> None:
        self.port.write(data)
        self.port.flush()

    def read_line(self, terminator: bytes) -> bytes | None:
        try:
            raw = self.port.read_until(terminator, MAX_LINE)
        except Exception as exc:
            raise AdapterUnreachable(f"读取串口失败：{exc}") from exc
        if not raw.endswith(terminator):
            return None
        return raw[: -len(terminator)]

    def close(self) -> None:
        try:
            self.port.close()
        except Exception:
            pass


def _steps(value, label: str) -> list[dict]:
    if value in (None, ""):
        return []
    if not isinstance(value, list) or not all(isinstance(step, dict) and step.get("send") for step in value):
        raise AdapterError(f"{label} 必须是命令列表：[{{\"send\": \"...\", \"expect\": \"正则\"}}]")
    for step in value:
        _regex(step.get("expect"), f"{label}.expect")
        _regex(step.get("reject"), f"{label}.reject")
    return value


def _query(value, label: str, group: str = "") -> dict | None:
    if value in (None, ""):
        return None
    if not isinstance(value, dict) or not value.get("send") or not value.get("pattern"):
        raise AdapterError(f"{label} 必须是 {{\"send\": 命令, \"pattern\": 正则}}")
    pattern = _regex(value["pattern"], f"{label}.pattern")
    if group and group not in pattern.groupindex:
        raise AdapterError(f"{label}.pattern 必须带命名组 (?P<{group}>...)")
    return {**value, "regex": pattern}


class LineCommandAdapter(MappedJobAdapter):
    DRIVER = DRIVER
    PROTOCOL = "串口 / TCP 命令"
    NOTE = "按命令手册配置的串口 / TCP 文本命令设备"

    def __init__(self, record, journal_key: str = ""):
        super().__init__(record, journal_key)
        self.transport = LineTransport(self.config)
        self.error_pattern = _regex(self.config.get("error_pattern"), "error_pattern")
        identity = self.config.get("identity") or []
        identity = [identity] if isinstance(identity, dict) else identity
        self.identity_queries = [_query(item, "identity") for item in identity]
        self.ready = _query(self.config.get("ready"), "ready", "value")
        self.interlock_check = _query(self.config.get("interlock"), "interlock", "value")
        raw_points = self.config.get("points") or {}
        if not isinstance(raw_points, dict):
            raise AdapterError("points 必须是 {点名: {send, pattern, …}}")
        self.points = {}
        for name, spec in raw_points.items():
            query = _query(spec, f"points.{name}", "value")
            check_point_meta(name, spec, set())
            if spec.get("writable") and not spec.get("write"):
                raise AdapterError(f"points.{name} 声明了可写，要配写命令 write：[{{\"send\": \"SP {{value}}\", \"expect\": \"^OK\"}}]")
            write = spec.get("write")
            query["write_steps"] = _steps([write] if isinstance(write, dict) else write, f"points.{name}.write")
            self.points[name] = query
        if not self.tasks and not self.points:
            raise AdapterError("line_command_v1 至少要配点表（points，读写点位）或能力映射（capabilities，参与自动流程）")
        # 只读写点位的设备可以不配状态；配了能力映射（要参与自动流程）就必须能查到设备状态
        self.status = _query(self.config.get("status"), "status", "state")
        if self.status is None and self.tasks:
            raise AdapterError("line_command_v1 配了能力映射就要配 status：查询命令、正则与状态映射")
        if self.status is not None:
            states = self.status.get("states") or {}
            if not isinstance(states, dict) or not states or not set(states.values()) <= STATE_NAMES:
                raise AdapterError("status.states 必须把设备状态值映射到 idle / running / held / done / failed")
        actuals = self.config.get("actuals") or []
        self.actual_queries = [_query(item, "actuals") for item in (actuals if isinstance(actuals, list) else [actuals])]
        self.controls = {name: _steps(self.config.get(name), name) for name in ("hold", "resume", "abort", "acknowledge")}
        for capability, spec in (self.config.get("capabilities") or {}).items():
            if not isinstance(spec, dict):
                raise AdapterError(f"capabilities.{capability} 必须是对象")
            if not _steps(spec.get("start"), f"capabilities.{capability}.start"):
                raise AdapterError(f"capabilities.{capability}.start 至少要有一条命令")

    def close(self) -> None:
        self.transport.close()

    # ---------- 命令收发 ----------

    def _check(self, step: dict, line: str, reply: str | None, stage: str) -> None:
        """stage：pre（启动命令之前）/ motion（启动命令）/ post（启动之后）/ control（保持、终止、复位）。"""
        if reply is None:
            return
        reject = _regex(step.get("reject"), "reject")
        refused = (reject is not None and reject.search(reply)) or (
            self.error_pattern is not None and self.error_pattern.search(reply))
        if refused:
            if stage == "post":
                raise AdapterIndeterminate(f"启动之后的命令 {line!r} 被设备拒绝：{reply}")
            # 启动前、启动命令本身、控制命令被拒：设备明确没有执行这一条
            raise AdapterError(f"设备拒绝 {line!r}：{reply}")
        expect = _regex(step.get("expect"), "expect")
        if expect is not None and not expect.search(reply):
            if stage == "pre":
                raise AdapterError(f"{line!r} 的回复 {reply!r} 不符合预期；启动命令还没发出，设备没有动作")
            raise AdapterIndeterminate(f"{line!r} 的回复 {reply!r} 不符合预期，无法确认设备是否已执行")

    def _run(self, steps: list[dict], values: dict, *, motion_index: int | None = None) -> list[str | None]:
        replies: list[str | None] = []
        with self.transport.session() as session:
            for index, step in enumerate(steps):
                line = render(str(step["send"]), values)
                if motion_index is None:
                    stage = "control"
                else:
                    stage = "pre" if index < motion_index else "motion" if index == motion_index else "post"
                try:
                    reply = session.exchange(line, reply=step.get("reply", True) is not False)
                except AdapterUnreachable as exc:
                    if stage == "pre":
                        raise AdapterError(f"{line!r} 没有回复（{exc}）；启动命令还没发出，设备没有动作") from exc
                    raise
                self._check(step, line, reply, stage)
                replies.append(reply)
                if step.get("wait_ms"):
                    time.sleep(float(step["wait_ms"]) / 1000)
        return replies

    def _ask(self, query: dict, session=None) -> re.Match:
        def ask(active):
            reply = active.exchange(str(query["send"]))
            match = query["regex"].search(reply or "")
            if match is None:
                if self.error_pattern is not None and self.error_pattern.search(reply or ""):
                    raise AdapterIndeterminate(f"设备对 {query['send']!r} 报错：{reply}")
                raise AdapterIndeterminate(f"设备对 {query['send']!r} 的回复 {reply!r} 与配置的格式不符")
            return match

        if session is not None:
            return ask(session)
        with self.transport.session() as active:
            return ask(active)

    # ---------- 钩子 ----------

    def accepted_params(self, spec: dict) -> set[str]:
        used = set()
        for step in spec.get("start") or []:
            used |= placeholders(str(step.get("send") or ""))
        return (used - BUILTINS) | set(spec.get("accept") or [])

    def read_identity(self) -> dict:
        identity: dict = {"vendor": self.config.get("vendor", ""), "model": self.config.get("model", "")}
        with self.transport.session() as session:
            for query in self.identity_queries:
                identity.update({k: v.strip() for k, v in self._ask(query, session).groupdict().items() if v})
            if self.ready is not None:
                value = self._ask(self.ready, session).group("value")
                identity["accepts_commands"] = value in (self.ready.get("ok") or [])
            if self.interlock_check is not None:
                value = self._ask(self.interlock_check, session).group("value")
                identity["interlock"] = value not in (self.interlock_check.get("ok") or [])
            if not self.identity_queries and self.ready is None and self.interlock_check is None:
                # 一条命令都不问就报在线是假在线（串口服务器连得上不等于仪表在回话）：问一次状态或第一个点
                self._ask(self.status or next(iter(self.points.values())), session)
        identity.setdefault("device_id", identity.get("serial", ""))
        return identity

    # ---------- 点位 ----------

    def point_specs(self) -> dict:
        return dict(self.points)

    def read_point_value(self, name: str):
        raw = self._ask(self.points[name]).group("value")
        try:
            return float(raw)
        except (TypeError, ValueError):
            return (raw or "").strip()

    def write_point_value(self, name: str, value) -> None:
        # 控制命令的规则：设备回报错 = 明确没执行（AdapterError）；没回复、回复不符合 expect = 结果未知
        self._run(self.points[name]["write_steps"], {"value": value})

    def precheck(self, spec: dict) -> None:
        if self.ready is None and self.interlock_check is None:
            return
        identity = self.read_identity()
        if identity.get("interlock"):
            raise AdapterError("设备联锁未解除（Interlocked），未发出启动命令")
        if identity.get("accepts_commands") is False:
            raise AdapterError("设备未就绪（未处于远程 / 自动模式），未发出启动命令")

    def check_start(self, spec: dict, values: dict) -> None:
        # 整套启动命令先渲染一遍：缺参数、格式不对在第一条发出之前就拒绝，不会只发出半套设定
        for step in spec.get("start") or []:
            render(str(step["send"]), values)

    def start_job(self, job: dict, spec: dict, values: dict) -> dict | None:
        steps = spec["start"]
        motion = next((index for index, step in enumerate(steps) if step.get("motion")), len(steps) - 1)
        replies = self._run(steps, values, motion_index=motion)
        result = spec.get("result")
        if not result:
            return None
        # 即时动作（如读码器 LON → 条码）：动作命令的回复就是结果，作业当场完成
        pattern = _regex(result.get("pattern"), "result.pattern")
        match = pattern.search(replies[motion] or "") if pattern is not None else None
        if match is None:
            raise AdapterIndeterminate(f"动作命令的回复 {replies[motion]!r} 与 result.pattern 不符，无法确认结果")
        values = {}
        for name, raw in match.groupdict().items():
            if raw is None:
                continue
            try:
                values[name] = float(raw)
            except ValueError:
                values[name] = raw.strip()
        return {"actuals": values}

    def read_status(self, job: dict) -> tuple[str, str]:
        match = self._ask(self.status)
        value = match.group("state")
        states = self.status["states"]
        state = states.get(value) or states.get(value.upper())
        if state is None:
            raise AdapterIndeterminate(f"设备状态值 {value!r} 没有在 status.states 里映射")
        detail = match.groupdict().get("detail") or ""
        if state == "failed" and detail:
            detail = (self.config.get("error_codes") or {}).get(detail.strip(), f"设备报警 {detail.strip()}")
        return state, detail

    def read_actuals(self, job: dict, spec: dict) -> dict:
        if job.get("actuals"):
            return dict(job["actuals"])  # 即时动作：结果随动作命令一起回来了，不再另外查询
        actuals = {}
        with self.transport.session() as session:
            for query in self.actual_queries:
                for name, raw in self._ask(query, session).groupdict().items():
                    if raw is None:
                        continue
                    try:
                        actuals[name] = float(raw)
                    except ValueError:
                        actuals[name] = raw.strip()  # 文本结果（批号、条码）原样进回执
        return actuals

    def _control_steps(self, name: str, job: dict | None) -> None:
        steps = self.controls.get(name) or []
        if not steps:
            raise AdapterError(f"映射里没有配置 {name} 命令")
        values = {"command_id": (job or {}).get("id", ""), "program": (job or {}).get("program", "")}
        self._run(steps, values)

    def hold_job(self, job: dict) -> None:
        self._control_steps("hold", job)

    def resume_job(self, job: dict) -> None:
        self._control_steps("resume", job)

    def abort_job(self, job: dict | None) -> None:
        self._control_steps("abort", job)

    def acknowledge(self) -> bool:
        if not self.controls.get("acknowledge"):
            return False
        self._control_steps("acknowledge", None)
        return True
