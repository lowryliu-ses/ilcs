"""设备模块自测：对假 BTS 跑 ILCS 的接入验收清单（含故障项目），再测驱动自己的判断。CI 里必须全过。

    pytest devices/gateway/neware-bts/tests
"""
from __future__ import annotations

import json
from pathlib import Path
import time

import pytest

from ilcs_gateway import Job, ReceiptLost, Rejected, serve
from ilcs_gateway.testing import acceptance

from driver.bts_api import BtsRefused
from driver.config import Config
from driver.device import CAPABILITY, Instrument, barcode_of
from simulator.fake_bts import FakeBts, default_config

CONFIG = Config.parse(default_config())
DEVICE_ID = CONFIG.device_id
SUPPORTS = {"hold": False, "abort": True, "query": True, "dedup": True}


def instrument(*, channels: int = 8, run_seconds: float = 60, **changes) -> tuple[Instrument, FakeBts]:
    config = Config.parse({**default_config(channels=channels), **changes})
    bts = FakeBts(list(config.channels), run_seconds=run_seconds)
    return Instrument(bts, config), bts


def job(command_id: str, **params) -> Job:
    return Job(command_id=command_id, capability=CAPABILITY, params=params, method={"program": "CC-CV"})


@pytest.fixture()
def gateway(tmp_path: Path):
    secrets = tmp_path / "secrets"
    bts = FakeBts(list(CONFIG.channels), run_seconds=0.5)
    server = serve(Instrument(bts, CONFIG), device_id=DEVICE_ID, state_dir=tmp_path / "state", address="127.0.0.1",
                   port=0, token_file=secrets / f"{DEVICE_ID}.token", cert=secrets / f"{DEVICE_ID}.crt",
                   key=secrets / f"{DEVICE_ID}.key", host_name="localhost")
    try:
        yield server, bts, secrets
    finally:
        server.stop()


def test_module_passes_the_ilcs_acceptance_checklist(gateway, tmp_path):
    server, bts, secrets = gateway
    report = acceptance(
        f"https://localhost:{server.port}/api/v1", token_file=secrets / f"{DEVICE_ID}.token",
        ca_file=secrets / f"{DEVICE_ID}.crt", capability=CAPABILITY, params={}, expected_device_id=DEVICE_ID,
        state_root=tmp_path / "ilcs", supports=SUPPORTS,
    )
    states = {check.key: check.state for check in report.checks}
    assert report.ok, report.markdown()
    assert states.pop("hold") == "skip", "BTS 接口不做保持，契约如实声明"
    assert all(state == "pass" for state in states.values()), states
    assert report.simulator and report.identity["methods"], "方法目录由网关配置的工步自报"


def test_gateway_restart_still_answers_by_command_id(gateway, tmp_path):
    """网关进程重启：台账在盘上，按原指令号照样查得到；同一指令号再投不会让通道再启动一次。"""
    server, bts, secrets = gateway
    first = server.gateway.submit({"command_id": "CMD-1", "capability": CAPABILITY, "params": {"channel": 3}})
    assert first["state"] == "running" and bts.faults.motions == 1
    server.stop()
    again = serve(Instrument(bts, CONFIG), device_id=DEVICE_ID, state_dir=tmp_path / "state", address="127.0.0.1",
                  port=0, token_file=secrets / f"{DEVICE_ID}.token", cert=secrets / f"{DEVICE_ID}.crt",
                  key=secrets / f"{DEVICE_ID}.key", host_name="localhost")
    try:
        assert again.gateway.query("CMD-1")["delivered"]["channel"] == "1-1-3"
        replay = again.gateway.submit({"command_id": "CMD-1", "capability": CAPABILITY, "params": {"channel": 3}})
        assert replay["state"] in {"running", "done"} and bts.faults.motions == 1, "重投回放台账，不再动设备"
    finally:
        again.stop()


@pytest.mark.parametrize(("capability", "params", "program", "kind"), [
    ("cap.not_this_device", {"channel": 1}, "CC-CV", "unsupported"),
    (CAPABILITY, {"channel": 1}, "UNKNOWN-PROGRAM", "invalid"),
    (CAPABILITY, {"channel": 1, "rate": 0.5}, "CC-CV", "invalid"),   # 工艺参数在工步文件里，不悄悄忽略
    (CAPABILITY, {"channel": 9}, "CC-CV", "invalid"),                # 白名单只有 8 个
    (CAPABILITY, {"channel": "21-1-1"}, "CC-CV", "invalid"),         # 不在白名单的通道号
    (CAPABILITY, {"channel": True}, "CC-CV", "invalid"),
])
def test_driver_rejects_what_the_device_cannot_do(capability, params, program, kind):
    device, bts = instrument()
    with pytest.raises(Rejected) as caught:
        device.start(Job(command_id="CMD-X", capability=capability, params=params, method={"program": program}))
    assert caught.value.kind == kind and bts.faults.motions == 0


