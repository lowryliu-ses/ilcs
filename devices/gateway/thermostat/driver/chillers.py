"""真实接口：三家恒温循环器 / 冷水机的命令，统一成同一组方法（`Chiller`）。命令与应答取自厂家手册（见 README「参考」）。

| 方法 | Huber（PB 命令） | Julabo | LAUDA |
|---|---|---|---|
| 浴温 | `{M01****` → `{S01xxxx`（内部温度） | `in_pv_00` → `-10.03` | `IN_PV_00` → `-10.03`（出口温度） |
| 设定值 | `{M00****`；写 `{M00xxxx`，回冷水机此刻的设定值 | `in_sp_00`；写 `out_sp_00 -10.00`（不回） | `IN_SP_00`；写 `OUT_SP_00_-10.00` → `OK` |
| 启停 | `{M140001` / `{M140000`（控温开 / 关） | `out_mode_05 1` / `0`（不回），`in_mode_05` 读 | `START` / `STOP`（待机），`IN_MODE_02` 读（0 开 / 1 待机） |
| 报警 | 状态字 `{M0A****` 第 8 位，错误号 `{M05****` | `status` 回负数开头的报警文字 | `STATUS`（0 正常 / -1 故障），`STAT` 细节 |

- Huber：数值是 4 位十六进制的二进制补码，温度单位 0.01 ℃（-23.15 ℃ → `F6F5`）；没开放的地址回 `7FFF`；
  探头读不到回 -151.00 ℃（`C504`）。命令格式不对冷水机一句都不回。用数据命令改的设置不存盘，断电后回到面板上的值。
- Julabo：命令和参数之间空格、CR 结尾；`in` 命令回一行、`out` 命令什么都不回；**只有面板切到远程控制（status 02 / 03）
  才执行 `out` 命令**，面板控制时（00 / 01）一律悄悄忽略。值超范围不回话，下一次 `status` 报 -10 / -11。
- LAUDA：写命令也回 `OK` 或 `ERR_x`；`_` 也可以写成空格。WK / WKL 冷水机的设定值要过几秒才转给控制器，一小时内
  最多改 20 次（`ERR_38`），所以网关只在设定值真的要变时才写。

异常：`LinkError`（链路，`sent` 说明写出去没有）；`ChillerError`（应答读不懂、读数不可用：这次没读到 / 没确认）；
`Refused`（冷水机明确不接受这条命令：设备没动）。
"""
from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any

from .link import Link, LinkError

# 设定值回读和要求的差多少算没设上（三家都按 0.01 ℃ 收）
SETPOINT_TOLERANCE = 0.011


class ChillerError(Exception):
    """应答读不懂、读数不可用（探头坏了、命令没开放）：按「这次没读到 / 没确认」处理。"""


class Refused(ChillerError):
    """冷水机明确不接受这条命令（值超范围、命令不允许、面板控制模式）：设备没动。"""


@dataclass
class Reading:
    """一次读数。"""

    bath: float               # 浴温 ℃（Huber 内部温度、Julabo 浴温、LAUDA 出口温度）
    setpoint: float           # 冷水机此刻的设定值 ℃
    running: bool             # 控温 / 循环开着
    alarm: str = ""           # 报警（冷水机会停机或已经停机）；空 = 没有
    warning: str = ""         # 警告（照常运行）
    remote: bool = True       # 接受远程命令（Julabo 面板要切到远程）
    restarted: bool = False   # 冷水机自上次读之后重启过（只有 Huber 报得出来：状态字第 14 位）


