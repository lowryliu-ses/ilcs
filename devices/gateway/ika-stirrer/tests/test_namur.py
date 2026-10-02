"""真实接口（driver/namur.py + driver/link.py）对着假加热板（simulator/namur_server.py）走真实的 TCP 行：
写命令不回、读命令回「数值 通道号」、错位的应答、线断了、START_1 之后设定值复位要重发、看门狗回显。"""
from __future__ import annotations

import socket
import time

import pytest

from driver.link import Link, LinkError
from driver.namur import Hotplate, NamurError, number
from simulator.namur_server import FakePlate, NamurServer


@pytest.fixture()
def rig():
    plate = FakePlate(tau_s=0.2, ramp_rpm_s=10000)
    server = NamurServer(plate)
    client = Hotplate(Link({"kind": "tcp", "host": "127.0.0.1", "port": server.port, "gap_sec": 0, "timeout_sec": 0.5}))
    try:
        yield plate, client
    finally:
        client.link.close()
        server.stop()


def test_reads_parse_the_value_and_check_the_channel_index(rig):
    plate, client = rig
    assert client.name() == "RCT digital"
    assert client.temperature(2) == pytest.approx(25.0)
    assert client.temperature(1) == pytest.approx(25.0)
    assert client.speed() == 0.0
    assert client.setpoint(1) == pytest.approx(25.0)


def test_write_commands_get_no_reply_and_the_next_read_is_not_confused(rig):
    plate, client = rig
    client.set_speed(300)
    client.set_temperature(40.5)
    client.start_motor()
    client.start_heater()
    assert client.setpoint(4) == 300 and client.setpoint(1) == pytest.approx(40.5)
    time.sleep(0.3)
    assert client.speed() == pytest.approx(300) and client.temperature(2) > 30
    assert plate.log[:4] == ["OUT_SP_4 300", "OUT_SP_1 40.5", "START_4", "START_1"]
    client.stop_motor()
    client.stop_heater()
    assert client.confirm() >= 0 and not plate.snapshot()["motor"] and not plate.snapshot()["heater"]


def test_setpoints_are_written_the_way_the_device_takes_them():
    assert number(40) == "40" and number(40.0) == "40" and number(40.5) == "40.5" and number(37.26) == "37.3"


def test_a_late_reply_is_drained_not_taken_for_the_next_answer(rig):
    """有的固件对写命令也回点什么：缓冲里晚到的字节在下一条读命令之前丢掉，不当成它的应答。"""
    plate, client = rig
    original = plate.handle

    def chatty(line):
        reply = original(line)
        return line if reply is None and line.startswith("START") else reply

    plate.handle = chatty
    client.set_speed(500)
    client.start_motor()
    time.sleep(0.1)  # 回显已经到了接收缓冲里
    assert client.setpoint(4) == 500


def test_a_reply_for_another_channel_is_not_accepted(rig):
    plate, client = rig
    plate.handle = lambda line: "300 2" if line == "IN_PV_4" else FakePlate.handle(plate, line)
    with pytest.raises(NamurError, match="通道号不是 4"):
        client.speed()
    plate.handle = lambda line: "ER 2"
    with pytest.raises(NamurError, match="不是数值"):
        client.temperature(1)


def test_a_dead_line_is_a_link_error_and_a_write_alone_proves_nothing(rig):
    """线断了：写命令照样「写出去」（设备不回，网关不知道它收没收到），紧跟的读命令才暴露出来。"""
    plate, client = rig
    plate.mute = True
    client.stop_motor()  # 不报错：写命令本来就没有应答
    with pytest.raises(LinkError) as caught:
        client.confirm()
    assert caught.value.sent
    plate.mute = False
    assert client.name() == "RCT digital", "丢掉坏连接、下次重连"


def test_unreachable_server_is_not_sent():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    client = Hotplate(Link({"kind": "tcp", "host": "127.0.0.1", "port": port, "timeout_sec": 0.5}))
    with pytest.raises(LinkError) as caught:
        client.start_motor()
    assert caught.value.sent is False


def test_watchdog_mode_2_echoes_and_falls_back_to_the_safety_values(rig):
    plate, client = rig
    client.arm_watchdog(20, 25, 0)
    assert plate.snapshot()["watchdog"] == 20 and plate.safe_temp == 25 and plate.safe_speed == 0
    client.set_temperature(60)
    client.start_heater()
    client.confirm()  # 写命令不回：读一次，确认前面的写已经到了
    plate.fed -= 25  # 网关 25 秒没喂
    state = plate.snapshot()
    assert state["watchdog_event"] and state["temp_setpoint"] == 25 and state["heater"], "模式 2 只改设定值、不关加热"
    client.clear_watchdog()
    assert plate.snapshot()["watchdog"] == 0 and not plate.snapshot()["watchdog_event"]


def test_device_clamps_setpoints_outside_its_range(rig):
    plate, client = rig
    client.set_speed(30)    # 低于最低转速 50
    client.set_temperature(400)
    assert client.setpoint(4) == 50 and client.setpoint(1) == 310