def test_missing_step_file_is_rejected(tmp_path):
    device, bts = instrument(programs={"CC-CV": {"file": str(tmp_path / "gone.xml")}})
    with pytest.raises(Rejected, match="不在网关这台机器上"):
        device.start(job("CMD-X", channel=1))
    assert bts.faults.motions == 0


def test_channel_is_required_unless_auto_channel_is_on():
    device, bts = instrument(auto_channel=False)
    with pytest.raises(Rejected, match="由 ILCS 指定"):
        device.start(job("CMD-X"))
    assert device.start(job("CMD-A", channel=2)) == "1-1-2"
    assert device.start(job("CMD-B", channel="1-1-5")) == "1-1-5"
    assert bts.channels(["1-1-2"])["1-1-2"]["barcode"] == barcode_of("CMD-A")


def test_busy_channel_and_interlock_are_explicit_rejections():
    device, bts = instrument(channels=2)
    device.start(job("CMD-A", channel=1))
    with pytest.raises(Rejected) as busy:
        device.start(job("CMD-B", channel=1))
    assert busy.value.kind == "busy"
    assert device.start(job("CMD-C")) == "1-1-2", "自动挑通道跳过在用的"
    with pytest.raises(Rejected) as full:
        device.start(job("CMD-D"))
    assert full.value.kind == "busy"
    bts.manual("1-1-1", workstatus="finish")
    bts.faults.set_fault("interlock")
    with pytest.raises(Rejected) as interlock:
        device.start(job("CMD-E", channel=1))
    assert interlock.value.kind == "interlocked" and bts.faults.motions == 2


def test_channel_just_started_counts_as_busy_until_bts_shows_it():
    """BTS 刷新有延迟：刚启动的通道还报 finish、条码还是旧的，这时不能再往上派。"""
    device, bts = instrument(channels=1)
    device.start(job("CMD-A", channel=1))
    bts.manual("1-1-1", workstatus="finish", barcode="OLD")  # 模拟 BTS 还没刷新
    with pytest.raises(Rejected) as busy:
        device.start(job("CMD-B", channel=1))
    assert busy.value.kind == "busy"
    bts.manual("1-1-1", workstatus="finish", barcode=barcode_of("CMD-A"))  # 刷出来了、也跑完了
    assert device.start(job("CMD-B", channel=1)) == "1-1-1"


def test_bts_unreadable_before_start_is_a_rejection_not_unknown():
    device, bts = instrument()

    def offline(_pipelines):
        raise ConnectionError("BTS 没响应")

    bts.channels = offline
    with pytest.raises(Rejected) as caught:
        device.start(job("CMD-X", channel=1))
    assert caught.value.kind == "busy" and bts.faults.motions == 0


@pytest.mark.parametrize(("workstatus", "state", "error"), [
    ("working", "running", ""), ("pause", "held", ""), ("finish", "done", ""),
    ("stop", "failed", "在 BTS 上被停止"), ("protect", "failed", "保护停机"),
])
def test_status_maps_bts_workstatus(workstatus, state, error):
    device, bts = instrument()
    started = job("CMD-A", channel=1)
    started.handle = device.start(started)
    bts.manual("1-1-1", workstatus=workstatus)
    status = device.status(started)
    assert status.state == state and error in status.error
    assert status.actuals["channel"] == "1-1-1" and status.actuals["bts_barcode"] == barcode_of("CMD-A")


def test_unknown_workstatus_or_foreign_barcode_is_not_a_conclusion():
    device, bts = instrument()
    started = job("CMD-A", channel=1)
    started.handle = device.start(started)
    bts.manual("1-1-1", workstatus="wait")
    with pytest.raises(RuntimeError, match="没有映射"):
        device.status(started)
    bts.manual("1-1-1", workstatus="working", barcode="SOMEONE-ELSE")
    with pytest.raises(RuntimeError, match="不是本作业的"):
        device.status(started)


def test_abort_never_stops_someone_elses_test():
    device, bts = instrument()
    started = job("CMD-A", channel=1)
    started.handle = device.start(started)
    bts.manual("1-1-1", workstatus="working", barcode="MANUAL-CELL-07")  # 通道被人工接着用了
    with pytest.raises(Rejected, match="没有停"):
        device.abort(started)
    assert bts.channels(["1-1-1"])["1-1-1"]["workstatus"] == "working"
    bts.manual("1-1-1", barcode=barcode_of("CMD-A"))
    device.abort(started)
    assert bts.channels(["1-1-1"])["1-1-1"]["workstatus"] == "stop"


