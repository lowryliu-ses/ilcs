"""真实接口（driver/chillers.py、driver/namur.py + driver/link.py）对着假设备（simulator/）走真实的 TCP 行：
三家冷水机各自的命令、应答格式、错误表现，和搅拌板的 NAMUR 电机命令。数值例子取自厂家手册。"""
from __future__ import annotations

import socket
import time

import pytest

from driver.chillers import ChillerError, Huber, Julabo, Lauda, Refused, huber_encode, huber_temperature, make_chiller
from driver.config import BRANDS, NAMUR, normalize_link
from driver.link import Link, LinkError
from driver.namur import NamurError, Stirrer
from simulator.chillers import FAKES, Bath, LineServer
from simulator.namur_server import FakePlate, NamurServer


@pytest.fixture()
def rig():
    """rig(kind) → (假冷水机, 真实客户端)。"""
    made = []

    def factory(kind: str, **options):
        fake = FAKES[kind](Bath(ambient_c=22, tau_s=0.2))
        server = LineServer(fake.handle)
        link, problems = normalize_link("chiller", {"kind": "tcp", "host": "127.0.0.1", "port": server.port,
                                                    "gap_sec": 0, "timeout_sec": 0.5}, BRANDS[kind])
        assert not problems
        client = make_chiller(kind, Link(link), **options)
        made.append((server, client))
        return fake, client

    yield factory
    for server, client in made:
        client.link.close()
        server.stop()


# ---------- Huber：PB 命令 ----------

def test_huber_values_are_hex_twos_complement_in_hundredths_like_the_manual():
    assert huber_encode(20) == "07D0" and huber_encode(-23.15) == "F6F5" and huber_encode(-10) == "FC18"
    assert huber_temperature(0xFFCC) == -0.52 and huber_temperature(0x1010) == 41.12
    assert huber_temperature(0x087F) == 21.75 and huber_temperature(0x07E7) == 20.23
    assert huber_temperature(0xC504) == -151.0, "-151 ℃ 是「没有探头」的标记"
    # 有符号数小于 -15111 的按无符号数读（300 ℃ 以上的设备）
    assert huber_temperature(0x8000) == 327.68 and huber_temperature(0xC4F8) == 504.24


def test_huber_reads_writes_and_starts_over_pb_commands(rig):
    fake, client = rig("huber")
    reading = client.poll()
    assert reading.bath == pytest.approx(22, abs=0.01) and not reading.running and not reading.alarm
    assert client.set_setpoint(-10) == -10.0 and "{M00FC18" in fake.log
    client.start()
    assert "{M140001" in fake.log and client.poll().running
    time.sleep(0.3)
    assert client.poll().bath < 0, "控温开着，浴温往设定值走"
    client.stop()
    assert "{M140000" in fake.log and not client.poll().running
    assert client.identify()["serial"] == "23456789"


def test_huber_answers_a_limited_setpoint_with_the_value_it_took(rig):
    fake, client = rig("huber")
    fake.min_setpoint = -20.0
    assert client.limits() == (-20.0, 100.0)
    assert client.set_setpoint(-30) == -20.0, "写完之后回的是冷水机此刻的设定值：被限幅了"


def test_huber_unavailable_variables_and_missing_sensors_are_read_errors(rig):
    fake, client = rig("huber")
    with pytest.raises(ChillerError, match="7FFF"):
        client.exchange(0x02)  # 回流温度：这台没开放
    with pytest.raises(ChillerError, match="-151"):
        client._temperature(0x07, "过程温度")  # 没接外置探头


def test_huber_reports_errors_and_a_restart_from_the_status_word(rig):
    fake, client = rig("huber")
    assert client.poll().restarted, "重启后第一次读状态字：第 14 位是 0"
    assert not client.poll().restarted
    fake.set_alarm()
    reading = client.poll()
    assert reading.alarm == "Huber 报错 -1331" and client.health()[0] == "Huber 报错 -1331"
    fake.set_alarm(False)
    client.set_setpoint(5)
    fake.restart()  # 断电重启：远程写的设定值回到面板上的值
    assert client.health() == ("", True)  # 健康检查先读到了「重启过」……
    reading = client.poll()
    assert reading.restarted and reading.setpoint == pytest.approx(22), "……下一次读数照样报出来，不会漏掉"


