"""假 Keithley SourceMeter：2450（原生 SCPI 命令集）与 2400（老 SCPI）。只模拟「源 0 A、测电压」的开路电压检测用到的命令。

行为依据（出处见 README「参考」）：

- 2450，Reference Manual 2450-901-01 Rev. E：`:READ?` 测量、存进缓冲区（缺省 defbuffer1）、回最后一个读数；
  `:FETCh?` 回缓冲区里最新的读数，空缓冲区报错；`:TRACe:ACTual?` 回缓冲区读数个数；`:SYSTem:ERRor?` 没有错误时回
  `0,"No error;0;0 0"`；`*CLS` 清事件寄存器与事件日志；`*LANG?` 回 SCPI / TSP / SCPI2400；输出关断状态缺省 NORMal；
  超量程回 9.9e+37；Interlock 设成 On 时联锁没接通就打不开输出；改四线 / 前后面板端子时输出先关掉。
- 2400，User's Manual 2400S-900-01 Rev. G：输出关着（又没开自动关断）时 `:READ?` 报 +803、不测；联锁挡住输出报 +802；
  `:FORMat:ELEMents` 决定读数串里有哪些元素；数据缓冲区 `:TRACe:FEED:CONTrol NEXT` 时测量存进去、存满就停（回到 NEVer）；
  存储进行中改 `:TRACe:FEED` 报 +800；没有读数时取数报 -230；错误队列空时回 `0,"No error"`。

按模拟需要定下的（手册没写）：存储进行中改 `:TRACe:POINts` 也按 +800 处理；忙（`busy`）时改设置的命令记一条执行错误
（2450 记 -200，2400 记 -221）；2450 联锁
挡住输出时记 -200；2450 输出关着时 `:READ?` 回 0 V（HIMP 关断时端子是断开的）；夹具上没有电芯时读数停在电压限值上
（0 A 源开路时电压顶到限值）。模拟仪表的 Interlock 设置缺省是 On（当作现场把夹具盖开关接到了联锁上），
`interlock` 故障就是盖子开着。
"""
from __future__ import annotations

from collections import deque
import math
import time
from typing import Any

from simulator.scpi import (
    Cell, ScpiError, ScpiMeter, boolean, choice, command_only, number, query_only, unquote,
)

OVERFLOW = 9.9e37
NOT_A_NUMBER = 9.91e37