def test_lookup_finds_the_job_by_barcode_after_a_lost_receipt():
    device, bts = instrument(run_seconds=60)
    bts.faults.set_fault("lost_receipt")
    lost = job("CMD-LOST", channel=4)
    with pytest.raises(Exception):  # noqa: B017  ReceiptLost：网关记成在途、按指令号找回
        device.start(lost)
    assert device.lookup(lost) == "1-1-4"
    assert device.lookup(job("CMD-NEVER")) is None


def test_misconfigured_channel_fails_identity():
    config = Config.parse(default_config())
    device = Instrument(FakeBts(["1-1-1"]), config)
    with pytest.raises(RuntimeError, match="不在 BTS 上"):
        device.identity()


def test_config_problems_are_reported_together():
    with pytest.raises(ValueError) as caught:
        Config.parse({"channels": ["21-1", "21-1-1", "21-1-1"], "programs": {"A": {}}, "default_program": "B"})
    message = str(caught.value)
    for fragment in ("device_id", "设备号-子设备号-通道号", "重复", "要写 file", "default_program"):
        assert fragment in message


def test_simulated_run_finishes():
    device, bts = instrument(run_seconds=0.05)
    started = job("CMD-A", channel=1)
    started.handle = device.start(started)
    time.sleep(0.1)
    status = device.status(started)
    assert status.state == "done" and status.actuals["capacity"] > 0


# ---------- 一条指令几颗电芯（ILCS 逐孔参数 wells） ----------

def wells_job(command_id: str, wells: dict, **params) -> Job:
    return Job(command_id=command_id, capability=CAPABILITY, params={"wells": wells, **params},
               method={"program": "CC-CV"})


def test_each_well_starts_its_own_channel_with_its_own_barcode():
    device, bts = instrument(auto_channel=False)
    started = wells_job("CMD-W", {"A1": {"channel": 3}, "A2": {"channel": 5.0}, "A3": {"channel": "1-1-1"}})
    started.handle = device.start(started)
    assert json.loads(started.handle) == {"A1": "1-1-3", "A2": "1-1-5", "A3": "1-1-1"}
    rows = bts.channels(["1-1-3", "1-1-5", "1-1-1"])
    assert {rows[p]["barcode"] for p in rows} == {barcode_of("CMD-W", w) for w in ("A1", "A2", "A3")}
    assert bts.faults.motions == 3
    status = device.status(started)
    assert status.state == "running" and set(status.actuals["wells"]) == {"A1", "A2", "A3"}
    assert status.actuals["wells"]["A2"]["channel"] == "1-1-5"
    assert {point["metric"] for point in status.telemetry} >= {"voltage@A1", "current@A3"}


def test_wells_fall_back_to_the_step_channel_but_never_share_one():
    device, bts = instrument(auto_channel=False)
    assert json.loads(device.start(wells_job("CMD-1", {"A1": {}}, channel=2))) == {"A1": "1-1-2"}
    with pytest.raises(Rejected, match="一个通道只能放一颗电芯"):
        device.start(wells_job("CMD-2", {"B1": {"channel": 4}, "B2": {"channel": 4}}))
    with pytest.raises(Rejected, match="一个通道只能放一颗电芯"):
        device.start(wells_job("CMD-3", {"C1": {"channel": 6}, "C2": {}}, channel=6))
    with pytest.raises(Rejected, match="孔位 D2"):
        device.start(wells_job("CMD-4", {"D1": {"channel": 7}, "D2": {}}))
    assert bts.faults.motions == 1


@pytest.mark.parametrize("wells", [{}, {"A1": 3}, {"A1": {"channel": 1, "rate": 1}}, {"": {"channel": 1}}])
def test_malformed_wells_are_rejected(wells):
    device, bts = instrument()
    with pytest.raises(Rejected) as caught:
        device.start(wells_job("CMD-X", wells))
    assert caught.value.kind == "invalid" and bts.faults.motions == 0


def test_one_busy_channel_blocks_the_whole_command():
    device, bts = instrument(auto_channel=False)
    device.start(job("CMD-A", channel=2))
    with pytest.raises(Rejected) as busy:
        device.start(wells_job("CMD-W", {"A1": {"channel": 1}, "A2": {"channel": 2}}))
    assert busy.value.kind == "busy" and bts.faults.motions == 1, "一个都不启动"


