#!/usr/bin/env python
"""假 MethodSCRIPT 仪器：TCP 上说 PalmSens 的通讯协议，真实接口（driver/palmsens.py）照常经 TCP 连它——
模拟走的是和真机同一套协议代码，只有最底下那根线换成了 TCP。

- 通讯命令：`t`（两行固件版本）、`i`、`v`、`l` / `e` + 脚本 + 空行（收完做语法检查，出错回 `l!XXXX: Line L, Col C`）、
  `r`（先回 `r`，然后脚本输出，最后空行）、`Z`（脚本在跑：输出流里回 `Z`，测量循环收尾、执行 `on_finished:`；
  没在跑：`Z!0006`）、`Y`、`h` / `H`；不认识的命令回 `<首字母>!0003`，脚本在跑时用了只能空闲时用的命令回 `!0006`；
- 脚本：认这个网关会写的那些命令（var、set_pgstat_*、set_range*、set_autoranging、set_max_bandwidth、set_e、
  cell_on / cell_off、timer_*、meas、meas_loop_ocp / lsv / cv（nscans）/ ca / eis、pck_*、if / elseif / else / endif、
  loop / endloop / breakloop、on_finished:、send_string、abort），按手册的写法核对参数（数的写法、变量要先声明、
  测量循环不能嵌套）；运行时也查：开路电位要先 cell_off（!0014）、扫描 / 阻抗要先 cell_on（!4027）、EIS 要高速模式
  （!0023）、电位 / 频率 / 振幅超出这台仪器的范围（!000F / !0011 / !0012）；
- 数据：电池按 `cell_model.py`，数据包照手册编码（值挑最细的 SI 前缀，电流带状态位与量程号元数据），
  按测量本身的节奏出点（`time_scale` 倍，测试里调小）；电流按自动量程的上下限选量程，超出量程置过载位、读数削顶；
- 测试用的开关：`fail_after`（出 N 个数据包后报 !0032 电池严重过载）、`drop_after`（出 N 个数据包后断开连接，
  脚本照跑、输出没人收）、`mute`（什么都不回）、`abort_delay`（收到 Z 之后再拖多少秒才停）。

    # 现场没有仪器时，起一台假仪器，再让网关（不带 --simulate）用 {"kind": "tcp", "host": "127.0.0.1", "port": 4100} 连它
    python simulator/fake_methodscript.py --port 4100
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import math
from pathlib import Path
import re
import socketserver
import sys
import threading
from typing import Any

if __package__ in (None, ""):  # 单独运行：把模块目录放进路径
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from driver import methodscript as ms  # noqa: E402

try:
    from .cell_model import CellModel  # noqa: E402
except ImportError:  # 单独运行
    from simulator.cell_model import CellModel  # noqa: E402

# EmStat4 的电流量程（手册表 34 / 36）：量程号 → 标称值；能测到标称值的约 3 倍（过载 2.92 倍、过载预警 2.46 倍、欠载 0.123 倍）
RANGES = ((0x03, 1e-9), (0x06, 10e-9), (0x09, 100e-9), (0x0C, 1e-6), (0x0F, 10e-6), (0x12, 100e-6), (0x15, 1e-3),
          (0x18, 10e-3), (0x1B, 100e-3))
OPERATORS = {"==": lambda a, b: a == b, "!=": lambda a, b: a != b, ">": lambda a, b: a > b, "<": lambda a, b: a < b,
             ">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b}
SPEC: dict[str, tuple[str, ...]] = {
    "var": ("decl",), "store_var": ("out", "lit", "vt"), "copy_var": ("name", "out"), "add_var": ("out", "val"),
    "sub_var": ("out", "val"), "set_pgstat_chan": ("uint",), "set_pgstat_mode": ("uint",),
    "set_max_bandwidth": ("val",), "set_range": ("vt", "val"), "set_range_minmax": ("vt", "val", "val"),
    "set_autoranging": ("vt", "val", "val"), "set_e": ("val",), "cell_on": (), "cell_off": (), "timer_start": (),
    "timer_get": ("out",), "wait": ("val",), "meas": ("val", "out", "vt"), "meas_loop_ocp": ("out", "val", "val"),
    "meas_loop_lsv": ("out", "out", "val", "val", "val", "val"),
    "meas_loop_cv": ("out", "out", "val", "val", "val", "val", "val"), "meas_loop_ca": ("out", "out", "val", "val", "val"),
    "meas_loop_eis": ("out", "out", "out", "val", "val", "val", "val", "val"), "pck_start": (), "pck_add": ("name",),
    "pck_end": (), "if": ("val", "op", "val"), "elseif": ("val", "op", "val"), "else": (), "endif": (),
    "loop": ("val", "op", "val"), "endloop": (), "breakloop": (), "send_string": ("string",), "abort": (),
}
OPTIONS = {"meas_loop_cv": {"nscans"}}
TAG = "on_finished:"
LINE_LIMIT = 256


class ScriptError(Exception):
    def __init__(self, code: int, line: int, column: int | None = None):
        super().__init__(f"!{code:04X}")
        self.code, self.line, self.column = code, line, column


@dataclass
class Command:
    line: int                       # 脚本里的行号（1 起，注释行也算）
    name: str
    args: list[str]
    options: dict[str, list[str]] = field(default_factory=dict)
    match: int | None = None        # 块的结束（endloop / 下一个 elseif、else、endif）


@dataclass
class Program:
    commands: list[Command]
    tag: int | None                 # on_finished: 后面第一条命令的下标


def _column(raw: str, token: str) -> int:
    position = raw.find(token)
    return position + 1 if position >= 0 else 1


def parse_script(lines: list[str]) -> Program:
    """收到的脚本 → 命令表。写法不对抛 ScriptError（带行号、列号）。"""
    commands: list[Command] = []
    declared: set[str] = set()
    stack: list[tuple[str, int]] = []   # (块类型, 命令下标)
    tag = None
    for number, raw in enumerate(lines, start=1):
        if len(raw) + 1 > LINE_LIMIT:
            raise ScriptError(0x0008, number, 1)
        text = raw.split("#", 1)[0].strip() if not raw.strip().startswith("send_string") else raw.strip()
        if not text:
            continue
        if text == TAG:
            if tag is not None or stack:
                raise ScriptError(0x400C, number, 1)
            tag = len(commands)
            continue
        if text.startswith("send_string"):
            rest = text[len("send_string"):].strip()
            if not (len(rest) >= 2 and rest[0] == '"' and rest[-1] == '"'):
                raise ScriptError(0x4004, number, _column(raw, rest or "send_string"))
            commands.append(Command(number, "send_string", [rest[1:-1]]))
            continue
        tokens = text.split()
        name, args = tokens[0], tokens[1:]
        if name not in SPEC:
            raise ScriptError(0x4001, number, 1)
        options: dict[str, list[str]] = {}
        while args and re.fullmatch(r"[a-z_]+\(.*\)", args[-1]):
            option = args.pop()
            key, inner = option[:option.index("(")], option[option.index("(") + 1:-1]
            if key not in OPTIONS.get(name, set()):
                raise ScriptError(0x4008, number, _column(raw, option))
            options[key] = inner.split()
        spec = SPEC[name]
        if len(args) > len(spec):
            raise ScriptError(0x420A, number, _column(raw, args[len(spec)]))
        if len(args) < len(spec):
            raise ScriptError(0x4004, number, len(raw.rstrip()) + 1)
        for kind, arg in zip(spec, args):
            column = _column(raw, arg)
            if kind == "decl":
                if not ms.identifier(arg):
                    raise ScriptError(0x402B, number, column)
                if arg in declared:
                    raise ScriptError(0x4026, number, column)
                declared.add(arg)
            elif kind in {"out", "name"}:
                if not ms.identifier(arg):
                    raise ScriptError(0x4208, number, column)
                if arg not in declared:
                    raise ScriptError(0x420B, number, column)
            elif kind == "val":
                if ms.identifier(arg):
                    if arg not in declared:
                        raise ScriptError(0x420B, number, column)
                else:
                    try:
                        ms.parse_literal(arg)
                    except ValueError:
                        raise ScriptError(0x4004, number, column) from None
            elif kind == "lit":
                try:
                    ms.parse_literal(arg)
                except ValueError:
                    raise ScriptError(0x4004, number, column) from None
            elif kind == "vt":
                if arg not in ms.VAR_TYPES:
                    raise ScriptError(0x0002, number, column)
            elif kind == "uint":
                try:
                    value, _ = ms.parse_literal(arg if arg.endswith("i") else arg + "i")
                except ValueError:
                    raise ScriptError(0x4004, number, column) from None
                if value < 0:
                    raise ScriptError(0x4200, number, column)
            elif kind == "op" and arg not in OPERATORS:
                raise ScriptError(0x4004, number, column)
        if name == "meas_loop_cv" and "nscans" in options:
            scans = options["nscans"]
            if len(scans) != 1 or not scans[0].rstrip("i").isdigit() or not 1 <= int(scans[0].rstrip("i")) <= 9999:
                raise ScriptError(0x4205, number, _column(raw, "nscans"))
        index = len(commands)
        commands.append(Command(number, name, args, options))
        if name.startswith("meas_loop_"):
            if any(kind == "meas" for kind, _ in stack):
                raise ScriptError(0x400B, number, 1)
            stack.append(("meas", index))
        elif name == "loop":
            stack.append(("loop", index))
        elif name == "endloop":
            if not stack or stack[-1][0] not in {"meas", "loop"}:
                raise ScriptError(0x400E, number, 1)
            commands[stack.pop()[1]].match = index
        elif name == "if":
            stack.append(("if", index))
        elif name in {"elseif", "else"}:
            if not stack or stack[-1][0] != "if":
                raise ScriptError(0x400E, number, 1)
            commands[stack.pop()[1]].match = index
            stack.append(("if", index))
        elif name == "endif":
            if not stack or stack[-1][0] != "if":
                raise ScriptError(0x400E, number, 1)
            commands[stack.pop()[1]].match = index
        elif name == "breakloop" and not any(kind in {"meas", "loop"} for kind, _ in stack):
            raise ScriptError(0x400C, number, 1)
    if stack:
        raise ScriptError(0x4018, len(lines), 1)
    return Program(commands, tag)


class RuntimeFault(Exception):
    """脚本运行时出错（仪器回 `!XXXX: Line L`，不执行 on_finished）。"""

    def __init__(self, code: int, line: int):
        super().__init__(f"!{code:04X}: Line {line}")
        self.code, self.line = code, line


class _Break(Exception):
    pass


class _Abort(Exception):
    pass


class FakeInstrument:
    """一台假 MethodSCRIPT 仪器（一个通道）。同一时刻只认一个连接（像串口）：新连接顶掉旧连接。"""

    def __init__(self, cell: CellModel | None = None, *, device_type: str = "es4_hr",
                 serial: str = "ILCS-SIMULATOR-ES4HR-01", version: str = "1400", time_scale: float = 1.0):
        if device_type not in ms.DEVICE_TYPES:
            raise ValueError(f"假仪器不认设备类型 {device_type}")
        self.cell = cell or CellModel()
        self.device_type, self.serial, self.version = device_type, serial, version
        self.time_scale = time_scale
        self.lock = threading.RLock()
        self.send_lock = threading.Lock()
        self.connection: Any = None
        self.loading: list[str] | None = None
        self.load_command = "l"
        self.program: Program | None = None
        self.script: list[str] = []
        self.running = False
        self.thread: threading.Thread | None = None
        self.abort_event = threading.Event()
        self.loop_break = threading.Event()
        self.resumed = threading.Event()
        self.resumed.set()
        # 仪器状态
        self.cell_on = False
        self.mode = 0
        self.clock = 0.0        # 仪器的模拟时钟（秒）：测量节奏按它走，出点再按 time_scale 等实际时间
        self.timer0 = 0.0
        self.autorange = (1e-9, 1e-2)
        self.vars: dict[str, list[Any]] = {}
        self.package: list[tuple[str, float, int, int | None]] | None = None
        # 测试用的开关与记录
        self.fail_after: int | None = None
        self.drop_after: int | None = None
        self.mute = False
        self.abort_delay = 0.0
        self.packages = 0
        self.runs: list[dict[str, Any]] = []
        self.received: list[str] = []

    # ---------- 连接 ----------

    def attach(self, connection: Any) -> None:
        with self.send_lock:
            old, self.connection = self.connection, connection
        if old is not None and old is not connection:
            try:
                old.close()
            except OSError:
                pass
        if self.device_type == "espico":
            self._raw(b"\x11")  # EmStat Pico 上电 / 连上时可能发一个 XON

    def detach(self, connection: Any) -> None:
        with self.send_lock:
            if self.connection is connection:
                self.connection = None

    def drop(self) -> None:
        """断开当前连接（仪器照跑，输出没人收）。"""
        with self.send_lock:
            connection, self.connection = self.connection, None
        if connection is not None:
            try:
                connection.shutdown(2)
            except OSError:
                pass
            try:
                connection.close()
            except OSError:
                pass

    def _raw(self, data: bytes) -> None:
        with self.send_lock:
            connection = self.connection
            if connection is None or self.mute:
                return
            try:
                connection.sendall(data)
            except OSError:
                self.connection = None

    def send(self, text: str) -> None:
        self._raw(text.encode("ascii") + b"\n")

    # ---------- 通讯命令 ----------

    def receive(self, line: str) -> None:
        line = line.replace("\r", "")
        with self.lock:
            self.received.append(line)
            if self.mute:
                return
            if self.loading is not None:
                if line.strip() == "":
                    self._loaded()
                else:
                    self.loading.append(line)
                return
            command = line.strip()
            if not command:
                return
            if self.running:
                self._script_mode(command)
                return
            if command == "t":
                self._firmware()
            elif command == "i":
                self.send("i" + self.serial)
            elif command == "v":
                self.send("v01.08.00")
            elif command in {"l", "e"}:
                self.loading, self.load_command = [], command
            elif command == "r":
                if self.program is None:
                    self.send("r!000C")
                else:
                    self.send("r")
                    self._start()
            elif command == "Z":
                self.send("Z!0006")
            elif command in {"Y", "h", "H", "R"}:
                self.send(f"{command}!0006")
            else:
                self.send(f"{command[0]}!0003")

    def _script_mode(self, command: str) -> None:
        if command == "Z":
            self.send("Z")
            if self.abort_delay:
                threading.Timer(self.abort_delay, self.abort_event.set).start()  # 测试用：停得慢（实际秒数）
            else:
                self.abort_event.set()
            self.resumed.set()
        elif command == "Y":
            self.send("Y")
            self.loop_break.set()
        elif command == "h":
            self.send("h")
            self.resumed.clear()
        elif command == "H":
            self.send("H")
            self.resumed.set()
        elif command == "R":
            self.send("R")
        elif command == "t":
            self._firmware()
        else:
            self.send(f"{command[0]}!0006")

    def _firmware(self) -> None:
        self.send(f"t{self.device_type}{self.version}#Oct  1 2026 12:00:00")
        self.send("R*")

    def _loaded(self) -> None:
        lines, command, self.loading = self.loading or [], self.load_command, None
        try:
            program = parse_script(lines)
        except ScriptError as error:
            self.program = None
            self.send(f"{command}!{error.code:04X}: Line {error.line}, Col {error.column or 1}")
            return
        self.program, self.script = program, lines
        self.send(command)
        if command == "e":
            self._start()

    # ---------- 运行脚本 ----------

    def _start(self) -> None:
        self.running = True
        self.abort_event.clear()
        self.loop_break.clear()
        self.resumed.set()
        self.vars, self.package, self.packages = {}, None, 0
        self.thread = threading.Thread(target=self._execute, args=(self.program,), daemon=True, name="fake-mscript")
        self.thread.start()

    def _execute(self, program: Program) -> None:
        record: dict[str, Any] = {"techniques": [], "aborted": False, "error": None}
        self.runs.append(record)
        self.record = record
        ending = [""]
        try:
            try:
                end = program.tag if program.tag is not None else len(program.commands)
                self._block(program, 0, end)
            except _Abort:
                record["aborted"] = True
            if program.tag is not None:
                self.abort_event.clear()  # on_finished 之后的命令终止不了
                self._block(program, program.tag, len(program.commands), finishing=True)
        except RuntimeFault as fault:
            record["error"] = fault.code
            # 通讯协议文档的例子：出错之后还有一个空行；on_finished 不执行（电池还开着）
            ending = [f"!{fault.code:04X}: Line {fault.line}", ""]
        except Exception as exc:  # noqa: BLE001  假仪器自己的错：照样结束脚本，别让测试挂住
            record["error"] = repr(exc)
            ending = ["!4012: Line 1", ""]
        finally:
            self.running = False  # 先回到空闲、再发结尾的空行：主机收到空行马上发下一条命令不会撞上「还在跑」
        for text in ending:
            self.send(text)

    def _value(self, token: str) -> float:
        if ms.identifier(token):
            return self.vars.get(token, [0.0, "aa", 0, None])[0]
        return ms.parse_literal(token)[0]

    def _set(self, name: str, value: float, kind: str, status: int = 0, current_range: int | None = None) -> None:
        self.vars[name] = [value, kind, status, current_range]

    def _limits(self, line: int, *values: float) -> None:
        low, high, _ = ms.DEVICE_TYPES[self.device_type]["modes"].get(self.mode, (-1e9, 1e9, 0))
        if any(not low - 1e-9 <= value <= high + 1e-9 for value in values):
            raise RuntimeFault(0x000F, line)

    def _check_abort(self) -> None:
        if self.abort_event.is_set():
            raise _Abort()

    def _block(self, program: Program, start: int, end: int, *, finishing: bool = False) -> None:
        index = start
        while index < end:
            if not finishing:
                self._check_abort()
            command = program.commands[index]
            name, args, line = command.name, command.args, command.line
            if name == "var":
                self._set(args[0], 0.0, "aa")
            elif name == "store_var":
                value, integer = ms.parse_literal(args[1])
                self._set(args[0], value, args[2])
            elif name == "copy_var":
                self.vars[args[1]] = list(self.vars.get(args[0], [0.0, "aa", 0, None]))
            elif name in {"add_var", "sub_var"}:
                current = self.vars.setdefault(args[0], [0.0, "aa", 0, None])
                current[0] = current[0] + self._value(args[1]) * (1 if name == "add_var" else -1)
            elif name == "set_pgstat_mode":
                mode = int(ms.parse_literal(args[0] if args[0].endswith("i") else args[0] + "i")[0])
                if mode not in {0, 2, 3, 4, 6}:
                    raise RuntimeFault(0x0021, line)
                self.mode = mode
            elif name == "set_autoranging" and args[0] == "ba":
                low, high = self._value(args[1]), self._value(args[2])
                if low <= 0 or high <= 0:
                    raise RuntimeFault(0x4204, line)
                self.autorange = (min(low, high), max(low, high))
            elif name == "set_e":
                value = self._value(args[0])
                self._limits(line, value)
            elif name == "cell_on":
                if self.mode == 0:
                    raise RuntimeFault(0x0023, line)
                self.cell_on = True
            elif name == "cell_off":
                self.cell_on = False
            elif name == "timer_start":
                self.timer0 = self.clock
            elif name == "timer_get":
                self._set(args[0], self.clock - self.timer0, "eb")
            elif name == "wait":
                self._sleep(self._value(args[0]))
            elif name == "meas":
                seconds = self._value(args[0])
                self._sleep(seconds)
                if args[2] == "ab":
                    self._set(args[1], self.cell.ocp(self.clock), "ab")
                else:
                    self._set(args[1], 0.0, args[2])
            elif name.startswith("meas_loop_"):
                self._measure(program, index, finishing)
                index = command.match
            elif name == "pck_start":
                self.package = []
            elif name == "pck_add":
                if self.package is None:
                    raise RuntimeFault(0x401B, line)
                value, kind, status, current_range = self.vars.get(args[0], [0.0, "aa", 0, None])
                self.package.append((kind, value, status, current_range))
            elif name == "pck_end":
                if self.package is None:
                    raise RuntimeFault(0x401B, line)
                self.send(ms.format_package(self.package))
                self.package = None
            elif name in {"if", "elseif"}:
                if OPERATORS[args[1]](self._value(args[0]), self._value(args[2])):
                    # 执行到下一个 elseif / else / endif，然后跳到 endif 之后
                    self._block(program, index + 1, command.match, finishing=finishing)
                    index = self._endif(program, index) + 1
                    continue
                following = program.commands[command.match]
                if following.name == "elseif":
                    index = command.match
                elif following.name == "else":
                    self._block(program, command.match + 1, following.match, finishing=finishing)
                    index = following.match + 1
                else:
                    index = command.match + 1
                continue
            elif name in {"else", "endif"}:
                pass
            elif name == "loop":
                while OPERATORS[args[1]](self._value(args[0]), self._value(args[2])):
                    self.send("L")
                    try:
                        self._block(program, index + 1, command.match, finishing=finishing)
                    except _Break:
                        break
                index = command.match
            elif name == "breakloop":
                raise _Break()
            elif name == "send_string":
                self.send("T" + args[0])
            elif name == "abort":
                if not finishing:
                    self.abort_event.set()
                    raise _Abort()
            # set_pgstat_chan / set_max_bandwidth / set_range / set_range_minmax / 其他：记下就行，不影响假数据
            index += 1

    @staticmethod
    def _endif(program: Program, index: int) -> int:
        while program.commands[index].name != "endif":
            index = program.commands[index].match
        return index

    def _sleep(self, seconds: float) -> None:
        self.clock += seconds
        if self.time_scale > 0 and seconds > 0:
            self.abort_event.wait(seconds * self.time_scale)

    # ---------- 测量循环 ----------

    def _current(self, current: float) -> tuple[float, int, int]:
        """按自动量程的上下限选量程：返回 (读数, 状态位, 量程号)。超出最高量程置过载位、读数削顶。"""
        low, high = self.autorange
        allowed = [(index, nominal) for index, nominal in RANGES if low / 3.0001 <= nominal <= high * 1.0001]
        if not allowed:
            allowed = [min(RANGES, key=lambda item: abs(math.log10(item[1] / high)))]
        index, nominal = next(((i, n) for i, n in allowed if abs(current) <= 2.46 * n), allowed[-1])
        status = 0
        magnitude = abs(current)
        if magnitude > 2.92 * nominal:
            status |= ms.STATUS_OVERLOAD
            current = math.copysign(3.0 * nominal, current) if magnitude > 3.0 * nominal else current
        elif magnitude > 2.46 * nominal:
            status |= ms.STATUS_OVERLOAD_WARNING
        elif magnitude < 0.123 * nominal and (index, nominal) == allowed[0]:
            status |= ms.STATUS_UNDERLOAD
        return current, status, index

    def _measure(self, program: Program, index: int, finishing: bool) -> None:
        command = program.commands[index]
        name, args, line = command.name, command.args, command.line
        technique = name[len("meas_loop_"):]
        values = [self._value(arg) for arg in args]
        record = {"technique": technique, "args": dict(zip(SPEC[name], args)), "values": values, "points": 0}
        self.record["techniques"].append(record)
        iterations = self._iterations(technique, command, values, line)
        self.send(f"M{ms.TECHNIQUE_CODES[technique]:04X}")
        scan = None
        self.loop_break.clear()
        for item in iterations:
            if item[0] == "scan":
                if scan is not None:
                    self.send("-")
                scan = item[1]
                self.send(f"C{scan:04d}")
                continue
            _, dt, outputs = item
            if (not finishing and self.abort_event.is_set()) or self.loop_break.is_set():
                break
            self.resumed.wait()
            self._sleep(dt)
            if not finishing and self.abort_event.is_set():
                break
            for var, value, kind, status, current_range in outputs:
                self._set(var, value, kind, status, current_range)
            if self.fail_after is not None and self.packages >= self.fail_after:
                raise RuntimeFault(0x0032, line)  # 仪器中止测量：on_finished 不执行，电池还开着
            try:
                self._block(program, index + 1, command.match, finishing=finishing)
            except (_Break, _Abort):
                break
            record["points"] += 1
            self.packages += 1
            if self.drop_after is not None and self.packages == self.drop_after:
                self.drop()
        if scan is not None:
            self.send("-")
        self.send("*")
        if not finishing and self.abort_event.is_set():
            raise _Abort()

    def _iterations(self, technique: str, command: Command, values: list[float], line: int):
        """生成 ("point", 间隔秒, [(变量, 值, 类型, 状态, 量程号)]) 与 ("scan", n)。参数不对抛 RuntimeFault。"""
        cell, args = self.cell, command.args
        info = ms.DEVICE_TYPES[self.device_type]
        if technique == "ocp":
            interval, run = values[1], values[2]
            if self.cell_on:
                raise RuntimeFault(0x0014, line)
            if interval <= 0 or run <= 0:
                raise RuntimeFault(0x4204, line)
            count = int(math.floor(run / interval + 1e-9))

            def ocp():
                for k in range(count):
                    yield "point", interval, [(args[0], cell.ocp(self.clock + interval), "ab", 0, None)]
            return ocp()
        if not self.cell_on:
            raise RuntimeFault(0x4027, line)
        if technique == "eis":
            amplitude, f_start, f_end, points, dc = values[3:8]
            if self.mode != 3:
                raise RuntimeFault(0x0023, line)
            if amplitude <= 0 or amplitude > info["eis_max_vrms"]:
                raise RuntimeFault(0x0012, line)
            if min(f_start, f_end) <= 0 or max(f_start, f_end) > info["eis_max_hz"]:
                raise RuntimeFault(0x0011, line)
            self._limits(line, dc)
            count = int(points)
            if count < 1:
                raise RuntimeFault(0x4204, line)

            def eis():
                for k in range(count):
                    f = f_start * (f_end / f_start) ** (k / (count - 1)) if count > 1 else f_start
                    z = cell.measured_impedance(f)
                    current, status, current_range = self._current(amplitude / max(abs(z), 1e-12))
                    yield "point", 0.2 + 3.0 / f, [(args[0], f, "dc", 0, None),
                                                   (args[1], z.real, "cc", status, current_range),
                                                   (args[2], z.imag, "cd", 0, None)]
            return eis()
        if technique == "ca":
            e, interval, run = values[2], values[3], values[4]
            if interval <= 0 or run <= 0:
                raise RuntimeFault(0x4204, line)
            self._limits(line, e)
            count = int(math.floor(run / interval + 1e-9))
            sweep = cell.sweep(interval)

            def ca():
                for k in range(count):
                    current, status, current_range = self._current(sweep.current(e, de_dt=0.0))
                    yield "point", interval, [(args[0], e, "da", 0, None), (args[1], current, "ba", status, current_range)]
            return ca()
        step, rate = (values[4], values[5]) if technique == "lsv" else (values[5], values[6])
        if step <= 0 or rate <= 0:
            raise RuntimeFault(0x4204, line)
        dt = step / rate
        sweep = cell.sweep(dt)
        if technique == "lsv":
            begin, end = values[2], values[3]
            self._limits(line, begin, end)
            count = int(round(abs(end - begin) / step)) + 1
            if count < 2:
                raise RuntimeFault(0x4029, line)
            sign = 1.0 if end >= begin else -1.0

            def lsv():
                for k in range(count):
                    e = end if k == count - 1 else begin + sign * k * step
                    current, status, current_range = self._current(sweep.current(e, de_dt=sign * rate))
                    yield "point", dt, [(args[0], e, "da", 0, None), (args[1], current, "ba", status, current_range)]
            return lsv()
        begin, vertex1, vertex2 = values[2], values[3], values[4]
        self._limits(line, begin, vertex1, vertex2)
        scans = int(command.options["nscans"][0].rstrip("i")) if "nscans" in command.options else 1
        path: list[tuple[float, float]] = []
        for start, stop in ((begin, vertex1), (vertex1, vertex2), (vertex2, begin)):
            steps = int(round(abs(stop - start) / step))
            sign = 1.0 if stop >= start else -1.0
            path += [(start + sign * k * step, sign) for k in range(steps)]
        if not path:
            raise RuntimeFault(0x4029, line)

        def cv():
            for scan in range(scans):
                if scans > 1:
                    yield "scan", scan
                points = path if scans > 1 else path + [(begin, path[-1][1])]
                for e, sign in points:
                    current, status, current_range = self._current(sweep.current(e, de_dt=sign * rate))
                    yield "point", dt, [(args[0], e, "da", 0, None), (args[1], current, "ba", status, current_range)]
        return cv()


class InstrumentServer:
    """假仪器的 TCP 口（像串口服务器那样透明转发）。"""

    def __init__(self, instrument: FakeInstrument, host: str = "127.0.0.1", port: int = 0):
        self.instrument = instrument
        owner = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                connection = self.request
                owner.instrument.attach(connection)
                buffer = b""
                try:
                    while True:
                        try:
                            chunk = connection.recv(4096)
                        except OSError:
                            return
                        if not chunk:
                            return
                        buffer += chunk
                        while b"\n" in buffer:
                            raw, buffer = buffer.split(b"\n", 1)
                            owner.instrument.receive(raw.decode("ascii", errors="replace"))
                finally:
                    owner.instrument.detach(connection)

        self.server = socketserver.ThreadingTCPServer((host, port), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.instrument.abort_event.set()
        self.instrument.drop()
        self.server.shutdown()
        self.server.server_close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4100)
    parser.add_argument("--device-type", default="es4_hr", choices=sorted(ms.DEVICE_TYPES))
    parser.add_argument("--time-scale", type=float, default=1.0, help="出点的实际间隔 = 测量本身的间隔 × 这个系数")
    args = parser.parse_args(argv)
    server = InstrumentServer(FakeInstrument(device_type=args.device_type, time_scale=args.time_scale),
                              host=args.host, port=args.port)
    print(f"假 MethodSCRIPT 仪器（{args.device_type}）在 {args.host}:{server.port}")
    try:
        server.thread.join()
    except KeyboardInterrupt:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["FakeInstrument", "InstrumentServer", "Program", "RuntimeFault", "ScriptError", "parse_script"]
