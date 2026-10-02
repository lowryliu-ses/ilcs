"""假仪表自己的行为：命令头的长短写法、错误怎么记、读数格式照手册、TCP 口的结束符、丢回复与失联。"""
from __future__ import annotations

import socket
import time

import pytest

from simulator.hioki import HiokiBT3562, fixed, special
from simulator.keithley import Keithley2400, Keithley2450
from simulator.scpi import CLOSE, Cell, header
from simulator.server import MeterServer


@pytest.mark.parametrize(("spec", "text", "matches"), [
    ("[SENSe[1]:]VOLTage[:DC]:NPLCycles", "SENS:VOLT:NPLC", True),
    ("[SENSe[1]:]VOLTage[:DC]:NPLCycles", "VOLT:NPLC", True),
    ("[SENSe[1]:]VOLTage[:DC]:NPLCycles", "sense1:voltage:dc:nplcycles", True),
    ("[SENSe[1]:]VOLTage[:DC]:NPLCycles", "SENS:VOLTA:NPLC", False),  # 只认短写或长写
    ("SOURce[1]:CURRent[:LEVel][:IMMediate][:AMPLitude]", "SOUR:CURR", True),
    ("SOURce[1]:CURRent[:LEVel][:IMMediate][:AMPLitude]", "SOUR:CURR:RANG", False),
    ("*IDN", "*idn", True),
    ("ESR0", "ESR0", True),
])
def test_headers_accept_short_and_long_forms(spec, text, matches):
    assert bool(header(spec).fullmatch(text)) is matches


def test_keithley_2450_error_queue_and_buffer():
    meter = Keithley2450()
    assert meter.handle("*IDN?") == "KEITHLEY INSTRUMENTS,MODEL 2450,ILCS-SIMULATOR-2450-01,1.7.12b"
    assert meter.handle(":SYST:ERR?") == '0,"No error;0;0 0"'
    assert meter.handle(":BOGUS 1") is None
    assert meter.handle(":SYST:ERR?").startswith('-113,"Undefined header;1;')
    assert meter.handle(':FETC? "defbuffer1"') is None, "空缓冲区：报错、不回复"
    assert meter.handle(":SYST:ERR?").startswith("-230,")
    assert meter.handle(":SOUR:CURR:VLIM 500") is None and meter.handle(":SYST:ERR?").startswith("-222,")
    meter.handle(":SOUR:FUNC CURR")
    meter.handle(':SENS:FUNC "VOLT"')
    meter.handle(":OUTP ON")
    assert meter.handle(":READ?") == "3.850000E+00" and meter.handle(":TRAC:ACT?") == "1"
    assert meter.handle(':FETC? "defbuffer1"') == "3.850000E+00"
    meter.handle(':TRAC:CLE "defbuffer1"')
    assert meter.handle(':TRAC:ACT? "defbuffer1"') == "0"
    meter.handle(":SENS:VOLT:RSEN ON")
    assert meter.state()["output"] is False, "输出开着时改四线 / 两线，输出先关掉"


def test_keithley_2450_overflow_and_voltage_limit():
    meter = Keithley2450(Cell(ocv_V=12.0))
    for line in (":SOUR:FUNC CURR", ":SOUR:CURR 0", ":SOUR:CURR:VLIM 10", ':SENS:FUNC "VOLT"', ":SENS:VOLT:RANG 20",
                 ":OUTP ON"):
        meter.handle(line)
    assert meter.handle(":READ?") == "1.000000E+01" and meter.handle(":SOUR:CURR:VLIM:TRIP?") == "1"
    meter.handle(":SENS:VOLT:RANG 2")
    meter.cell.ocv_V = 3.85
    assert meter.handle(":READ?") == "9.900000E+37", "固定量程超量程回 9.9e+37"


def test_keithley_2400_needs_the_output_on_and_honours_the_interlock():
    meter = Keithley2400()
    assert meter.handle(":READ?") is None and meter.handle(":SYST:ERR?") == '+803,"Not permitted with OUTPUT off"'
    meter.set_fault("interlock")
    meter.handle(":OUTP ON")
    assert meter.handle(":SYST:ERR?") == '+802,"OUTPUT blocked by interlock"' and meter.handle(":OUTP?") == "0"
    assert meter.handle(":OUTP:INT:TRIP?") == "0"
    meter.set_fault("none")
    meter.handle(":OUTP ON")
    assert meter.handle(":READ?").count(",") == 4, "缺省五个元素：电压、电流、电阻、时间戳、状态字"
    meter.handle(":TRAC:POIN 1")
    meter.handle(":TRAC:FEED:CONT NEXT")
    meter.handle(":TRAC:FEED SENS")
    assert meter.handle(":SYST:ERR?") == '+800,"Illegal with storage active"'


