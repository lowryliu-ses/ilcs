"""假 Hioki BT3562 系列电池内阻测试仪（BT3561A / BT3562A / BT3563A / BT3562 / BT3563 的通信命令子集）。

行为依据：Hioki 说明书 BT3562A981-12（2024 年 6 月第 12 版）第 8 章「RS-232C/GP-IB/LAN Interfaces」：

- 命令不回复；查询出错不回复，按类别置标准事件寄存器（SESR）的位：命令错 CME（32）、执行错 EXE（16）、
  设备错 DDE（8）、查询错 QYE（4）；开机时 PON（128）置位。`*ESR?` 读出并清零，`*CLS` 清零。
- 设备事件寄存器 ESR0：bit0 EOM（转换结束）、bit1 INDEX（测量结束）、bit5 ERR（测量异常）；`:ESR0?` 读出并清零。
  状态字节（`*STB?`，读了不清零）的 bit0 ESB0 是「ESR0 与 ESE0 相与」的汇总：`:ESE0 1` 之后，测完一笔、`*CLS` 之前
  `*STB?` 是奇数。说明书的 `*STB?` 例子回的是 16（bit4 MAV，输出队列有消息），模拟仪表照这个样子总带上 MAV。
- `:INITiate:CONTinuous OFF` + `:TRIGger:SOURce IMMediate` 时 `:READ?` 触发一次测量、回读数；连续测量开着时 `:READ?`
  是执行错误（不回复）。`:FETCh?` 回最近一次的读数，不触发。开机缺省连续测量开着、自动量程开着、3 mΩ / 6 V 档。
- ΩV 模式回「电阻,电压」，格式按量程固定（3 / 30 / 300 mΩ 档是 `±**.****E-3` / `±***.***E-3` / `±****.**E-3`，
  6 V 档是 `±*.*****E+0`）：正数的符号位是空格，整数部分前面的 0 换成空格；超量程（±OF）回 1E+9、测量异常回 1E+10，
  按各档的尾数写法（如 300 mΩ 档 `1000.00E+6` / `+1000.00E+7`，6 V 档 `1.00000E+9` / `+1.00000E+10`）。
- `*IDN?` 回 `HIOKI,<型号>,0,<软件版本>`（序列号位固定是 0）；模拟仪表在序列号位报 ILCS-SIMULATOR，让 ILCS 认出是模拟器。

按模拟需要定下的：忙（`busy`）时改设置的命令按执行错误（EXE）处理、不生效（说明书里 EXE 包括「被正在进行的
其他操作挡住」）；探针没接触上（电芯 connected=False）与 `fail` 故障按测量异常回。仪器没有联锁输入，`interlock`
故障注入不了（验收判跳过）。不模拟比较器、统计、存储、零位调整与 EXT I/O。
"""
from __future__ import annotations

from typing import Any

from simulator.scpi import (
    Cell, ScpiError, ScpiMeter, boolean, choice, command_only, number, query_only,
)

EOM, INDEX, ERR = 1, 2, 32
PON = 128
SESR_BITS = {"command": 32, "execution": 16, "device": 8, "query": 4}
# 电阻量程（Ω）→ (整数位数, 小数位数, 指数)
RESISTANCE_FORMATS = {
    3e-3: (2, 4, -3), 30e-3: (3, 3, -3), 300e-3: (4, 2, -3),
    3.0: (2, 4, 0), 30.0: (3, 3, 0), 300.0: (4, 2, 0), 3000.0: (2, 4, 3),
}
# 电压量程（V）→ (整数位数, 小数位数)
VOLTAGE_FORMATS = {6.0: (1, 5), 60.0: (2, 4), 100.0: (3, 3), 300.0: (3, 3)}
MODELS = {
    "BT3562A": {"resistance": tuple(RESISTANCE_FORMATS), "voltage": (6.0, 60.0, 100.0)},
    "BT3563A": {"resistance": tuple(RESISTANCE_FORMATS), "voltage": (6.0, 60.0, 300.0)},
    "BT3562": {"resistance": tuple(RESISTANCE_FORMATS), "voltage": (6.0, 60.0)},
    "BT3563": {"resistance": tuple(RESISTANCE_FORMATS), "voltage": (6.0, 60.0, 300.0)},
    "BT3561A": {"resistance": tuple(RESISTANCE_FORMATS)[1:], "voltage": (6.0, 60.0)},
}


