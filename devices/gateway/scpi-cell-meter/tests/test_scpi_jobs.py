"""对着假仪表直接驱动 ILCS 的 `line_command_v1`：一次测量当场完成并带实测值；重投回放；回复丢了怎么找回；
仪表忙、联锁、命令集不对、溢出 / 测量异常各怎么判。都不连 ILCS 数据库。"""
from __future__ import annotations

import pytest

from scpi_harness import acceptance, adapter, bench, record, request

NAMES = ["keithley-2450", "keithley-2400", "hioki-bt3562"]
EXPECTED = {
    "keithley-2450": {"ocv_V": 3.85},
    "keithley-2400": {"ocv_V": 3.85},
    "hioki-bt3562": {"ir_mohm": 15.0, "ocv_V": 3.85},
}


@pytest.mark.parametrize("name", NAMES)
def test_measurement_completes_as_a_job_with_actuals(name):
    """READ? 的回复就是结果：提交即完成，读数进回执与遥测（检测没有设定值）；重投回放台账、仪表不再测；
    执行器重启（新驱动实例）照样按指令号查回。"""
    with bench(name) as rig:
        rec = record(rig)
        driver = adapter(rec)
        health = driver.healthcheck()
        assert (health["simulator"], health["accepts_commands"], health["interlock"]) == (True, True, False)

        done = driver.submit(request("CMD-1"))
        assert (done.state, done.quality, done.origin) == ("done", "good", "real:line_command_v1")
        assert done.delivered == pytest.approx(EXPECTED[name])
        assert {point.metric: point.value for point in done.telemetry} == pytest.approx(EXPECTED[name])
        assert all(point.setpoint is None for point in done.telemetry)
        assert rig.meter.motions == 1

        replay = driver.submit(request("CMD-1"))
        assert replay.state == "done" and replay.delivered == done.delivered and rig.meter.motions == 1
        assert adapter(rec).query("CMD-1").state == "done", "台账在盘上：新实例按指令号查得回"
        assert driver.query("CMD-NEVER-SENT") is None

        # 下一颗电芯：上一笔读数先复位清掉，再测
        assert driver.submit(request("CMD-2")).state == "done" and rig.meter.motions == 2


def test_keithley_2450_measures_at_zero_current_and_disconnects_afterwards():
    """测量时源 0 A、电压限值 10 V 高于电芯、过压保护 20 V；测完输出关掉，关断状态是 HIMP（输出继电器断开，不拉电芯）。"""
    with bench("keithley-2450") as rig:
        adapter(record(rig)).submit(request("CMD-1"))
        state = rig.meter.state()
    assert state["output"] is False and state["off_mode"]["CURR"] == "HIMP"
    assert (state["source_function"], state["source_level"]) == ("CURR", 0.0)
    assert (state["vlimit"], state["protection"], state["volt_range"], state["volt_autorange"]) == (10.0, 20.0, 20.0, False)
    assert state["rsense"] is True and state["terminals"] == "FRON" and state["count"] == 1
    assert state["errors"] == [] and state["readings"] == 1


def test_keithley_2400_measures_at_zero_current_and_disconnects_afterwards():
    with bench("keithley-2400") as rig:
        adapter(record(rig)).submit(request("CMD-1"))
        state = rig.meter.state()
    assert state["output"] is False and state["off_mode"] == "HIMP" and state["auto_off"] is False
    assert (state["source_function"], state["source_level"], state["compliance"]) == ("CURR", 0.0, 10.0)
    assert state["elements"] == ["VOLT"] and state["rsense"] is True and state["errors"] == []
    assert state["trace"] == [3.85] and state["trace_control"] == "NEV", "数据缓冲区只存这一个读数、存满就停"