def test_huber_does_not_answer_a_malformed_command(rig):
    fake, client = rig("huber")
    with pytest.raises(LinkError) as caught:
        client.link.ask("{M0")
    assert caught.value.sent
    assert client.poll().bath == pytest.approx(22, abs=0.01), "丢掉坏连接、下次重连"


# ---------- Julabo ----------

def test_julabo_in_commands_reply_and_out_commands_do_not(rig):
    fake, client = rig("julabo")
    reading = client.poll()
    assert reading.remote and not reading.running and reading.setpoint == pytest.approx(22)
    assert client.set_setpoint(-10) == -10.0
    client.start()
    assert "out_sp_00 -10.00" in fake.log and fake.log[fake.log.index("out_sp_00 -10.00") + 1] == "in_sp_00", \
        "out 命令不回：紧跟一条 in 命令回读"
    assert fake.log[fake.log.index("out_mode_05 1") + 1] == "in_mode_05"
    assert client.status() == (3, "03 REMOTE START") and client.poll().running
    client.stop()
    assert client.status()[1] == "02 REMOTE STOP"
    assert client.identify()["firmware"] == "JULABO CF41 VERSION 1.30"


def test_julabo_value_out_of_range_is_a_refusal(rig):
    fake, client = rig("julabo")
    fake.max_setpoint = 30.0
    with pytest.raises(Refused, match="-11 VALUE TOO LARGE"):
        client.set_setpoint(50)
    assert fake.bath.setpoint == pytest.approx(22), "冷水机没收这个值"


def test_julabo_in_manual_mode_ignores_out_commands(rig, monkeypatch):
    monkeypatch.setattr(Julabo, "START_PAUSE", 0.01)
    fake, client = rig("julabo")
    fake.remote = False
    assert client.health() == ("", False) and not client.poll().remote
    with pytest.raises(Refused, match="面板控制模式"):
        client.set_setpoint(5)
    with pytest.raises(Refused, match="面板控制模式"):
        client.start()
    assert not fake.bath.running and fake.bath.setpoint == pytest.approx(22)


def test_julabo_alarms_come_from_status_and_warnings_are_not_alarms(rig):
    fake, client = rig("julabo")
    fake.set_alarm()
    reading = client.poll()
    assert reading.alarm == "Julabo 报警：-01 LOW LEVEL ALARM" and "in_mode_05" in fake.log, "status 报消息时启停另读"
    assert Julabo.classify(-20, "-20 WARNING: CLEAN CONDENSOR") == "warning"
    assert Julabo.classify(-11, "-11 VALUE TOO LARGE") == "command"
    assert Julabo.classify(-14, "-14 EXCESS TEMPERATURE PROTECTOR ALARM") == "alarm"
    assert Julabo.classify(3, "03 REMOTE START") == "state"


def test_julabo_missing_probe_and_uppercase_commands(rig):
    fake, client = rig("julabo", uppercase=True)
    assert client.poll().bath == pytest.approx(22, abs=0.01)
    assert "IN_PV_00" in fake.log and "STATUS" in fake.log, "CF 系列手册写的是大写"
    original = fake.answer
    fake.answer = lambda line: "---.--" if line.lower() == "in_pv_00" else original(line)
    with pytest.raises(ChillerError, match="探头没接"):
        client.poll()


# ---------- LAUDA ----------

def test_lauda_write_commands_answer_ok_and_reads_are_fixed_point(rig):
    fake, client = rig("lauda")
    assert client.poll().setpoint == 22.0 and "IN_SP_00" in fake.log  # 假设备回的是 022.00
    assert client.set_setpoint(-10) == -10.0 and "OUT_SP_00_-10.00" in fake.log
    client.start()
    assert fake.log[fake.log.index("START") + 1] == "IN_MODE_02" and client.poll().running
    client.stop()
    assert not client.poll().running
    assert client.identify() == {"model": "PRO RP 1090 C", "firmware": "V2.30", "serial": ""}