def test_hioki_number_formats_follow_the_manual():
    # 说明书：_0001.36E-3 → ____1.36E-3；-0007.51E+0 → -___7.51E+0（_ 是空格）
    assert fixed(1.36, 4, 2) + "E-3" == "    1.36E-3"
    assert fixed(-7.51, 4, 2) + "E+0" == "-   7.51E+0"
    assert special(4, 2, 9, " ") == " 1000.00E+6" and special(4, 2, 10, "+") == "+1000.00E+7"  # 300 mΩ 档 ±OF / 异常
    assert special(1, 5, 9, " ") == " 1.00000E+9" and special(1, 5, 10, "+") == "+1.00000E+10"  # 6 V 档
    assert special(2, 4, 9, "-") == "-10.0000E+8"  # 3 mΩ 档 -OF


def test_hioki_registers_and_trigger_modes():
    meter = HiokiBT3562()
    assert meter.handle("*IDN?") == "HIOKI,BT3562A,ILCS-SIMULATOR,V2.10"
    assert meter.handle("*ESR?") == "128" and meter.handle("*ESR?") == "0", "开机 PON，读一次就清"
    assert meter.handle(":READ?") is None and meter.handle("*ESR?") == "16", "连续测量开着时 READ? 是执行错误、不回复"
    assert meter.handle(":BOGUS") is None and meter.handle("*ESR?") == "32"
    assert meter.handle(":SYST:ERR?") == "ASYNCHRONOUS", ":SYSTem:ERRor 在这台仪器上是 ERR 输出时机，不是错误队列"
    for line in (":ESE0 1", ":AUT OFF", ":RES:RANG 300E-3", ":VOLT:RANG 6", ":TRIG:SOUR IMM", ":INIT:CONT OFF"):
        meter.handle(line)
    assert meter.handle(":RES:RANG?") == "300.00E-3" and meter.handle(":VOLT:RANG?") == "6.00000E+0"
    assert meter.handle("*STB?") == "16"
    assert meter.handle(":READ?") == "   15.00E-3, 3.85000E+0"
    assert meter.handle("*STB?") == "17" and meter.handle("*STB?") == "17", "*STB? 读了不清零"
    assert meter.handle(":FETC?") == "   15.00E-3, 3.85000E+0"
    assert meter.handle(":ESR0?") == "3" and meter.handle(":ESR0?") == "0", ":ESR0? 读了就清"
    meter.handle(":RES:RANG 120E-3")
    assert meter.state()["resistance_range"] == 0.3, "按要测的值挑够用的最小量程（说明书的例子）"


def _send(port: int, line: bytes, newline: bytes, timeout: float = 2.0) -> bytes:
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        sock.sendall(line)
        data = b""
        while not data.endswith(newline):
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
        return data


@pytest.mark.parametrize(("meter", "terminator"), [
    (Keithley2450(), b"\n"), (Keithley2400(), b"\r"), (HiokiBT3562(), b"\r\n"),
])
def test_server_takes_cr_lf_or_crlf_and_answers_with_the_model_terminator(meter, terminator):
    server = MeterServer(meter).start()
    try:
        for ending in (b"\n", b"\r", b"\r\n"):
            reply = _send(server.port, b"*IDN?" + ending, meter.newline.encode())
            assert reply.endswith(meter.newline.encode()) and b"ILCS-SIMULATOR" in reply
        assert meter.newline.encode() == terminator
    finally:
        server.stop()


def test_lost_receipt_measures_then_drops_the_connection():
    meter = Keithley2450()
    for line in (":SOUR:FUNC CURR", ':SENS:FUNC "VOLT"', ":OUTP ON"):
        meter.handle(line)
    meter.set_fault("lost_receipt")
    assert meter.handle(":READ?") is CLOSE and meter.motions == 1 and meter.handle(":TRAC:ACT?") == "1"
    server = MeterServer(meter).start()
    try:
        assert _send(server.port, b":READ?\n", b"\n") == b"", "回复不发、连接断开"
    finally:
        server.stop()


def test_offline_stops_listening_then_comes_back_on_the_same_port():
    server = MeterServer(Keithley2450()).start()
    port = server.port
    try:
        server.go_offline(1.5)
        time.sleep(0.6)
        with pytest.raises(OSError):
            _send(port, b"*IDN?\n", b"\n", timeout=0.5)
        deadline = time.monotonic() + 5
        while True:
            try:
                assert b"MODEL 2450" in _send(port, b"*IDN?\n", b"\n")
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.1)
    finally:
        server.stop()