def test_hioki_is_configured_for_host_triggered_rv_measurements():
    """开机时标准事件寄存器的 PON 置位：模板先 *CLS 再查 *ESR?，不会把开机标志当成命令错误。"""
    with bench("hioki-bt3562") as rig:
        assert rig.meter.state()["sesr"] == 128
        adapter(record(rig)).submit(request("CMD-1"))
        state = rig.meter.state()
    assert (state["function"], state["autorange"], state["resistance_range"], state["voltage_range"]) == ("RV", False, 0.3, 6.0)
    assert (state["trigger"], state["continuous"], state["rate"], state["headers"]) == ("IMM", False, "MED", False)
    assert state["sesr"] == 0 and state["ese0"] == 1 and state["esr0"] == 3


@pytest.mark.parametrize("name", NAMES)
def test_lost_reply_is_unknown_then_recovered_from_the_instrument(name):
    """READ? 测了、回复没到：提交判结果未知（不是明确失败、不重发）；之后按仪表里的新读数（缓冲区 / 状态字节）判做完，
    实测值另外取回，质量标 uncertain。重启后的新实例同样查得回，重投不再测。"""
    from app.adapters import AdapterError, AdapterUnreachable

    with bench(name) as rig:
        rec = record(rig)
        rig.meter.set_fault("lost_receipt")
        with pytest.raises(AdapterUnreachable) as lost:
            adapter(rec).submit(request("CMD-LOST"))
        assert not isinstance(lost.value, AdapterError)
        assert rig.meter.motions == 1, "仪表测了，只是回复没到"
        rig.meter.set_fault("none")

        restarted = adapter(rec)
        found = restarted.query("CMD-LOST")
        assert (found.state, found.quality) == ("done", "uncertain")
        assert found.delivered == pytest.approx(EXPECTED[name])
        assert restarted.submit(request("CMD-LOST")).state == "done" and rig.meter.motions == 1


@pytest.mark.parametrize("name", NAMES)
def test_trigger_that_never_measured_stays_unknown(name):
    """上一颗电芯的读数还在仪表里；这次 READ? 没回、仪表也没测：开测前已经复位清掉旧读数，所以不会拿旧读数当这一次的，
    结论是结果未知、转人工，不猜「测完了」也不猜「没测」。"""
    from app.adapters import AdapterUnreachable

    with bench(name) as rig:
        rec = record(rig, request_timeout_sec=0.5)
        adapter(rec).submit(request("CMD-OLD"))
        rig.meter.set_fault("stuck")
        with pytest.raises(AdapterUnreachable):
            adapter(rec).submit(request("CMD-NEW"))
        rig.meter.set_fault("none")
        assert rig.meter.motions == 1
        assert adapter(rec).query("CMD-NEW").state == "unknown"


@pytest.mark.parametrize("name", NAMES)
def test_overflow_or_measurement_fault_is_an_explicit_failure(name):
    """溢出（Keithley 9.9E37）/ 测量异常（Hioki +1E+10）的读数：明确失败（这一笔没有有效读数，可以按恢复规则重测），
    重投照样明确失败、不再测；之后照常测下一笔。"""
    from app.adapters import AdapterError

    with bench(name) as rig:
        driver = adapter(record(rig))
        rig.meter.set_fault("fail")
        with pytest.raises(AdapterError, match="设备拒绝 ':READ?"):
            driver.submit(request("CMD-OF"))
        keithley = name.startswith("keithley")
        if keithley:
            assert rig.meter.state()["output"] is True, "动作命令判失败后不再发「关输出」：输出开着（源 0 A），下一笔开测时关掉"
        with pytest.raises(AdapterError):
            driver.submit(request("CMD-OF"))
        assert rig.meter.motions == 1
        rig.meter.set_fault("none")
        assert driver.submit(request("CMD-OK")).state == "done" and rig.meter.motions == 2
        if keithley:
            assert rig.meter.state()["output"] is False


def test_hioki_open_probes_are_an_explicit_failure():
    """探针没压上电芯：Hioki 回测量异常值（300 mΩ 档 +1000.00E+7），明确失败。"""
    from app.adapters import AdapterError

    with bench("hioki-bt3562") as rig:
        rig.meter.cell.connected = False
        with pytest.raises(AdapterError, match=r"\+1000\.00E\+7"):
            adapter(record(rig)).submit(request("CMD-OPEN"))