def test_auto_channel_gives_each_well_a_distinct_free_channel():
    device, bts = instrument(channels=3)
    device.start(job("CMD-A", channel=2))
    layout = json.loads(device.start(wells_job("CMD-W", {"A1": {}, "A2": {"channel": 3}})))
    assert layout == {"A1": "1-1-1", "A2": "1-1-3"}
    with pytest.raises(Rejected, match="空闲的白名单通道只有 0 个"):
        device.start(wells_job("CMD-X", {"B1": {}}))


def test_partial_start_is_unknown_not_a_rejection():
    """第二个通道被 BTS 拒：第一个已经在跑了，不能报「没动」，也不自动去停它。"""
    device, bts = instrument(auto_channel=False)
    original = bts.start

    def refuse_second(pipeline, barcode, step_file, save_dir):
        if pipeline == "1-1-2":
            raise BtsRefused("通道保护中")
        original(pipeline, barcode, step_file, save_dir)

    bts.start = refuse_second
    command = wells_job("CMD-P", {"A1": {"channel": 1}, "A2": {"channel": 2}})
    with pytest.raises(RuntimeError, match="只做了一部分"):
        device.start(command)
    assert bts.channels(["1-1-1"])["1-1-1"]["workstatus"] == "working"
    assert device.lookup(command) is None, "只找到一部分：不认，交人核查"


def test_multi_well_status_waits_for_every_cell_and_names_the_failed_one():
    device, bts = instrument(auto_channel=False)
    started = wells_job("CMD-W", {"A1": {"channel": 1}, "A2": {"channel": 2}})
    started.handle = device.start(started)
    bts.manual("1-1-1", workstatus="finish")
    assert device.status(started).state == "running"
    bts.manual("1-1-2", workstatus="protect")
    failed = device.status(started)
    assert failed.state == "failed" and "孔位 A2 1-1-2 BTS 保护停机" in failed.error
    bts.manual("1-1-2", workstatus="finish")
    done = device.status(started)
    assert done.state == "done" and done.error == ""
    assert done.actuals["wells"]["A1"]["capacity"] >= 0 and done.actuals["wells"]["A2"]["bts_barcode"] == barcode_of("CMD-W", "A2")


def test_multi_well_abort_stops_only_this_commands_cells():
    device, bts = instrument(auto_channel=False)
    started = wells_job("CMD-W", {"A1": {"channel": 1}, "A2": {"channel": 2}})
    started.handle = device.start(started)
    bts.manual("1-1-2", workstatus="working", barcode="MANUAL-CELL-09")
    device.abort(started)
    rows = bts.channels(["1-1-1", "1-1-2"])
    assert rows["1-1-1"]["workstatus"] == "stop" and rows["1-1-2"]["workstatus"] == "working"


def test_lost_receipt_on_a_multi_well_start_is_found_again_by_barcodes():
    device, bts = instrument(auto_channel=False)
    bts.faults.set_fault("lost_receipt")
    command = wells_job("CMD-L", {"A1": {"channel": 4}, "A2": {"channel": 6}})
    with pytest.raises(ReceiptLost) as lost:
        device.start(command)
    assert json.loads(lost.value.handle) == {"A1": "1-1-4", "A2": "1-1-6"} and bts.faults.motions == 2
    assert device.lookup(command) == lost.value.handle


def test_gateway_runs_a_multi_well_command_end_to_end(tmp_path: Path):
    config = Config.parse({**default_config(), "auto_channel": False})
    bts = FakeBts(list(config.channels), run_seconds=0.2)
    server = serve(Instrument(bts, config), device_id=DEVICE_ID, state_dir=tmp_path / "state", address="127.0.0.1",
                   port=0, token_file=tmp_path / "s" / "t.token", cert=tmp_path / "s" / "c.crt",
                   key=tmp_path / "s" / "k.key", host_name="localhost")
    try:
        body = {"command_id": "CMD-E2E", "capability": CAPABILITY, "method": {"program": "CC-CV"},
                "params": {"wells": {"A1": {"channel": 1.0}, "A2": {"channel": 2.0}}}}
        assert server.gateway.submit(body)["state"] == "running"
        time.sleep(0.3)
        receipt = server.gateway.query("CMD-E2E")
        assert receipt["state"] == "done", receipt
        assert set(receipt["delivered"]["wells"]) == {"A1", "A2"}
        assert server.gateway.submit(body)["state"] == "done" and bts.faults.motions == 2, "重投回放，不再启动"
    finally:
        server.stop()
