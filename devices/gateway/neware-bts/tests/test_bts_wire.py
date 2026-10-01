"""真实接口（driver/bts.py + aurora-neware）对着假 BTS 的 TCP XML 服务跑：拼命令、解析应答、断线、回执丢失。

要装 aurora-neware（见模块的 requirements.txt）；没装就跳过——ILCS 仓库的 api/.venv 不带它。
"""
from __future__ import annotations

from pathlib import Path
import socket

import pytest

pytest.importorskip("aurora_neware")

from ilcs_gateway import serve  # noqa: E402
from ilcs_gateway.testing import acceptance  # noqa: E402

from driver.bts import AuroraBts  # noqa: E402
from driver.bts_api import BtsOffline, BtsRefused  # noqa: E402
from driver.config import Config  # noqa: E402
from driver.device import CAPABILITY, Instrument  # noqa: E402
from simulator.bts_server import BtsServer  # noqa: E402
from simulator.fake_bts import STEPS, FakeBts, default_config  # noqa: E402

PIPELINES = ["21-1-1", "21-1-2", "21-2-1"]
STEP = str(STEPS / "CC-CV.xml")


@pytest.fixture()
def bts():
    fake = FakeBts(PIPELINES, run_seconds=60)
    server = BtsServer(fake)
    try:
        yield fake, AuroraBts("127.0.0.1", server.port, timeout_sec=3)
    finally:
        server.stop()


def test_reads_channels_and_starts_and_stops_over_the_wire(bts):
    fake, client = bts
    assert client.info()["pipelines"] == sorted(PIPELINES)
    client.start("21-1-2", "ILCS-ABC", STEP, "")
    row = client.channels(["21-1-2"])["21-1-2"]
    assert row["workstatus"] == "working" and row["barcode"] == "ILCS-ABC" and isinstance(row["voltage"], float)
    assert all(isinstance(row[key], (int, float)) for key in ("capacity", "energy", "current")), row  # 含科学计数法
    assert fake.rows["21-1-2"]["step_file"] == STEP
    client.stop("21-1-2")
    assert client.channels(["21-1-2"])["21-1-2"]["workstatus"] == "stop"


def test_explicit_refusals_are_bts_refused(bts):
    fake, client = bts
    with pytest.raises(BtsRefused):
        client.start("9-9-9", "X", STEP, "")          # 不在 BTS 上：没发
    with pytest.raises(BtsRefused):
        client.start("21-1-1", "X", "/no/such.xml", "")  # 工步文件不在：没发
    client.start("21-1-1", "A", STEP, "")
    with pytest.raises(BtsRefused):
        client.start("21-1-1", "B", STEP, "")          # 通道在跑：没发
    fake.faults.set_fault("busy")
    with pytest.raises(BtsRefused):
        client.start("21-1-2", "C", STEP, "")          # BTS 回 false
    assert fake.faults.motions == 1


def test_lost_receipt_is_unknown_not_refused_and_the_client_reconnects(bts):
    fake, client = bts
    fake.faults.set_fault("lost_receipt")
    with pytest.raises(Exception) as caught:
        client.start("21-1-1", "LOST", STEP, "")
    assert not isinstance(caught.value, BtsRefused), "发出去了没回：结果未知，不能当拒绝"
    assert fake.faults.motions == 1
    fake.faults.set_fault("none")
    assert client.channels(["21-1-1"])["21-1-1"]["barcode"] == "LOST", "丢掉坏连接、下次重连"


def test_unreachable_bts_before_start_is_offline():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with pytest.raises(BtsOffline):
        AuroraBts("127.0.0.1", port, timeout_sec=1).start("21-1-1", "X", STEP, "")


def test_gateway_over_the_real_client_passes_acceptance(tmp_path: Path):
    """整条线：ILCS http_json_v1 → 网关 → driver/bts.py → aurora-neware → TCP → 假 BTS。故障项目走不了统一控制口，不测。"""
    config = Config.parse({**default_config(), "channels": PIPELINES})
    fake = FakeBts(PIPELINES, run_seconds=0.5)
    bts_server = BtsServer(fake)
    secrets = tmp_path / "secrets"
    device_id = config.device_id
    server = serve(Instrument(AuroraBts("127.0.0.1", bts_server.port, timeout_sec=3), config), device_id=device_id,
                   state_dir=tmp_path / "state", address="127.0.0.1", port=0,
                   token_file=secrets / f"{device_id}.token", cert=secrets / f"{device_id}.crt",
                   key=secrets / f"{device_id}.key", host_name="localhost")
    try:
        report = acceptance(
            f"https://localhost:{server.port}/api/v1", token_file=secrets / f"{device_id}.token",
            ca_file=secrets / f"{device_id}.crt", capability=CAPABILITY, params={}, expected_device_id=device_id,
            state_root=tmp_path / "ilcs", faults=False, supports={"hold": False},
        )
        assert report.ok, report.markdown()
        barcodes = {row["barcode"] for row in fake.rows.values()}
        assert any(code.startswith("ILCS-") for code in barcodes)
    finally:
        server.stop()
        bts_server.stop()