def fixed(value: float, integer_digits: int, decimals: int) -> str:
    """按说明书的定宽写法：符号位（正数是空格）+ 整数部分（前导 0 换成空格）+ 小数部分。"""
    body = f"{abs(value):0{integer_digits + 1 + decimals}.{decimals}f}"
    integer, _, fraction = body.partition(".")
    integer = (integer.lstrip("0") or "0").rjust(integer_digits)
    return f"{'-' if value < 0 else ' '}{integer}.{fraction}"


def special(integer_digits: int, decimals: int, power: int, sign: str) -> str:
    """超量程（power=9）与测量异常（power=10）：尾数写成本档的位数，指数相应调整。"""
    mantissa = "1" + "0" * (integer_digits - 1) + "." + "0" * decimals
    return f"{sign}{mantissa}E+{power - (integer_digits - 1)}"


class HiokiBT3562(ScpiMeter):
    VENDOR = "HIOKI"
    newline = "\r\n"
    unsupported = {"interlock": "BT3562 系列没有联锁输入：夹具门与安全回路在工位层面管，不经过这台仪表"}

    def __init__(self, cell: Cell | None = None, serial: str = "", firmware: str = "", *, model: str = "BT3562A"):
        if model not in MODELS:
            raise ValueError(f"不认识的型号 {model}；可选 {', '.join(MODELS)}")
        self.MODEL = model
        self.ranges = MODELS[model]
        self.sesr = PON
        self.esr0 = 0
        self.ese = 0
        self.ese0 = 0
        self.sre = 0
        super().__init__(cell, serial or "ILCS-SIMULATOR", firmware)
        self.reset()

    def default_firmware(self) -> str:
        return "V2.10"

    def reset(self) -> None:
        """出厂缺省（说明书 4.13「Initial Factory Default Settings」）。"""
        self.headers = False
        self.function = "RV"
        self.autorange = True
        self.resistance_range = self.ranges["resistance"][0]
        self.voltage_range = 6.0
        self.rate = "SLOW"
        self.trigger = "IMM"
        self.continuous = True
        self.error_timing = "ASYN"
        self.last: tuple[str, str] | None = None

    def record(self, error: ScpiError) -> None:
        self.sesr |= SESR_BITS.get(error.kind, 16)

    def busy_error(self) -> ScpiError:
        return ScpiError("execution", 0, "simulated busy")

    def commands(self):
        return [
            ("*IDN", self._idn), ("*RST", self._rst), ("*CLS", self._cls), ("*ESR", self._esr), ("*ESE", self._ese),
            ("*OPC", self._opc), ("*TST", self._tst), ("*STB", self._stb), ("*SRE", self._sre),
            ("ESR0", self._esr0), ("ESE0", self._ese0),
            ("SYSTem:HEADer", self._header), ("SYSTem:ERRor", self._error_timing),
            ("SYSTem:TERMinator", self._ignore), ("SYSTem:LOCal", self._ignore),
            ("FUNCtion", self._function), ("AUTorange", self._autorange),
            ("RESistance:RANGe", self._resistance_range), ("VOLTage:RANGe", self._voltage_range),
            ("SAMPle:RATE", self._rate), ("TRIGger:SOURce", self._trigger_source),
            ("INITiate:CONTinuous", self._continuous), ("INITiate[:IMMediate]", self._initiate),
            ("READ", self._read), ("FETCh", self._fetch),
        ]

    # ---------- 标准命令 ----------

    def _answer(self, name: str, value: str) -> str:
        """设置类查询的回复：`:SYSTem:HEADer ON` 时带上命令头（长写、大写）。"""
        return f":{name} {value}" if self.headers else value

    def _idn(self, query: bool, args: str) -> str:
        query_only(query)
        return f"{self.VENDOR},{self.MODEL},{self.serial},{self.firmware}"

    def _rst(self, query: bool, args: str) -> None:
        command_only(query)
        self.reset()

    def _cls(self, query: bool, args: str) -> None:
        command_only(query)
        self.sesr = 0
        self.esr0 = 0

    def _esr(self, query: bool, args: str) -> str:
        query_only(query)
        value, self.sesr = self.sesr, 0
        return str(value)

    def _ese(self, query: bool, args: str) -> str | None:
        if query:
            return str(self.ese)
        self.ese = int(number(args, 0, 255))
        return None

    def _opc(self, query: bool, args: str) -> str | None:
        return "1" if query else None

    def _tst(self, query: bool, args: str) -> str:
        query_only(query)
        return "0"

    def _stb(self, query: bool, args: str) -> str:
        query_only(query)
        summary = (1 if self.esr0 & self.ese0 else 0) | (32 if self.sesr & self.ese else 0)
        byte = summary | 16  # MAV：照说明书的例子，回复正在输出队列里
        if byte & self.sre & ~64:
            byte |= 64  # MSS
        return str(byte)

    def _sre(self, query: bool, args: str) -> str | None:
        if query:
            return str(self.sre)
        self.sre = int(number(args, 0, 255)) & ~64
        return None

    def _esr0(self, query: bool, args: str) -> str:
        query_only(query)
        value, self.esr0 = self.esr0, 0
        return self._answer("ESR0", str(value))

    def _ese0(self, query: bool, args: str) -> str | None:
        if query:
            return self._answer("ESE0", str(self.ese0))
        self.ese0 = int(number(args, 0, 255))
        return None

    # ---------- 设置 ----------

    def _header(self, query: bool, args: str) -> str | None:
        if query:
            return ":SYSTEM:HEADER ON" if self.headers else "OFF"
        self.headers = boolean(args)
        return None

    def _error_timing(self, query: bool, args: str) -> str | None:
        """`:SYSTem:ERRor` 在这台仪器上不是错误队列，是 EXT I/O 的 ERR 输出时机（同步 / 异步）。"""
        if query:
            return self._answer("SYSTEM:ERROR", "SYNCHRONOUS" if self.error_timing == "SYNC" else "ASYNCHRONOUS")
        self.error_timing = choice(args, {"SYNChronous": "SYNC", "ASYNchronous": "ASYN"})
        return None

    def _ignore(self, query: bool, args: str) -> None:
        return None

    def _function(self, query: bool, args: str) -> str | None:
        if query:
            return self._answer("FUNCTION", {"RV": "RV", "RES": "RESISTANCE", "VOLT": "VOLTAGE"}[self.function])
        self.function = choice(args, {"RV": "RV", "RESistance": "RES", "VOLTage": "VOLT"})
        return None

    def _autorange(self, query: bool, args: str) -> str | None:
        if query:
            return self._answer("AUTORANGE", "ON" if self.autorange else "OFF")
        self.autorange = boolean(args)
        return None

    def _resistance_range(self, query: bool, args: str) -> str | None:
        if query:
            digits, decimals, power = RESISTANCE_FORMATS[self.resistance_range]
            return self._answer("RESISTANCE:RANGE", f"{self.resistance_range / 10 ** power:.{decimals}f}E{power:+d}")
        value = number(args, 0, 3100)
        # 按要测的值挑一个够用的最小量程（说明书：:RES:RANG 120E-3 选 300 mΩ 档）
        self.resistance_range = next(level for level in self.ranges["resistance"] if value <= level * 31 / 30
                                     or level == self.ranges["resistance"][-1])
        self.autorange = False
        return None

    def _voltage_range(self, query: bool, args: str) -> str | None:
        if query:
            digits, decimals = VOLTAGE_FORMATS[self.voltage_range]
            return self._answer("VOLTAGE:RANGE", f"{self.voltage_range:.{decimals}f}E+0")
        value = abs(number(args, -300, 300))
        self.voltage_range = next(level for level in self.ranges["voltage"] if value <= level
                                  or level == self.ranges["voltage"][-1])
        self.autorange = False
        return None

    def _rate(self, query: bool, args: str) -> str | None:
        if query:
            return self._answer("SAMPLE:RATE", {"EXF": "EXFAST", "FAST": "FAST", "MED": "MEDIUM", "SLOW": "SLOW"}[self.rate])
        self.rate = choice(args, {"EXFast": "EXF", "FAST": "FAST", "MEDium": "MED", "SLOW": "SLOW"})
        return None

    def _trigger_source(self, query: bool, args: str) -> str | None:
        if query:
            return self._answer("TRIGGER:SOURCE", "IMMEDIATE" if self.trigger == "IMM" else "EXTERNAL")
        self.trigger = choice(args, {"IMMediate": "IMM", "EXTernal": "EXT"})
        return None

    def _continuous(self, query: bool, args: str) -> str | None:
        if query:
            return self._answer("INITIATE:CONTINUOUS", "ON" if self.continuous else "OFF")
        self.continuous = boolean(args)
        return None

    # ---------- 测量 ----------

    def _resistance_text(self, faulted: bool) -> str:
        if self.autorange:
            self.resistance_range = next((level for level in self.ranges["resistance"]
                                          if self.cell.ir_ohm <= level * 31 / 30), self.ranges["resistance"][-1])
        digits, decimals, power = RESISTANCE_FORMATS[self.resistance_range]
        if faulted:
            return special(digits, decimals, 10, "+")  # 测量异常：1E+10（Ω），按本档尾数写
        if abs(self.cell.ir_ohm) > self.resistance_range * 31 / 30:
            return special(digits, decimals, 9, "-" if self.cell.ir_ohm < 0 else " ")  # ±OF：1E+9
        return f"{fixed(self.cell.ir_ohm / 10 ** power, digits, decimals)}E{power:+d}"

    def _voltage_text(self, faulted: bool) -> str:
        if self.autorange:
            self.voltage_range = next((level for level in self.ranges["voltage"] if abs(self.cell.ocv_V) <= level),
                                      self.ranges["voltage"][-1])
        digits, decimals = VOLTAGE_FORMATS[self.voltage_range]
        if faulted:
            return special(digits, decimals, 10, "+")
        if abs(self.cell.ocv_V) > self.voltage_range:
            return special(digits, decimals, 9, "-" if self.cell.ocv_V < 0 else " ")
        return f"{fixed(self.cell.ocv_V, digits, decimals)}E+0"

    def _reading(self) -> str:
        resistance, voltage = self.last or (self._resistance_text(True), self._voltage_text(True))
        return {"RV": f"{resistance},{voltage}", "RES": resistance, "VOLT": voltage}[self.function]

    def _measure(self) -> None:
        faulted = self.fault == "fail" or not self.cell.connected
        self.last = (self._resistance_text(faulted), self._voltage_text(faulted))
        self.esr0 |= EOM | INDEX | (ERR if faulted else 0)
        self.motions += 1

    def _initiate(self, query: bool, args: str) -> None:
        command_only(query)
        if self.continuous:
            raise ScpiError("execution", 0, "continuous measurement is on")
        if self.trigger == "IMM":
            self._measure()

    def _read(self, query: bool, args: str) -> Any:
        query_only(query)
        if self.continuous:
            raise ScpiError("execution", 0, "continuous measurement is on")  # 执行错误、不回复
        if self.trigger != "IMM" or self.fault == "stuck":
            return None  # 等外部触发（TRIG 键 / EXT I/O）才测：不回复
        self._measure()
        return self.measured(self._reading())

    def _fetch(self, query: bool, args: str) -> str:
        query_only(query)
        return self._reading()

    def snapshot(self) -> dict[str, Any]:
        return {
            "function": self.function, "autorange": self.autorange, "resistance_range": self.resistance_range,
            "voltage_range": self.voltage_range, "rate": self.rate, "trigger": self.trigger,
            "continuous": self.continuous, "headers": self.headers, "sesr": self.sesr, "esr0": self.esr0,
            "ese0": self.ese0,
            "last": list(self.last) if self.last else None,
        }
