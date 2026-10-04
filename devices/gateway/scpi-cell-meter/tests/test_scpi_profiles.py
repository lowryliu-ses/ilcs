"""三份 profile（驱动宿主的设备配置）：建得出 line_command 插件、支持标志与验收缺省，再查命令列表本身守不守 SCPI 的规矩。

    api/.venv/bin/pytest -q -p no:cacheprovider devices/gateway/scpi-cell-meter/tests
"""
from __future__ import annotations

import re

import pytest

from scpi_harness import CAPABILITY, PROFILES, load_profile

NAMES = sorted(PROFILES)
SUPPORTS = {"hold": False, "abort": False, "query": True, "dedup": True}
OUTPUTS = {"keithley-2450": {"ocv_V"}, "keithley-2400": {"ocv_V"}, "hioki-bt3562": {"ocv_V", "ir_mohm"}}


@pytest.mark.parametrize("name", NAMES)
def test_profile_builds_the_driver_host_plugin(name, monkeypatch):
    """profile 的映射 + 示例连接参数建得出驱动宿主的 line_command 插件（插件构造时就核对命令列表、正则、状态映射）；
    连接参数只有 transport（设备编号的核对在 ILCS 工位那边：sila2_v1 的 expected_device_id）；支持标志与验收缺省照测量的性质。"""
    from types import SimpleNamespace

    from ilcs_host.plugins.line_command import LineCommandAdapter
    from ilcs_host.settings import settings

    monkeypatch.setattr(settings, "allowed_hosts", "*")  # 示例里的仪表主机名（非正式环境 * 放行）
    profile = load_profile(name)
    assert profile["format"] == "ilcs-host-device-profile/1" and profile["plugin"] == "line_command"
    assert profile["version"] == "scpi-cell-meter 0.2"
    assert set(profile["connection"]) == {"transport"}
    plugin = LineCommandAdapter(SimpleNamespace(
        station_id=f"CHECK-{name}", config={**profile["config"], **profile["connection"]}, credential_ref="",
        protocol=profile["protocol"], version=profile["version"], note="",
        **{f"supports_{key}": value for key, value in profile["supports"].items()},
    ))
    assert plugin.tasks and plugin.capability_spec(CAPABILITY)["start"]
    assert profile["supports"] == SUPPORTS, "测量是即时动作：没有在途作业可保持、可终止"
    assert profile["acceptance"] == {"capability": CAPABILITY, "params": {}}


def test_profile_codes_are_distinct():
    codes = [load_profile(name)["code"] for name in NAMES]
    assert len(set(codes)) == len(codes)


@pytest.mark.parametrize("name", NAMES)
def test_commands_follow_the_reply_rules(name):
    """设置命令不回复（reply: false），查询（带 ?）等回复；只有一条动作命令，就是读数的 :READ?；动作之前至少查一次错；
    读数正则与实测正则只出这台仪表的输出项。"""
    config = load_profile(name)["config"]
    spec = config["capabilities"][CAPABILITY]
    steps = spec["start"]
    for step in steps + config.get("acknowledge", []):
        query = step["send"].split()[0].endswith("?")
        assert (step.get("reply", True) is not False) is query, step
    motion = [index for index, step in enumerate(steps) if step.get("motion")]
    assert len(motion) == 1 and steps[motion[0]]["send"].split()[0] == ":READ?"
    assert any(step.get("expect") for step in steps[:motion[0]]), "配置没被仪表接受就不该触发测量"
    assert steps[motion[0]].get("reject"), "溢出 / 测量异常的读数要判明确失败"
    assert set(re.compile(spec["result"]["pattern"]).groupindex) == OUTPUTS[name]
    assert set(re.compile(config["actuals"]["pattern"]).groupindex) == OUTPUTS[name]
    assert config["idle_after_start"] == "unknown", "空闲不能当成做完：回复丢了时只认缓冲区 / 状态字节里的新读数"


@pytest.mark.parametrize("name", NAMES)
def test_status_is_a_non_destructive_read(name):
    """状态查询不能是读了就清的寄存器（Hioki 的 :ESR0?、SCPI 的 *ESR?）：两个驱动实例（执行器、验收）先后读，
    后读的会看不到前一个看到的「测完了」。"""
    status = load_profile(name)["config"]["status"]
    assert status["send"] in {':TRAC:ACT? "defbuffer1"', ":TRAC:POIN:ACT?", "*STB?"}
    assert set(status["states"].values()) <= {"idle", "done"}


