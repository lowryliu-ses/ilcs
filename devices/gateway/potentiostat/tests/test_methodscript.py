"""MethodSCRIPT 编解码：用 PalmSens 文档里的例子核对（MethodSCRIPT 手册 v1.8 第 4–6、9、11 章，EmStat4 / EmStat Pico
通讯协议），再核对网关写出来的脚本——假仪器的语法检查要全收，写错的要按手册的错误码拒收。

    pytest devices/gateway/potentiostat/tests/test_methodscript.py
"""
from __future__ import annotations

import math

import pytest

from driver import methodscript as ms
from driver.backend import Plan, Ranging
from driver.palmsens import build_script, silence_limit
from driver.techniques import check_settings, Cell
from simulator.fake_methodscript import ScriptError, parse_script


# ---------- 数据包里的值 ----------

@pytest.mark.parametrize(("field", "value"), [
    ("800000Am", 0.01),          # 手册 4.3：0.01 = 800000Am
    ("7FFFFF6m", -0.01),         # 手册 4.3：-0.01 = 7FFFFF6m
    ("8000800u", 2048e-6),       # 手册 5.3：08000800 − 0x8000000 = 2048，前缀 u
    ("DF5CB18n", 0.099994392),   # 手册 6.3：设定 100 mV，DAC 分辨率下实际 0.099994392 V
    ("8000000 ", 0.0),           # 值为 0 时前缀是空格（手册 9.3 的 nscans 输出）
    ("8000000", 0.0),            # 行尾的空格被裁掉了也认
    ("9AE0ABCf", 2.8183228e-8),
])
def test_decode_values_from_the_manual(field, value):
    assert ms.decode_value(field) == pytest.approx(value, rel=1e-12, abs=1e-24)


def test_integer_and_nan_values():
    assert ms.decode_value("8000001i") == 1 and isinstance(ms.decode_value("8000001i"), int)
    assert math.isnan(ms.decode_value("     nan"))  # 手册 4.3.1：5 个空格 + nan
    with pytest.raises(ms.PackageError):
        ms.decode_value("80000G0u")
    with pytest.raises(ms.PackageError):
        ms.decode_value("8000000x")


def test_package_parsing_example_from_the_manual():
    """手册 5.3：Pda8000800u;ba8000800u,10,20B —— 设定电位 2.048 mV；电流 2.048 mA，状态 OK，量程号 0x0B。"""
    potential, current = ms.parse_package("Pda8000800u;ba8000800u,10,20B")
    assert (potential.type, current.type) == ("da", "ba")
    assert potential.value == pytest.approx(2.048e-3) and potential.unit == "V" and potential.range is None
    assert current.value == pytest.approx(2.048e-3) and current.unit == "A"
    assert current.status == 0 and current.range == 0x0B


def test_metadata_status_range_and_noise():
    """手册 6.3：PdaDF5CB18n;ba9699F74p,14,218,40 —— 欠载（状态 4）、量程 0x18（EmStat4 的 10 mA 档）、噪声 0。"""
    potential, current = ms.parse_package("PdaDF5CB18n;ba9699F74p,14,218,40")
    assert potential.value == pytest.approx(0.099994392)
    assert current.value == pytest.approx(23.699316e-6)
    assert current.status == ms.STATUS_UNDERLOAD and current.range == 0x18 and current.noise == 0


def test_emstat4_lsv_example_packages():
    """EmStat4 通讯协议 4.28 的 LSV 例子（100 kΩ 电阻，−1 V 起扫）：计数 1、−1 V、约 −10 µA；时间约 22.5 s。"""
    count, potential, current = ms.parse_package("Pja8000001i;da7F0BDF9u;ba7678CD7p,10,20F,40")
    assert count.value == 1 and count.type == "ja"
    assert potential.value == pytest.approx(-0.999943) and current.value == pytest.approx(-9.990953e-6)
    assert current.range == 0x0F
    elapsed, _ = ms.parse_package("Peb9570C36u;ba898E141p,10,20F,40")
    assert elapsed.type == "eb" and elapsed.value == pytest.approx(22.481974)
    zero, small = ms.parse_package("Pda8000000 ;ba9AE0ABCf,14,212,40")
    assert zero.value == 0 and small.value == pytest.approx(2.8183228e-8) and small.status == 4


