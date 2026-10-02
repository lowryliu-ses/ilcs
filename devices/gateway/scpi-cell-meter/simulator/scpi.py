"""假 SCPI 仪表的公共部分：命令头按手册写法匹配、一行多条命令、待测电芯、故障与测量计数。

真仪表的行为以 README「参考」里列的手册为准。这里只模拟三份 profile（以及 README 里给的可选写法）用到的命令，
够 ILCS 的 `line_command_v1` 驱动与接入验收清单对着它跑；手册没写清、按模拟需要定下的行为（忙、联锁时报哪条错）
在各型号的文件头注明，不当成真表的行为。

一条命令行按 IEEE 488.2 的规矩处理：分号隔开的几条依次执行，某条出错就记进错误队列 / 事件寄存器，这一行余下的
不再执行；出错的查询不回复。不认 SCPI 的「当前路径」（`:SOUR:FUNC CURR;CURR 0` 的第二条按绝对路径解析）。
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import threading
import time
from typing import Any, Callable

LOWER = "abcdefghijklmnopqrstuvwxyz"


class _Close:
    """回复丢失：动作照做，回复不发、连接断开（驱动立刻知道「没回」，不用等到超时）。"""

    def __repr__(self) -> str:
        return "CLOSE"


CLOSE = _Close()


@dataclass
class Delayed:
    """回复迟到 `seconds` 秒（slow_submit）。"""

    reply: str | None
    seconds: float


class ScpiError(Exception):
    """命令被拒。`kind` 是 IEEE 488.2 的错误类别（command / execution / device / query），`code` 与 `message`
    给错误队列用（Keithley）；Hioki 只按类别置标准事件寄存器的位。"""

    def __init__(self, kind: str, code: int, message: str):
        super().__init__(message)
        self.kind, self.code, self.message = kind, code, message


def header(spec: str) -> re.Pattern[str]:
    """手册写法 → 正则：`SOURce[1]:CURRent[:LEVel]` 认 SOUR / SOURCE、可省的 `[:LEVel]` 与数字后缀 `[1]`，不分大小写。

    SCPI 只认短写或长写两种（`SOURC` 不行）。写法不带开头的冒号：收到的命令头先去掉开头的冒号再匹配；
    整个可省的头一个节点写成 `[SENSe[1]:]VOLTage`。
    """
    parts: list[str] = []
    index = 0
    while index < len(spec):
        char = spec[index]
        if char == "[":
            parts.append("(?:")
        elif char == "]":
            parts.append(")?")
        elif char.isalpha():
            end = index
            while end < len(spec) and spec[end].isalpha():
                end += 1
            word = spec[index:end]
            short = word.rstrip(LOWER)
            rest = word[len(short):]
            parts.append(re.escape(short) + (f"(?:{rest.upper()})?" if rest else ""))
            index = end
            continue
        else:
            parts.append(re.escape(char))
        index += 1
    return re.compile("".join(parts), re.IGNORECASE)


def _split(text: str, separator: str) -> list[str]:
    """按分隔符拆开（引号里的不算），去掉两侧空白与空项。"""
    found: list[str] = []
    current: list[str] = []
    quote = ""
    for char in text:
        if quote:
            current.append(char)
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
            current.append(char)
        elif char == separator:
            found.append("".join(current))
            current = []
        else:
            current.append(char)
    found.append("".join(current))
    return [item.strip() for item in found if item.strip()]


def units(line: str) -> list[str]:
    """一行里分号隔开的几条命令（引号里的分号不算）。"""
    return _split(line, ";")


def arguments(args: str) -> list[str]:
    """逗号隔开的参数（引号里的逗号不算）。"""
    return _split(args, ",")


def unquote(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def boolean(arg: str) -> bool:
    value = arg.strip().upper()
    if value in {"1", "ON"}:
        return True
    if value in {"0", "OFF"}:
        return False
    raise ScpiError("execution", -224, "Illegal parameter value")


def number(arg: str, low: float | None = None, high: float | None = None, *, named: dict[str, float] | None = None) -> float:
    """数值参数；`named` 给 MIN / MAX / DEF 之类的写法。越界是执行错误（-222），写法不对是命令错误（-104）。"""
    text = arg.strip()
    if not text:
        raise ScpiError("command", -109, "Missing parameter")
    for name, value in (named or {}).items():
        if header(name).fullmatch(text):
            return value
    try:
        value = float(text)
    except ValueError as exc:
        raise ScpiError("command", -104, "Data type error") from exc
    if (low is not None and value < low) or (high is not None and value > high):
        raise ScpiError("execution", -222, "Parameter data out of range")
    return value


def choice(arg: str, options: dict[str, str]) -> str:
    """选项参数：`options` 是 {手册写法: 存下来的值}，例如 {"HIMPedance": "HIMP"}。"""
    text = unquote(arg)
    if not text:
        raise ScpiError("command", -109, "Missing parameter")
    for spec, value in options.items():
        if header(spec).fullmatch(text):
            return value
    raise ScpiError("execution", -224, "Illegal parameter value")


@dataclass
class Cell:
    """夹具上的电芯：开路电压（V）、1 kHz 交流内阻（Ω）；`connected=False` 表示探针没接触上。"""

    ocv_V: float = 3.85
    ir_ohm: float = 0.015
    connected: bool = True


Handler = Callable[[bool, str], Any]


class ScpiMeter:
    """一台假仪表：`handle(一行命令)` 返回回复文字、None（不回复）或 CLOSE（不回复并断开连接）。

    子类给出 `commands()`（手册写法 → 处理函数）、`record()`（错误怎么记）与身份。线程安全：几个连接、
    统一控制口可以同时进来，状态改动都在 `lock` 里做。
    """

    MODEL = "SCPI"
    VENDOR = ""
    newline = "\n"
    # 这台模拟仪表在协议层注入不了的故障与原因：告诉验收清单判跳过，不硬判不通过
    unsupported: dict[str, str] = {}

    def __init__(self, cell: Cell | None = None, serial: str = "", firmware: str = ""):
        self.cell = cell or Cell()
        self.serial = serial or f"ILCS-SIMULATOR-{self.MODEL}-01"
        self.firmware = firmware or self.default_firmware()
        self.lock = threading.RLock()
        self.fault = "none"
        self.fault_parameter = 0.0
        self.motions = 0  # 真的做了几次测量：验收「同一指令号重投不再动作」看它
        self.received: list[str] = []
        self._table = [(header(spec), handler) for spec, handler in self.commands()]

    # ---------- 子类 ----------

    def commands(self) -> list[tuple[str, Handler]]:
        return []

    def default_firmware(self) -> str:
        return "sim-1.0"

    def record(self, error: ScpiError) -> None:
        raise NotImplementedError

    def busy_error(self) -> ScpiError:
        return ScpiError("execution", -200, "Execution error; simulated busy")

    def accepts(self, name: str, query: bool) -> ScpiError | None:
        """这条命令此刻收不收：缺省忙的时候不收改设置的命令（查询、通用命令照常）。"""
        if self.fault == "busy" and not query and not name.startswith("*"):
            return self.busy_error()
        return None

    def snapshot(self) -> dict[str, Any]:
        return {}

    # ---------- 故障与状态 ----------

    def set_fault(self, mode: str, parameter: float = 0.0) -> None:
        with self.lock:
            self.fault = mode
            self.fault_parameter = float(parameter or 0)

    def state(self) -> dict[str, Any]:
        with self.lock:
            return {
                "model": self.MODEL, "serial": self.serial, "fault": self.fault,
                "fault_parameter": self.fault_parameter, "motions": self.motions, **self.snapshot(),
            }

    def measured(self, reply: str) -> Any:
        """一次测量做完之后怎么回：按注入的故障丢回复、迟到，或照常回。"""
        if self.fault == "lost_receipt":
            return CLOSE
        if self.fault == "slow_submit":
            return Delayed(reply, self.fault_parameter or 1.0)
        return reply

    # ---------- 报文 ----------

    def handle(self, line: str) -> Any:
        text = line.strip()
        if not text:
            return None
        delay = 0.0
        replies: list[str] = []
        with self.lock:
            self.received.append(text)
            for unit in units(text):
                head, _, args = unit.partition(" ")
                query = head.endswith("?")
                name = (head[:-1] if query else head).lstrip(":")
                try:
                    handler = self._lookup(name)
                    if handler is None:
                        raise ScpiError("command", -113, "Undefined header")
                    refused = self.accepts(name, query)
                    if refused is not None:
                        raise refused
                    result = handler(query, args.strip())
                except ScpiError as error:
                    self.record(error)
                    break  # 出错之后到结束符为止的命令都不执行
                if result is CLOSE:
                    return CLOSE
                if isinstance(result, Delayed):
                    delay, result = result.seconds, result.reply
                if result is not None:
                    replies.append(str(result))
        if delay:
            time.sleep(delay)  # 锁外等：迟到的回复不挡统一控制口
        return ";".join(replies) if replies else None

    def _lookup(self, name: str) -> Handler | None:
        for pattern, handler in self._table:
            if pattern.fullmatch(name):
                return handler
        return None


def query_only(query: bool) -> None:
    if not query:
        raise ScpiError("command", -113, "Undefined header")


def command_only(query: bool) -> None:
    if query:
        raise ScpiError("query", -113, "Undefined header")