class _SourceMeter(ScpiMeter):
    """两台 SourceMeter 共用：错误队列、输出、源与测量的设置。"""

    def __init__(self, cell: Cell | None = None, serial: str = "", firmware: str = "", *,
                 interlock_enabled: bool = True):
        self.errors: deque[tuple[int, str]] = deque(maxlen=1000)
        self.interlock_enabled = interlock_enabled  # 存在非易失存储里：*RST 不动它
        self.started = time.monotonic()
        super().__init__(cell, serial, firmware)
        self.reset()

    def reset(self) -> None:
        self.output = False
        self.source_function = "VOLT"
        self.level = {"CURR": 0.0, "VOLT": 0.0}
        self.source_autorange = True
        self.source_range = 1e-4
        self.function = "CURR"
        self.volt_range = 20.0
        self.volt_autorange = True
        self.nplc = 1.0
        self.rsense = False
        self.terminals = "FRON"
        self.limited = False

    # ---------- 错误队列 ----------

    def record(self, error: ScpiError) -> None:
        self.errors.append((error.code, error.message))

    def _error_count(self, query: bool, args: str) -> str:
        query_only(query)
        return str(len(self.errors))

    def _clear_errors(self, query: bool, args: str) -> None:
        command_only(query)
        self.errors.clear()

    def _cls(self, query: bool, args: str) -> None:
        command_only(query)
        self.errors.clear()

    def _rst(self, query: bool, args: str) -> None:
        command_only(query)
        self.reset()

    def _opc(self, query: bool, args: str) -> str | None:
        return "1" if query else None

    def _idn(self, query: bool, args: str) -> str:
        query_only(query)
        return f"{self.VENDOR},MODEL {self.MODEL},{self.serial},{self.firmware}"

    # ---------- 输出 ----------

    def interlock_blocked(self) -> bool:
        """Interlock 设成 On、联锁信号没接通（夹具盖开着）：输出打不开。"""
        return self.interlock_enabled and self.fault == "interlock"

    def blocked_error(self) -> ScpiError:
        raise NotImplementedError

    def _output(self, query: bool, args: str) -> str | None:
        if query:
            return "1" if self.output else "0"
        on = boolean(args)
        if on and self.interlock_blocked():
            raise self.blocked_error()
        self.output = on
        return None

    def _interlock_state(self, query: bool, args: str) -> str | None:
        if query:
            return "1" if self.interlock_enabled else "0"
        self.interlock_enabled = boolean(args)
        return None

    def _interlock_tripped(self, query: bool, args: str) -> str:
        """两台都是 1 = 联锁信号接通（输出可以打开），0 = 没接通。"""
        query_only(query)
        return "0" if self.fault == "interlock" else "1"

    def _terminals(self, query: bool, args: str) -> str | None:
        if query:
            return self.terminals
        selected = choice(args, {"FRONt": "FRON", "REAR": "REAR"})
        if selected != self.terminals:
            self.output = False  # 换端子时输出先关掉
        self.terminals = selected
        return None

    # ---------- 源 ----------

    def _source_function(self, query: bool, args: str) -> str | None:
        if query:
            return self.source_function
        self.source_function = choice(args, {"CURRent[:DC]": "CURR", "VOLTage[:DC]": "VOLT"})
        return None

    def _current_level(self, query: bool, args: str) -> str | None:
        if query:
            return f"{self.level['CURR']:.6E}"
        self.level["CURR"] = number(args, -1.05, 1.05, named={"MINimum": -1.05, "MAXimum": 1.05, "DEFault": 0.0})
        return None

    def _source_autorange(self, query: bool, args: str) -> str | None:
        if query:
            return "1" if self.source_autorange else "0"
        self.source_autorange = boolean(args)
        return None

    def _source_range(self, query: bool, args: str) -> str | None:
        if query:
            return f"{self.source_range:.6E}"
        self.source_range = number(args, 0, 1.05, named={"MINimum": 1e-8, "MAXimum": 1.05, "DEFault": 1e-4})
        self.source_autorange = False
        return None

    # ---------- 测量 ----------

    def _volt_autorange(self, query: bool, args: str) -> str | None:
        if query:
            return "1" if self.volt_autorange else "0"
        self.volt_autorange = boolean(args)
        return None

    def _nplc(self, query: bool, args: str) -> str | None:
        if query:
            return f"{self.nplc:.6E}"
        self.nplc = number(args, 0.01, 10, named={"MINimum": 0.01, "MAXimum": 10, "DEFault": 1})
        return None

    def compliance(self) -> float:
        raise NotImplementedError

    def voltage(self) -> float:
        """源 0 A、测电压：夹具上的电芯电压；电压超过限值就钳在限值上，没接电芯也顶到限值。"""
        if self.fault == "fail":
            return OVERFLOW
        if self.source_function != "CURR":
            return self.level["VOLT"]
        limit = self.compliance()
        value = self.cell.ocv_V if self.cell.connected else limit
        self.limited = abs(value) >= limit
        if self.limited:
            value = math.copysign(limit, value)
        if not self.volt_autorange and abs(value) > self.volt_range * 1.05:
            return OVERFLOW
        return value

    def snapshot(self) -> dict[str, Any]:
        return {
            "output": self.output, "source_function": self.source_function, "source_level": self.level["CURR"],
            "compliance": self.compliance(), "function": self.function, "volt_range": self.volt_range,
            "volt_autorange": self.volt_autorange, "nplc": self.nplc, "rsense": self.rsense,
            "terminals": self.terminals, "errors": [f"{code},{message}" for code, message in self.errors],
        }