def test_bad_packages_are_reported():
    for line in ("Xda8000000 ", "P", "Pda80", "PZZ8000000 "):
        with pytest.raises(ms.PackageError):
            ms.parse_package(line)


@pytest.mark.parametrize("value", [0.0, 0.1, -0.1, 4.5, -2.5e-5, 1.234e-9, 80.123, 1e5, 3.3e-12, 0.0999943])
def test_encoding_round_trips_with_the_finest_prefix(value):
    """假仪器编码：挑放得下的最细的前缀（0.1 V 写成 n，和真仪器一样），解码回来误差在最后一位。"""
    field = ms.encode_value(value)
    assert len(field) == 8
    assert ms.decode_value(field) == pytest.approx(value, rel=1e-6, abs=1e-24)
    assert ms.encode_value(0.1) == "DF5E100n"  # 0.1 V = 100 000 000 n
    assert ms.encode_value(math.nan) == ms.NAN_FIELD and ms.encode_value(5, integer=True) == "8000005i"


def test_format_package_is_parsed_back():
    line = ms.format_package([("da", 0.5, 0, None), ("ba", -1.5e-6, ms.STATUS_OVERLOAD, 0x0C)])
    potential, current = ms.parse_package(line)
    assert potential.value == pytest.approx(0.5) and current.value == pytest.approx(-1.5e-6)
    assert current.status == ms.STATUS_OVERLOAD and current.range == 0x0C


# ---------- 脚本里的数 ----------

@pytest.mark.parametrize(("value", "text"), [
    (0, "0"), (6.0, "6"), (0.0001, "100u"), (1.5, "1500m"), (-0.5, "-500m"), (200000, "200k"), (1e6, "1M"),
    (1e-9, "1n"), (0.01, "10m"), (4.2, "4200m"), (0.30000000000000004, "300m"), (2.01e-3, "2010u"), (3e-5, "30u"),
])
def test_script_literals(value, text):
    """手册 4.2：脚本里的浮点数是「整数 + SI 前缀」，不能写小数点。"""
    assert ms.literal(value) == text
    parsed, integer = ms.parse_literal(text)
    assert parsed == pytest.approx(value) and not integer


def test_parse_literal_forms():
    assert ms.parse_literal("255i") == (255, True)
    assert ms.parse_literal("0xFF") == (255, True) and ms.parse_literal("0b11111111") == (255, True)
    assert ms.parse_literal("1M")[0] == 1e6 and ms.parse_literal("1m")[0] == pytest.approx(1e-3)
    for bad in ("1.5", "1x", "", "m", "1e3"):
        with pytest.raises(ValueError):
            ms.parse_literal(bad)
    with pytest.raises(ValueError):
        ms.literal(math.inf)
    assert ms.integer_literal(51) == "51i"
    assert ms.identifier("zr") and ms.identifier("e0_x") and not ms.identifier("0e") and not ms.identifier("Zr")


# ---------- 错误、固件 ----------

@pytest.mark.parametrize(("line", "code", "row", "column", "kind"), [
    ("l!4001: Line 1, Col 27", 0x4001, 1, 27, "invalid"),     # 加载时语法错（通讯协议第 8 章）
    ("!0028: Line 4", 0x0028, 4, None, "invalid"),            # 运行时除以 0
    ("Z!0006", 0x0006, None, None, "busy"),                   # 没有脚本在跑时发 Z
    ("w!0003", 0x0003, None, None, "invalid"),
    ("l!001B: Line 9, Col 1", 0x001B, 9, 1, "unsupported"),
])
def test_error_lines(line, code, row, column, kind):
    error = ms.parse_error(line)
    assert (error.code, error.line, error.column, error.kind) == (code, row, column, kind)
    assert f"!{code:04X}" in error.text()