@pytest.mark.parametrize(("name", "reason"), [
    ("keithley-2450", "simulated busy"), ("keithley-2400", "Settings conflict"), ("hioki-bt3562", "'16'"),
])
def test_busy_instrument_refuses_before_measuring(name, reason):
    """仪表在忙别的、不收改设置的命令：开测前的查错（SYST:ERR? / *ESR?）把它挡住，明确失败，仪表没测。"""
    from app.adapters import AdapterError

    with bench(name) as rig:
        rig.meter.set_fault("busy")
        with pytest.raises(AdapterError, match=reason):
            adapter(record(rig)).submit(request("CMD-BUSY"))
        assert rig.meter.motions == 0


@pytest.mark.parametrize(("name", "reason"), [
    ("keithley-2450", "interlock"), ("keithley-2400", "OUTPUT blocked by interlock"),
])
def test_interlock_keeps_the_output_off_and_nothing_is_measured(name, reason):
    """夹具盖开关接在仪表联锁上（Interlock 设成 On）、盖子开着：打开输出被拒，开输出之后的查错把它挡住，仪表没测。"""
    from app.adapters import AdapterError

    with bench(name) as rig:
        rig.meter.set_fault("interlock")
        with pytest.raises(AdapterError, match=reason):
            adapter(record(rig)).submit(request("CMD-LID"))
        assert rig.meter.motions == 0 and rig.meter.state()["output"] is False


@pytest.mark.parametrize("name", ["keithley-2450", "keithley-2400"])
def test_optional_interlock_query_from_the_readme(name):
    """README 给的可选写法：现场把夹具盖开关接到了仪表联锁上，就加这段 interlock 查询——盖子开着时健康检查报联锁，
    启动前就拒绝（不必等到开输出）。"""
    from app.adapters import AdapterError

    snippet = {"interlock": {"send": ":OUTP:INT:TRIP?", "pattern": "^(?P<value>[01])$", "ok": ["1"]}}
    with bench(name) as rig:
        driver = adapter(record(rig, **snippet))
        assert driver.healthcheck()["interlock"] is False
        rig.meter.set_fault("interlock")
        assert driver.healthcheck()["interlock"] is True
        with pytest.raises(AdapterError, match="联锁"):
            driver.submit(request("CMD-LID"))
        assert rig.meter.motions == 0


@pytest.mark.parametrize("language", ["TSP", "SCPI2400"])
def test_keithley_2450_in_another_command_set_takes_no_commands(language):
    """2450 不在 SCPI 命令集（*LANG? 回 TSP / SCPI2400）：健康检查报「不接受指令」，ILCS 不投递动作指令；
    动作级验收不通过、一项动作都不跑。"""
    with bench("keithley-2450", control=False, language=language) as rig:
        rec = record(rig)
        assert adapter(rec).healthcheck()["accepts_commands"] is False
        report = acceptance("keithley-2450", rec, faults=False)
        states = {check.key: check.state for check in report.checks}
        assert states["health"] == "fail" and states["complete"] == "skip" and not report.ok
        assert rig.meter.motions == 0


def test_hioki_left_in_free_run_is_reset_before_measuring():
    """有人在面板上按了 LOCAL：仪器回到连续测量，状态字节里一直有「测完了」。开测前复位命令关掉连续测量、清寄存器，
    再按主机触发测这一次。"""
    with bench("hioki-bt3562") as rig:
        driver = adapter(record(rig))
        driver.submit(request("CMD-1"))
        rig.meter.handle(":INIT:CONT ON")
        rig.meter.cell.ir_ohm = 0.0212
        done = driver.submit(request("CMD-2"))
        assert done.state == "done" and done.delivered["ir_mohm"] == pytest.approx(21.2)
        assert rig.meter.state()["continuous"] is False