def test_lauda_err_replies_are_refusals(rig):
    fake, client = rig("lauda")
    fake.min_setpoint = -5.0
    with pytest.raises(Refused, match="ERR_6（数值不允许）"):
        client.set_setpoint(-10)
    with pytest.raises(Refused, match="ERR_3（命令错误）"):
        client._ask("IN_PV_99")
    assert fake.bath.setpoint == pytest.approx(22)


def test_lauda_status_minus_one_is_an_alarm(rig):
    fake, client = rig("lauda")
    fake.set_alarm()
    assert client.poll().alarm == "LAUDA 报故障（STATUS -1，STAT 1000000）"


# ---------- 链路缺省与搅拌板 ----------

def test_link_defaults_follow_each_brand():
    huber, _ = normalize_link("x", {"kind": "serial", "port": "COM3"}, BRANDS["huber"])
    julabo, _ = normalize_link("x", {"kind": "serial", "port": "COM3"}, BRANDS["julabo"])
    lauda, _ = normalize_link("x", {"kind": "tcp", "host": "10.0.0.5"}, BRANDS["lauda"])
    plate, _ = normalize_link("x", {"kind": "serial", "port": "COM5"}, NAMUR)
    assert (huber["baudrate"], huber["bytesize"], huber["parity"], huber["rtscts"], huber["eol"]) == (9600, 8, "N", False, "\r\n")
    assert (julabo["baudrate"], julabo["bytesize"], julabo["parity"], julabo["rtscts"], julabo["eol"], julabo["gap_sec"]) \
        == (4800, 7, "E", True, "\r", 0.25)
    assert lauda["port"] == 54321 and normalize_link("x", {"kind": "tcp", "host": "h"}, BRANDS["huber"])[0]["port"] == 8101
    assert normalize_link("x", {"kind": "tcp", "host": "h"}, BRANDS["julabo"])[1], "Julabo 网口端口要照设备菜单写"
    assert (plate["baudrate"], plate["bytesize"], plate["parity"]) == (9600, 7, "E")


def test_unreachable_device_is_not_sent():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    client = Huber(Link({"kind": "tcp", "host": "127.0.0.1", "port": port, "timeout_sec": 0.5}))
    with pytest.raises(LinkError) as caught:
        client.start()
    assert caught.value.sent is False


@pytest.fixture()
def plate():
    fake = FakePlate()
    server = NamurServer(fake)
    client = Stirrer(Link({"kind": "tcp", "host": "127.0.0.1", "port": server.port, "gap_sec": 0, "timeout_sec": 0.5}))
    try:
        yield fake, client
    finally:
        client.link.close()
        server.stop()


def test_stirrer_runs_the_motor_only(plate):
    fake, client = plate
    assert client.name() == "RCT digital" and client.speed() == 0.0
    client.set_speed(400)
    assert client.speed_setpoint() == 400
    client.start()
    time.sleep(0.2)
    assert client.speed() == pytest.approx(400)
    client.stop()
    assert client.confirm() >= 0 and not fake.snapshot()["motor"]
    assert fake.log[-3:] == ["STOP_4", "STOP_1", "IN_PV_4"], "关搅拌、顺手关加热，再读一次确认送到"
    assert not any(line.startswith(("START_1", "OUT_SP_1")) for line in fake.log), "不碰加热"


def test_stirrer_rejects_a_reply_for_another_channel_and_a_dead_line(plate):
    fake, client = plate
    fake.handle = lambda line: "300 2" if line == "IN_PV_4" else FakePlate.handle(fake, line)
    with pytest.raises(NamurError, match="通道号不是 4"):
        client.speed()
    fake.handle = lambda line: None
    client.stop()  # 写命令本来就没有应答：不报错
    with pytest.raises(LinkError) as caught:
        client.confirm()
    assert caught.value.sent