def _parse(pattern: str, reply: str) -> dict | None:
    """驱动的做法：回复去掉两头空白，按正则取命名组，能转成数的转成数（科学计数法照转）。"""
    match = re.compile(pattern).search(reply.strip())
    return {key: float(value) for key, value in match.groupdict().items()} if match else None


@pytest.mark.parametrize(("name", "reply", "expected"), [
    # 2450 参考手册的读数写法（:READ? / :FETCh? 例子）
    ("keithley-2450", "3.850000E+00", {"ocv_V": 3.85}),
    ("keithley-2450", "-6.580474E-05", {"ocv_V": -6.580474e-05}),
    # 2400 用户手册的 ASCII 数据格式（:FORMat:ELEMents VOLT 时只有电压一项）
    ("keithley-2400", "+3.850000E+00", {"ocv_V": 3.85}),
    ("keithley-2400", "+1.000206E+00", {"ocv_V": 1.000206}),
    # Hioki 说明书的例子：:FETCh? 288.02E-3,1.3921E+0；:READ? 289.68E-3, 1.3921E+0；正数符号位与前导 0 是空格
    ("hioki-bt3562", "288.02E-3,1.3921E+0", {"ir_mohm": 288.02, "ocv_V": 1.3921}),
    ("hioki-bt3562", "289.68E-3, 1.3921E+0", {"ir_mohm": 289.68, "ocv_V": 1.3921}),
    ("hioki-bt3562", "   15.00E-3, 3.85000E+0", {"ir_mohm": 15.0, "ocv_V": 3.85}),
    ("hioki-bt3562", "  1.3000E-3,-1.23400E+0", {"ir_mohm": 1.3, "ocv_V": -1.234}),
])
def test_reading_patterns_parse_the_documented_formats(name, reply, expected):
    spec = load_profile(name)["config"]["capabilities"][CAPABILITY]
    motion = next(step for step in spec["start"] if step.get("motion"))
    assert not re.compile(motion["reject"]).search(reply)
    assert _parse(spec["result"]["pattern"], reply) == pytest.approx(expected)


@pytest.mark.parametrize(("name", "reply"), [
    ("keithley-2450", "9.9e+37"),            # 超量程（2450 参考手册）
    ("keithley-2450", "9.900000E+37"),
    ("keithley-2400", "+9.900000E+37"),      # 溢出（2400 用户手册：overflow reads as +9.9E37）
    ("keithley-2400", "+9.910000E+37"),      # 没测的元素是 NAN 9.91e37
    ("hioki-bt3562", " 1000.00E+6, 3.85000E+0"),     # 300 mΩ 档 ±OF
    ("hioki-bt3562", "+1000.00E+7,+1.00000E+10"),    # 测量异常（探针没接触上）
    ("hioki-bt3562", "   15.00E-3, 1.00000E+9"),     # 6 V 档 ±OF
])
def test_overflow_and_fault_readings_hit_reject(name, reply):
    """溢出与测量异常值在动作命令的 reject 上：驱动判明确失败，不把 9.9E37 当读数报完成。"""
    steps = load_profile(name)["config"]["capabilities"][CAPABILITY]["start"]
    motion = next(step for step in steps if step.get("motion"))
    assert re.compile(motion["reject"]).search(reply.strip())


@pytest.mark.parametrize(("name", "reply", "state"), [
    ("keithley-2450", "0", "idle"), ("keithley-2450", "1", "done"), ("keithley-2450", "850", "done"),
    ("keithley-2400", "0", "idle"), ("keithley-2400", "+00001", "done"), ("keithley-2400", "+00000", "idle"),
    ("hioki-bt3562", "0", "idle"), ("hioki-bt3562", "16", "idle"), ("hioki-bt3562", "17", "done"),
    ("hioki-bt3562", "1", "done"), ("hioki-bt3562", "48", "idle"), ("hioki-bt3562", "49", "done"),
])
def test_status_mapping(name, reply, state):
    """Keithley：缓冲区读数个数，0 = 没有新读数、非 0 = 测完了（前导 0 与正号照认）；Hioki：*STB? 的 bit0 = 奇数。"""
    status = load_profile(name)["config"]["status"]
    match = re.compile(status["pattern"]).search(reply)
    assert match and status["states"][match.group("state")] == state