class Chiller:
    """一台冷水机。`poll` / `health` 只读；`set_setpoint`、`start`、`stop` 会让设备动。"""

    vendor = ""

    def __init__(self, link: Link):
        self.link = link

    def describe(self) -> str:
        return self.link.describe()

    def identify(self) -> dict[str, str]:
        """{model, firmware, serial}，读不到的留空。"""
        raise NotImplementedError

    def poll(self) -> Reading:
        raise NotImplementedError

    def health(self) -> tuple[str, bool]:
        """(报警, 接不接远程命令)：健康检查用，命令尽量少。"""
        raise NotImplementedError

    def limits(self) -> tuple[float | None, float | None]:
        """冷水机自己的设定值范围；读不到返回 (None, None)。"""
        return None, None

    def set_setpoint(self, value: float) -> float:
        """写设定值，返回冷水机写完之后报的设定值（和要求的对不上由调用方判断）。"""
        raise NotImplementedError

    def start(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError


# ---------- Huber：PB 命令 ----------

def huber_encode(celsius: float) -> str:
    """℃ → 4 位十六进制补码（0.01 ℃）。"""
    return f"{int(round(float(celsius) * 100)) & 0xFFFF:04X}"


def huber_signed(raw: int) -> int:
    return raw - 0x10000 if raw & 0x8000 else raw


def huber_temperature(raw: int) -> float:
    """4 位十六进制 → ℃。手册：值域 -15111 … 50000，按有符号数小于 -15111 的要按无符号数读（300 ℃ 以上的设备）。"""
    value = huber_signed(raw)
    if value < -15111:
        value = raw
    return value / 100


class Huber(Chiller):
    vendor = "Huber"
    SETPOINT, INTERNAL, ERROR, WARNING, STATUS, CONTROL = 0x00, 0x01, 0x05, 0x06, 0x0A, 0x14
    SERIAL_LOW, SERIAL_HIGH, MIN_SETPOINT, MAX_SETPOINT = 0x1B, 0x1C, 0x30, 0x31
    UNAVAILABLE = 0x7FFF
    NO_SENSOR = -15100
    # 状态字（vStatus1）：0 控温开着，8 有错误，9 有新警告，14 为 0 表示自上次读之后电控重启过
    BIT_CONTROL, BIT_ERROR, BIT_WARNING, BIT_RUNNING_SINCE = 0, 8, 9, 14

    def __init__(self, link: Link):
        super().__init__(link)
        self._restart_seen = False  # 读状态字时看到过「重启过」：留给下一次 poll 报（健康检查读到了也不能漏掉）

    def exchange(self, address: int, value: str | None = None) -> int:
        """发一条 PB 命令，返回应答里的 16 位原值。`value` 为空是只读（`****`）。"""
        command = f"{{M{address:02X}{value or '****'}"
        reply = self.link.ask(command)
        if len(reply) != 8 or reply[:2] != "{S" or reply[2:4].upper() != f"{address:02X}":
            raise ChillerError(f"{self.describe()} 对 {command} 回 {reply!r}，不是这个地址的 PB 应答")
        try:
            raw = int(reply[4:8], 16)
        except ValueError as exc:
            raise ChillerError(f"{self.describe()} 对 {command} 回 {reply!r}，数值不是十六进制") from exc
        if raw == self.UNAVAILABLE:
            raise ChillerError(f"{self.describe()} 地址 0x{address:02X} 回 7FFF：这台冷水机没有这个变量或没开放（E-grade）")
        return raw

    def _temperature(self, address: int, what: str) -> float:
        raw = self.exchange(address)
        if huber_signed(raw) == self.NO_SENSOR:
            raise ChillerError(f"{self.describe()} 的{what}回 -151 ℃：没有探头或探头坏了")
        return huber_temperature(raw)

    def _status(self) -> int:
        status = self.exchange(self.STATUS)
        if not status & (1 << self.BIT_RUNNING_SINCE):
            self._restart_seen = True
        return status

    def _alarm(self, status: int) -> str:
        if not status & (1 << self.BIT_ERROR):
            return ""
        code = huber_signed(self.exchange(self.ERROR))
        return f"Huber 报错 {code}" if code else "Huber 状态字报错（错误号读回 0）"

    def identify(self) -> dict[str, str]:
        serial = ""
        try:
            serial = str((self.exchange(self.SERIAL_HIGH) << 16) | self.exchange(self.SERIAL_LOW))
        except ChillerError:
            pass  # 老控制器可能没开放序列号
        return {"model": "", "firmware": "", "serial": serial}

    def poll(self) -> Reading:
        status = self._status()
        bath = self._temperature(self.INTERNAL, "内部温度")
        setpoint = huber_temperature(self.exchange(self.SETPOINT))
        warning = ""
        if status & (1 << self.BIT_WARNING):
            code = huber_signed(self.exchange(self.WARNING))
            warning = f"Huber 警告 {code}" if code else ""
        restarted, self._restart_seen = self._restart_seen, False
        return Reading(bath=bath, setpoint=setpoint, running=bool(status & (1 << self.BIT_CONTROL)),
                       alarm=self._alarm(status), warning=warning, restarted=restarted)

    def health(self) -> tuple[str, bool]:
        return self._alarm(self._status()), True

    def limits(self) -> tuple[float | None, float | None]:
        try:
            return (huber_temperature(self.exchange(self.MIN_SETPOINT)),
                    huber_temperature(self.exchange(self.MAX_SETPOINT)))
        except ChillerError:
            return None, None

    def set_setpoint(self, value: float) -> float:
        # 冷水机回的是写完之后的设定值：值被限幅时和发的不一样
        return huber_temperature(self.exchange(self.SETPOINT, huber_encode(value)))

    def start(self) -> None:
        raw = self.exchange(self.CONTROL, "0001")
        if raw != 1:
            raise Refused(f"{self.describe()} 对 {{M140001 回 {raw:04X}：控温没开起来")

    def stop(self) -> None:
        raw = self.exchange(self.CONTROL, "0000")
        if raw != 0:
            raise ChillerError(f"{self.describe()} 对 {{M140000 回 {raw:04X}：控温没关掉")


# ---------- Julabo ----------

class Julabo(Chiller):
    vendor = "Julabo"
    # status 回负数时：-08 … -11 是对上一条命令的意见（命令不对、当前模式不允许、值太小、值太大），不是设备报警
    COMMAND_ERRORS = {-8, -9, -10, -11}
    REMOTE = {2, 3}      # 02 REMOTE STOP、03 REMOTE START
    STARTED = {1, 3}     # 01 MANUAL START、03 REMOTE START
    START_CHECKS, START_PAUSE = 3, 0.3

    def __init__(self, link: Link, *, uppercase: bool = False):
        super().__init__(link)
        # 手册里 CF 系列写大写（IN_PV_00），FL 等写小写（in_pv_00）；缺省发小写
        self.uppercase = uppercase

    def _command(self, text: str) -> str:
        return text.upper() if self.uppercase else text

    def _ask(self, text: str) -> str:
        return self.link.ask(self._command(text))

    def _send(self, text: str) -> None:
        self.link.send(self._command(text))

    def _number(self, text: str, what: str) -> float:
        reply = self._ask(text)
        try:
            return float(reply.split()[0])
        except (IndexError, ValueError) as exc:
            hint = "：探头没接？" if "---" in reply else ""
            raise ChillerError(f"{self.describe()} 对 {text}（{what}）回 {reply!r}，不是数值{hint}") from exc

    def status(self) -> tuple[int, str]:
        """(状态码, 原文)：00–03 是状态，负数是报警 / 警告 / 对上一条命令的意见。"""
        reply = self._ask("status")
        try:
            return int(reply.split()[0]), reply
        except (IndexError, ValueError) as exc:
            raise ChillerError(f"{self.describe()} 对 status 回 {reply!r}，读不懂") from exc

    @classmethod
    def classify(cls, code: int, text: str) -> str:
        if code >= 0:
            return "state"
        if code in cls.COMMAND_ERRORS:
            return "command"
        return "warning" if "WARNING" in text.upper() else "alarm"

    def identify(self) -> dict[str, str]:
        return {"model": "", "firmware": self._ask("version"), "serial": ""}

    def _mode(self) -> int:
        return int(self._number("in_mode_05", "启停状态"))

    def poll(self) -> Reading:
        code, text = self.status()
        bath = self._number("in_pv_00", "浴温")
        setpoint = self._number("in_sp_00", "设定值")
        kind = self.classify(code, text)
        if kind == "state":
            running, remote = code in self.STARTED, code in self.REMOTE
        else:  # status 报的是消息：启停另读
            running, remote = self._mode() == 1, True
        return Reading(bath=bath, setpoint=setpoint, running=running, remote=remote,
                       alarm=f"Julabo 报警：{text}" if kind == "alarm" else "",
                       warning=f"Julabo 警告：{text}" if kind == "warning" else "")

    def health(self) -> tuple[str, bool]:
        code, text = self.status()
        kind = self.classify(code, text)
        return (f"Julabo 报警：{text}" if kind == "alarm" else ""), (code in self.REMOTE if kind == "state" else True)

    def _refusal(self, command: str) -> None:
        """写命令没生效：看 status 是不是冷水机明确不接受（值超范围、面板控制模式），是就抛 `Refused`。"""
        code, text = self.status()
        kind = self.classify(code, text)
        if kind == "command":
            raise Refused(f"Julabo 不接受 {command}：{text}")
        if kind == "state" and code not in self.REMOTE:
            raise Refused(f"Julabo 在面板控制模式（status {text}）：远程命令一律不执行，{command} 被忽略")

    def set_setpoint(self, value: float) -> float:
        command = f"out_sp_00 {float(value):.2f}"
        self._send(command)  # out 命令不回：回读确认
        reported = self._number("in_sp_00", "设定值")
        if abs(reported - value) > SETPOINT_TOLERANCE:
            self._refusal(command)
        return reported

    def start(self) -> None:
        self._send("out_mode_05 1")
        for attempt in range(self.START_CHECKS):
            if self._mode() == 1:
                return
            time.sleep(self.START_PAUSE)
        self._refusal("out_mode_05 1")
        raise ChillerError(f"{self.describe()} 发了 out_mode_05 1，in_mode_05 一直回 0：没确认启动")

    def stop(self) -> None:
        self._send("out_mode_05 0")
        if self._mode() != 0:
            raise ChillerError(f"{self.describe()} 发了 out_mode_05 0，in_mode_05 还是 1：没确认停下")


# ---------- LAUDA ----------

LAUDA_ERRORS = {
    "ERR_2": "输入错误（如缓冲区溢出）", "ERR_3": "命令错误", "ERR_5": "数值语法错误", "ERR_6": "数值不允许",
    "ERR_32": "上限不高于下限", "ERR_38": "设定值一小时内改了 20 次以上",
}


class Lauda(Chiller):
    vendor = "LAUDA"
    # 设定值写进去以后回读等多久（WK / WKL 要几秒才转给控制器）
    READBACK_SEC, READBACK_PAUSE = 5.0, 0.5
    START_CHECKS, START_PAUSE = 3, 0.3

    def _ask(self, command: str) -> str:
        reply = self.link.ask(command)
        if reply.upper().startswith("ERR"):
            code = reply.split()[0].upper()
            raise Refused(f"LAUDA 对 {command} 回 {reply}（{LAUDA_ERRORS.get(code, '未登记的错误号')}）")
        return reply

    def _number(self, command: str, what: str) -> float:
        reply = self._ask(command)
        try:
            return float(reply)
        except ValueError as exc:
            raise ChillerError(f"{self.describe()} 对 {command}（{what}）回 {reply!r}，不是数值") from exc

    def _ok(self, command: str) -> None:
        reply = self._ask(command)
        if reply.upper() != "OK":
            raise ChillerError(f"{self.describe()} 对 {command} 回 {reply!r}，不是 OK")

    def _standby(self) -> bool:
        reply = self._ask("IN_MODE_02")
        if reply not in {"0", "1"}:
            raise ChillerError(f"{self.describe()} 对 IN_MODE_02 回 {reply!r}，不是 0 / 1")
        return reply == "1"

    def _alarm(self) -> str:
        reply = self._ask("STATUS")
        if reply == "0":
            return ""
        if reply != "-1":
            raise ChillerError(f"{self.describe()} 对 STATUS 回 {reply!r}，不是 0 / -1")
        try:
            detail = self._ask("STAT")
        except ChillerError:
            detail = "读不到"
        return f"LAUDA 报故障（STATUS -1，STAT {detail}）"

    def identify(self) -> dict[str, str]:
        model = self._ask("TYPE")
        try:
            firmware = self._ask("VERSION_R")
        except Refused:
            firmware = self._ask("VERSION")
        return {"model": model, "firmware": firmware, "serial": ""}

    def poll(self) -> Reading:
        alarm = self._alarm()
        return Reading(bath=self._number("IN_PV_00", "浴温"), setpoint=self._number("IN_SP_00", "设定值"),
                       running=not self._standby(), alarm=alarm)

    def health(self) -> tuple[str, bool]:
        return self._alarm(), True

    def set_setpoint(self, value: float) -> float:
        self._ok(f"OUT_SP_00_{float(value):.2f}")
        deadline = time.monotonic() + self.READBACK_SEC
        while True:
            reported = self._number("IN_SP_00", "设定值")
            if abs(reported - value) <= SETPOINT_TOLERANCE or time.monotonic() >= deadline:
                return reported
            time.sleep(self.READBACK_PAUSE)

    def start(self) -> None:
        self._ok("START")
        for attempt in range(self.START_CHECKS):
            if not self._standby():
                return
            time.sleep(self.START_PAUSE)
        raise ChillerError(f"{self.describe()} 回了 OK，IN_MODE_02 一直是 1（待机）：没确认启动")

    def stop(self) -> None:
        self._ok("STOP")
        if not self._standby():
            raise ChillerError(f"{self.describe()} 回了 OK，IN_MODE_02 还是 0：没确认进入待机")


KINDS: dict[str, type[Chiller]] = {"huber": Huber, "julabo": Julabo, "lauda": Lauda}


def make_chiller(kind: str, link: Link, **options: Any) -> Chiller:
    if kind == "julabo":
        return Julabo(link, uppercase=bool(options.get("uppercase")))
    return KINDS[kind](link)


__all__ = ["Chiller", "ChillerError", "Huber", "Julabo", "KINDS", "Lauda", "LinkError", "Reading", "Refused",
           "huber_encode", "huber_temperature", "make_chiller"]