def test_error_text_points_at_the_script_line():
    error = ms.parse_error("l!4001: Line 2, Col 1")
    text = error.text(["var p", "wrong_command 1"])
    assert "脚本命令不认识" in text and "第 2 行第 1 列" in text and "wrong_command 1" in text
    assert ms.parse_error("Pda8000000 ") is None and ms.parse_error("M0000") is None


@pytest.mark.parametrize(("first", "device_type", "version"), [
    ("tes4_hr1100#Jan 28 2022 11:04:43", "es4_hr", "1.1.00"),       # EmStat4 通讯协议 4.1
    ("tes4_lr1000#Jun      7 2021 16:51:38", "es4_lr", "1.0.00"),
    ("tespico10#Apr      1 2019 15:48:13", "espico", "1.0"),        # EmStat Pico 通讯协议 4.1
    ("tespico1304#Oct 22 2021 14:38:26", "espico", "1.3.04"),
])
def test_firmware_version(first, device_type, version):
    info = ms.parse_firmware(first, "R*")
    assert info["device_type"] == device_type and info["version"] == version and info["release"] == "正式版"
    assert "  " not in info["build"]
    with pytest.raises(ValueError):
        ms.parse_firmware("es4_hr1100", "R*")


def test_model_check_and_device_limits():
    assert ms.model_matches("EmStat4", "es4_hr") and ms.model_matches("EmStat4 HR", "es4_hr")
    assert ms.model_matches("PalmSens EmStat Pico", "espico")
    assert not ms.model_matches("EmStat4 LR", "es4_hr") and not ms.model_matches("EmStat Pico", "es4_lr")
    assert ms.model_matches("", "es4_hr") and ms.model_matches("EmStat4 HR", "unknown")
    hr, pico = ms.device_info("es4_hr"), ms.device_info("espico")
    assert hr["modes"][2][:2] == (-6.0, 6.0) and hr["eis_max_vrms"] == 0.9 and hr["eis_max_hz"] == 200e3
    assert pico["modes"][3] == (-1.7, 2.0, 1.214) and pico["eis_max_vrms"] == 0.429


# ---------- 网关写的脚本 ----------

def plan(technique: str, area: float | None = 2.01, **settings) -> Plan:
    checked, problems = check_settings(technique, settings, Cell(area_cm2=area))
    assert problems == []
    return Plan(technique=technique, settings=checked, ranging=Ranging(1e-4, 1e-9, 1e-2), bandwidth_Hz=4.0)


PLANS = {
    "ocp": dict(duration_s=10, interval_s=0.5),
    "lsv": dict(e_begin_V=3.0, e_end_V=4.0, scan_rate_V_s=0.01, e_step_V=0.002),
    "lsv-ocp": dict(e_begin_V="ocp", rest_s=5, e_end_V=6.0, scan_rate_V_s=0.001, e_step_V=0.001,
                    onset_threshold_mA_cm2=0.01, stop_mA_cm2=1.0),
    "cv": dict(e_begin_V=3.0, e_vertex1_V=3.6, e_vertex2_V=3.0, e_step_V=0.002, scan_rate_V_s=0.05, cycles=3),
    "ca": dict(e_V=4.2, duration_s=60, interval_s=0.5),
    "eis": dict(freq_start_Hz=100000, freq_end_Hz=1, points_per_decade=10, amplitude_Vrms=0.01, e_dc_V=0),
    "eis-ocp": dict(freq_start_Hz=100000, freq_end_Hz=0.1, points_per_decade=5, amplitude_Vrms=0.005, e_dc_V="ocp"),
}