class Keithley2450(_SourceMeter):
    """Keithley 2450 SourceMeter，`*LANG SCPI`（出厂缺省）。`language` 可以设成 TSP / SCPI2400 看命令集不对时的样子：
    除通用命令（*IDN? 之类）外一律报错。"""

    MODEL = "2450"
    VENDOR = "KEITHLEY INSTRUMENTS"
    newline = "\n"
    VOLT_RANGES = (0.02, 0.2, 2.0, 20.0, 200.0)
    PROTECTION = {f"PROT{level}": float(level) for level in (2, 5, 10, 20, 40, 60, 80, 100, 120, 140, 160, 180)}

    def __init__(self, cell: Cell | None = None, serial: str = "", firmware: str = "", *, language: str = "SCPI",
                 interlock_enabled: bool = True):
        self.language = language
        self.buffers: dict[str, list[float]] = {"defbuffer1": [], "defbuffer2": []}
        super().__init__(cell, serial, firmware, interlock_enabled=interlock_enabled)

    def default_firmware(self) -> str:
        return "1.7.12b"

    def reset(self) -> None:
        super().reset()
        self.off_mode = {"CURR": "NORM", "VOLT": "NORM"}
        self.vlimit = 21.0
        self.protection: float | None = None
        self.count = 1
        for readings in self.buffers.values():
            readings.clear()

    def compliance(self) -> float:
        return self.vlimit

    def accepts(self, name: str, query: bool) -> ScpiError | None:
        if self.language != "SCPI" and not name.startswith("*"):
            return ScpiError("command", -285, "TSP Syntax error at line 1: unexpected symbol")
        return super().accepts(name, query)

    def busy_error(self) -> ScpiError:
        return ScpiError("execution", -200, "Execution error; simulated busy (front panel or another controller)")

    def blocked_error(self) -> ScpiError:
        return ScpiError("execution", -200, "Execution error; simulated interlock not asserted, output stays off")

    def commands(self):
        return [
            ("*IDN", self._idn), ("*LANG", self._lang), ("*CLS", self._cls), ("*RST", self._rst), ("*OPC", self._opc),
            ("SYSTem:ERRor[:NEXT]", self._error_next), ("SYSTem:ERRor:COUNt", self._error_count),
            ("SYSTem:CLEar", self._clear_errors),
            ("OUTPut[1][:STATe]", self._output),
            ("OUTPut[1]:CURRent[:DC]:SMODe", lambda query, args: self._off_mode("CURR", query, args)),
            ("OUTPut[1]:VOLTage[:DC]:SMODe", lambda query, args: self._off_mode("VOLT", query, args)),
            ("OUTPut[1]:INTerlock:STATe", self._interlock_state),
            ("OUTPut[1]:INTerlock:TRIPped", self._interlock_tripped),
            ("ROUTe:TERMinals", self._terminals),
            ("SOURce[1]:FUNCtion[:MODE]", self._source_function),
            ("SOURce[1]:CURRent[:LEVel][:IMMediate][:AMPLitude]", self._current_level),
            ("SOURce[1]:CURRent:RANGe:AUTO", self._source_autorange),
            ("SOURce[1]:CURRent:RANGe", self._source_range),
            ("SOURce[1]:CURRent:VLIMit[:LEVel]", self._vlimit),
            ("SOURce[1]:CURRent:VLIMit[:LEVel]:TRIPped", self._vlimit_tripped),
            ("SOURce[1]:VOLTage:PROTection[:LEVel]", self._protection),
            ("[SENSe[1]:]FUNCtion[:ON]", self._sense_function),
            ("[SENSe[1]:]VOLTage[:DC]:RANGe[:UPPer]", self._volt_range),
            ("[SENSe[1]:]VOLTage[:DC]:RANGe:AUTO", self._volt_autorange),
            ("[SENSe[1]:]VOLTage[:DC]:NPLCycles", self._nplc),
            ("[SENSe[1]:]VOLTage[:DC]:RSENse", self._rsense),
            ("[SENSe[1]:]COUNt", self._count),
            ("READ", self._read),
            ("FETCh", self._fetch),
            ("TRACe:ACTual", self._trace_actual),
            ("TRACe:CLEar", self._trace_clear),
            ("ABORt", self._abort),
        ]

    # ---------- 2450 自己的命令 ----------

    def _lang(self, query: bool, args: str) -> str | None:
        if query:
            return self.language
        self.language = choice(args, {"SCPI": "SCPI", "TSP": "TSP", "SCPI2400": "SCPI2400"})  # 真表要重启才生效
        return None

    def _error_next(self, query: bool, args: str) -> str:
        query_only(query)
        if not self.errors:
            return '0,"No error;0;0 0"'
        code, message = self.errors.popleft()
        return f'{code},"{message};1;{time.strftime("%Y/%m/%d %H:%M:%S")}.000"'

    def _off_mode(self, function: str, query: bool, args: str) -> str | None:
        if query:
            return self.off_mode[function]
        self.off_mode[function] = choice(args, {"NORMal": "NORM", "HIMPedance": "HIMP", "ZERO": "ZERO", "GUARd": "GUAR"})
        return None

    def _vlimit(self, query: bool, args: str) -> str | None:
        if query:
            return f"{self.vlimit:.6E}"
        value = number(args, 0.02, 210, named={"MINimum": 0.02, "MAXimum": 210, "DEFault": 21})
        # 电压限值受过压保护约束：设得比保护值高就按保护值（真表另报一条警告）
        self.vlimit = min(value, self.protection) if self.protection is not None else value
        return None

    def _vlimit_tripped(self, query: bool, args: str) -> str:
        query_only(query)
        return "1" if self.limited else "0"

    def _protection(self, query: bool, args: str) -> str | None:
        if query:
            return next((name for name, level in self.PROTECTION.items() if level == self.protection), "NONE")
        selected = choice(args, {**{name: name for name in self.PROTECTION}, "NONE": "NONE"})
        self.protection = self.PROTECTION.get(selected)
        if self.protection is not None:
            self.vlimit = min(self.vlimit, self.protection)
        return None

    def _sense_function(self, query: bool, args: str) -> str | None:
        if query:
            return {"CURR": '"CURR:DC"', "VOLT": '"VOLT:DC"', "RES": '"RES"'}[self.function]
        self.function = choice(unquote(args), {"CURRent[:DC]": "CURR", "VOLTage[:DC]": "VOLT", "RESistance": "RES"})
        return None

    def _volt_range(self, query: bool, args: str) -> str | None:
        if query:
            return f"{self.volt_range:.6E}"
        value = abs(number(args, -210, 210, named={"MINimum": 0.02, "MAXimum": 200, "DEFault": 0.2}))
        self.volt_range = next(level for level in self.VOLT_RANGES if value <= level * 1.05 or level == 200.0)
        self.volt_autorange = False
        return None

    def _rsense(self, query: bool, args: str) -> str | None:
        if query:
            return "1" if self.rsense else "0"
        selected = boolean(args)
        if selected != self.rsense:
            self.output = False  # 输出开着时改四线 / 两线，输出先关掉
        self.rsense = selected
        return None

    def _count(self, query: bool, args: str) -> str | None:
        if query:
            return str(self.count)
        self.count = int(number(args, 1, 300000, named={"MINimum": 1, "MAXimum": 300000, "DEFault": 1}))
        return None

    def _buffer(self, args: str) -> list[float]:
        name = unquote(args.split(",")[0]) if args else "defbuffer1"
        if name not in self.buffers:
            raise ScpiError("execution", -224, "Illegal parameter value; reading buffer not found")
        return self.buffers[name]

    def _read(self, query: bool, args: str) -> Any:
        query_only(query)
        readings = self._buffer(args)
        if self.fault == "stuck":
            return None
        if self.function != "VOLT":
            value = NOT_A_NUMBER
        elif not self.output:
            value = 0.0
        else:
            value = self.voltage()
        readings.extend([value] * self.count)
        self.motions += 1
        return self.measured(f"{value:.6E}")

    def _fetch(self, query: bool, args: str) -> str:
        query_only(query)
        readings = self._buffer(args)
        if not readings:
            raise ScpiError("execution", -230, "Data corrupt or stale; reading buffer is empty")
        return f"{readings[-1]:.6E}"

    def _trace_actual(self, query: bool, args: str) -> str:
        query_only(query)
        return str(len(self._buffer(args)))

    def _trace_clear(self, query: bool, args: str) -> None:
        command_only(query)
        self._buffer(args).clear()

    def _abort(self, query: bool, args: str) -> None:
        command_only(query)

    def snapshot(self) -> dict[str, Any]:
        return {
            **super().snapshot(), "language": self.language, "off_mode": dict(self.off_mode),
            "vlimit": self.vlimit, "protection": self.protection, "count": self.count,
            "readings": len(self.buffers["defbuffer1"]),
        }


