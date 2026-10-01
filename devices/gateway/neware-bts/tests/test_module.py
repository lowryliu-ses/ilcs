"""设备模块自测：对假 BTS 跑 ILCS 的接入验收清单（含故障项目），再测驱动自己的判断。CI 里必须全过。

    pytest devices/gateway/neware-bts/tests
"""
from __future__ import annotations

from pathlib import Path
import time

import pytest

from ilcs_gateway import Job, Rejected, serve
from ilcs_gateway.testing import acceptance

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