@pytest.mark.parametrize("key", sorted(PLANS))
def test_generated_scripts_are_accepted_by_the_instrument_parser(key):
    technique = key.split("-")[0]
    lines = build_script(plan(technique, **PLANS[key]), potential_range_V=6.0)
    program = parse_script(lines)  # 假仪器按手册的写法核对：数的写法、变量先声明、块配对、测量循环不嵌套
    assert lines[-2:] == ["on_finished:", "cell_off"] and program.tag is not None
    assert all(len(line) < 255 for line in lines) and "" not in lines
    assert lines[:2] == [line for line in lines if line.startswith("var ")][:2]
    loops = [line for line in lines if line.startswith("meas_loop_")]
    assert loops[-1].startswith(f"meas_loop_{technique}")
    assert silence_limit(plan(technique, **PLANS[key])) > 30


def test_script_details_follow_the_manual():
    eis = build_script(plan("eis", **PLANS["eis"]), potential_range_V=6.0)
    assert "set_pgstat_mode 3" in eis, "EIS 要高速模式（手册 14.11.16）"
    assert "meas_loop_eis f zr zi 10m 100k 1 51i 0" in eis, "100 kHz–1 Hz、每十倍 10 点 = 51 点；振幅是有效值"
    assert not any(line.startswith("set_max_bandwidth") for line in eis)
    ocp = build_script(plan("ocp", **PLANS["ocp"]), potential_range_V=6.0)
    assert ocp.index("cell_off") < ocp.index("meas_loop_ocp p 500m 10"), "开路电位要先 cell_off（!0014）"
    assert "set_range ab 6" in ocp and "set_max_bandwidth 4" in ocp
    lsv = build_script(plan("lsv", **PLANS["lsv-ocp"]), potential_range_V=6.0)
    assert "meas_loop_ocp oc 500m 5" in lsv and "set_e oc" in lsv and "meas_loop_lsv p c oc 6 1m 1m" in lsv
    assert "if c > 2010u" in lsv and "if c < -2010u" in lsv and lsv.count("breakloop") == 2, "截止 1 mA/cm² × 2.01 cm²"
    assert lsv.index("meas_loop_ocp oc 500m 5") < lsv.index("cell_on")
    cv = build_script(plan("cv", **PLANS["cv"]), potential_range_V=6.0)
    assert "meas_loop_cv p c 3 3600m 3 2m 50m nscans(3)" in cv and "set_range_minmax da 3 3600m" in cv
    one = build_script(plan("cv", **{**PLANS["cv"], "cycles": 1}), potential_range_V=6.0)
    assert not any("nscans" in line for line in one)
    ca = build_script(plan("ca", **PLANS["ca"]), potential_range_V=6.0)
    assert "meas_loop_ca p c 4200m 500m 60" in ca and ca.index("cell_on") < ca.index("meas_loop_ca p c 4200m 500m 60")


@pytest.mark.parametrize(("lines", "code", "line", "column"), [
    (["var p", "wrong_command"], 0x4001, 2, 1),                       # 不认识的命令
    (["var p", "var p"], 0x4026, 2, 5),                               # 重复声明
    (["var P"], 0x402B, 1, 5),                                        # 变量名要小写
    (["set_e 1.5"], 0x4004, 1, 7),                                    # 不能写小数点
    (["set_e x"], 0x420B, 1, 7),                                      # 变量没声明
    (["var p", "var c", "meas_loop_lsv p c 0 1 10m"], 0x4004, 3, 26),  # 少参数
    (["var p", "meas_loop_ocp p 100m 2 3"], 0x420A, 2, 24),            # 多参数
    (["var p", "meas_loop_ocp p 100m 2 nscans(2)"], 0x4008, 2, 24),     # nscans 只给 CV
    (["var p", "meas_loop_ocp p 100m 2", "meas_loop_ocp p 100m 2", "endloop", "endloop"], 0x400B, 3, 1),
    (["var p", "meas_loop_ocp p 100m 2"], 0x4018, 2, 1),               # 没有 endloop
    (["endif"], 0x400E, 1, 1),
])
def test_instrument_parser_rejects_bad_scripts_like_the_manual(lines, code, line, column):
    with pytest.raises(ScriptError) as caught:
        parse_script(lines)
    assert (caught.value.code, caught.value.line, caught.value.column) == (code, line, column)