class Keithley2400(_SourceMeter):
    """Keithley 2400 SourceMeter（老 SCPI，RS-232 出厂 9600 8N1、结束符 CR）。"""

    MODEL = "2400"
    VENDOR = "KEITHLEY INSTRUMENTS INC."
    newline = "\r"
    VOLT_RANGES = (0.21, 2.1, 21.0, 210.0)

    def default_firmware(self) -> str:
        return "C32   Oct  4 2010 14:20:11/A02  /S/K"

    def reset(self) -> None:
        super().reset()
        self.off_mode = "NORM"
        self.auto_off = False
        self.source_mode = "FIX"
        self.vprotection = 21.0
        self.concurrent = True
        self.elements = ["VOLT", "CURR", "RES", "TIME", "STAT"]
        self.trigger_count = 1
        self.arm_count = 1
        self.delay = 0.0
        self.trace: list[float] = []
        self.trace_points = 100
        self.trace_feed = "SENS"
        self.trace_control = "NEV"
        self.sample: list[float] | None = None

    def compliance(self) -> float:
        return self.vprotection

    def busy_error(self) -> ScpiError:
        return ScpiError("execution", -221, "Settings conflict")

    def blocked_error(self) -> ScpiError:
        return ScpiError("execution", 802, "OUTPUT blocked by interlock")

    def commands(self):
        return [
            ("*IDN", self._idn), ("*CLS", self._cls), ("*RST", self._rst), ("*OPC", self._opc),
            ("SYSTem:ERRor[:NEXT]", self._error_next), ("SYSTem:ERRor:COUNt", self._error_count),
            ("SYSTem:CLEar", self._clear_errors), ("SYSTem:RSENse", self._rsense),
            ("OUTPut[1][:STATe]", self._output), ("OUTPut[1]:SMODe", self._off_mode),
            ("OUTPut[1]:INTerlock:STATe", self._interlock_state),
            ("OUTPut[1]:INTerlock:TRIPped", self._interlock_tripped),
            ("ROUTe:TERMinals", self._terminals),
            ("SOURce[1]:FUNCtion[:MODE]", self._source_function),
            ("SOURce[1]:CURRent:MODE", self._source_mode),
            ("SOURce[1]:CURRent:RANGe:AUTO", self._source_autorange),
            ("SOURce[1]:CURRent:RANGe[:UPPer]", self._source_range),
            ("SOURce[1]:CURRent[:LEVel][:IMMediate][:AMPLitude]", self._current_level),
            ("SOURce[1]:CLEar:AUTO", self._auto_off),
            ("SOURce[1]:CLEar[:IMMediate]", self._output_off),
            ("SOURce[1]:DELay", self._delay),
            ("[SENSe[1]:]FUNCtion:CONCurrent", self._concurrent),
            ("[SENSe[1]:]FUNCtion[:ON]", self._sense_function),
            ("[SENSe[1]:]VOLTage[:DC]:PROTection[:LEVel]", self._compliance),
            ("[SENSe[1]:]VOLTage[:DC]:RANGe[:UPPer]", self._volt_range),
            ("[SENSe[1]:]VOLTage[:DC]:RANGe:AUTO", self._volt_autorange),
            ("[SENSe[1]:]VOLTage[:DC]:NPLCycles", self._nplc),
            ("FORMat:ELEMents[:SENSe[1]]", self._elements),
            ("TRIGger[:SEQuence[1]]:COUNt", self._trigger_count),
            ("ARM[:SEQuence[1]][:LAYer[1]]:COUNt", self._arm_count),
            ("TRACe:FEED:CONTrol", self._feed_control), ("TRACe:FEED", self._feed),
            ("TRACe:POINts", self._points), ("TRACe:POINts:ACTual", self._points_actual),
            ("TRACe:CLEar", self._trace_clear), ("TRACe:DATA", self._trace_data),
            ("READ", self._read), ("FETCh", self._fetch), ("INITiate[:IMMediate]", self._initiate),
            ("ABORt", self._abort),
        ]

    # ---------- 2400 自己的命令 ----------

    def _error_next(self, query: bool, args: str) -> str:
        query_only(query)
        if not self.errors:
            return '0,"No error"'
        code, message = self.errors.popleft()
        return f'{code:+d},"{message}"'

    def _rsense(self, query: bool, args: str) -> str | None:
        if query:
            return "1" if self.rsense else "0"
        self.rsense = boolean(args)
        return None

    def _off_mode(self, query: bool, args: str) -> str | None:
        if query:
            return self.off_mode
        self.off_mode = choice(args, {"HIMPedance": "HIMP", "NORMal": "NORM", "ZERO": "ZERO", "GUARd": "GUAR"})
        return None

    def _source_mode(self, query: bool, args: str) -> str | None:
        if query:
            return self.source_mode
        self.source_mode = choice(args, {"FIXed": "FIX", "LIST": "LIST", "SWEep": "SWE"})
        return None

    def _auto_off(self, query: bool, args: str) -> str | None:
        if query:
            return "1" if self.auto_off else "0"
        self.auto_off = boolean(args)
        return None

    def _output_off(self, query: bool, args: str) -> None:
        command_only(query)
        self.output = False

    def _delay(self, query: bool, args: str) -> str | None:
        if query:
            return f"{self.delay:+.6E}"
        self.delay = number(args, 0, 9999.999, named={"MINimum": 0, "MAXimum": 9999.999, "DEFault": 0})
        return None

    def _concurrent(self, query: bool, args: str) -> str | None:
        if query:
            return "1" if self.concurrent else "0"
        self.concurrent = boolean(args)
        if not self.concurrent:
            self.function = "VOLT"  # 关掉并行测量时只剩电压测量
        return None

    def _sense_function(self, query: bool, args: str) -> str | None:
        if query:
            return {"CURR": '"CURR:DC"', "VOLT": '"VOLT:DC"', "RES": '"RES"'}[self.function]
        names = [choice(item, {"CURRent[:DC]": "CURR", "VOLTage[:DC]": "VOLT", "RESistance": "RES"})
                 for item in args.split(",") if item.strip()]
        if not names:
            raise ScpiError("command", -109, "Missing parameter")
        self.function = names[-1]
        return None

    def _compliance(self, query: bool, args: str) -> str | None:
        if query:
            return f"{self.vprotection:+.6E}"
        self.vprotection = abs(number(args, -210, 210, named={"MINimum": 0.0002, "MAXimum": 210, "DEFault": 21}))
        return None

    def _volt_range(self, query: bool, args: str) -> str | None:
        if query:
            return f"{self.volt_range:+.6E}"
        value = abs(number(args, -210, 210, named={"MINimum": 0.21, "MAXimum": 210, "DEFault": 21}))
        self.volt_range = next(level for level in self.VOLT_RANGES if value <= level or level == 210.0)
        self.volt_autorange = False
        return None

    def _elements(self, query: bool, args: str) -> str | None:
        if query:
            return ",".join(self.elements)
        options = {"VOLTage": "VOLT", "CURRent": "CURR", "RESistance": "RES", "TIME": "TIME", "STATus": "STAT"}
        selected = [choice(item, options) for item in args.split(",") if item.strip()]
        if not selected:
            raise ScpiError("command", -109, "Missing parameter")
        self.elements = selected
        return None

    def _trigger_count(self, query: bool, args: str) -> str | None:
        if query:
            return f"{self.trigger_count:+d}"
        self.trigger_count = int(number(args, 1, 2500, named={"MINimum": 1, "MAXimum": 2500, "DEFault": 1}))
        return None

    def _arm_count(self, query: bool, args: str) -> str | None:
        if query:
            return f"{self.arm_count:+d}"
        self.arm_count = int(number(args, 1, 2500, named={"MINimum": 1, "MAXimum": 2500, "DEFault": 1}))
        return None

    def _storing(self) -> bool:
        return self.trace_control == "NEXT" and len(self.trace) < self.trace_points

    def _feed_control(self, query: bool, args: str) -> str | None:
        if query:
            return "NEXT" if self.trace_control == "NEXT" else "NEV"
        self.trace_control = choice(args, {"NEXT": "NEXT", "NEVer": "NEV"})
        return None

    def _feed(self, query: bool, args: str) -> str | None:
        if query:
            return self.trace_feed
        if self._storing():
            raise ScpiError("execution", 800, "Illegal with storage active")
        self.trace_feed = choice(args, {"SENSe[1]": "SENS", "CALCulate[1]": "CALC1", "CALCulate2": "CALC2"})
        return None

    def _points(self, query: bool, args: str) -> str | None:
        if query:
            return f"{self.trace_points:+d}"
        if self._storing():
            raise ScpiError("execution", 800, "Illegal with storage active")
        self.trace_points = int(number(args, 1, 2500, named={"MINimum": 1, "MAXimum": 2500, "DEFault": 100}))
        return None

    def _points_actual(self, query: bool, args: str) -> str:
        query_only(query)
        return str(len(self.trace))

    def _trace_clear(self, query: bool, args: str) -> None:
        command_only(query)
        self.trace.clear()

    def _trace_data(self, query: bool, args: str) -> str:
        query_only(query)
        if not self.trace:
            raise ScpiError("execution", -230, "Data corrupt or stale")
        return ",".join(self._format(value) for value in self.trace)

    def _format(self, voltage: float) -> str:
        """一组读数按 :FORMat:ELEMents 排：电压、电流（源 0 A 时是源值）、电阻（没测是 NAN）、时间戳、状态字。"""
        values = {
            "VOLT": voltage, "CURR": self.level["CURR"] if self.source_function == "CURR" else 0.0,
            "RES": NOT_A_NUMBER, "TIME": time.monotonic() - self.started, "STAT": 0.0,
        }
        return ",".join(f"{values[name]:+.6E}" for name in self.elements)

    def _take(self) -> list[float]:
        """一个源—延时—测量周期：输出得开着（或开了自动关断），读数进采样缓冲区，开着数据缓冲区就存进去。"""
        if not self.output:
            if not self.auto_off:
                raise ScpiError("execution", 803, "Not permitted with OUTPUT off")
            if self.interlock_blocked():
                raise self.blocked_error()
        value = self.voltage() if self.function == "VOLT" else NOT_A_NUMBER
        readings = [value] * max(1, self.trigger_count * self.arm_count)
        self.sample = readings
        if self.trace_control == "NEXT":
            for reading in readings:
                if len(self.trace) < self.trace_points:
                    self.trace.append(reading)
            if len(self.trace) >= self.trace_points:
                self.trace_control = "NEV"  # 存满就停
        self.motions += 1
        return readings

    def _read(self, query: bool, args: str) -> Any:
        query_only(query)
        if self.fault == "stuck":
            return None
        readings = self._take()
        return self.measured(",".join(self._format(value) for value in readings))

    def _initiate(self, query: bool, args: str) -> None:
        command_only(query)
        self._take()

    def _fetch(self, query: bool, args: str) -> str:
        query_only(query)
        if not self.sample:
            raise ScpiError("execution", -230, "Data corrupt or stale")
        return ",".join(self._format(value) for value in self.sample)

    def _abort(self, query: bool, args: str) -> None:
        command_only(query)

    def snapshot(self) -> dict[str, Any]:
        return {
            **super().snapshot(), "off_mode": self.off_mode, "auto_off": self.auto_off, "elements": list(self.elements),
            "trace": list(self.trace), "trace_control": self.trace_control, "trace_points": self.trace_points,
            "readings": len(self.trace),
        }
