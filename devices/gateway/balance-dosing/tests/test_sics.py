"""天平 / Quantos 客户端（driver/sics.py）对着假天平（simulator/sics_server.py）走真实的 TCP 收发。"""
from __future__ import annotations

import threading

import pytest

from driver.sics import Balance, Quantos, QuantosError, SicsBusy, SicsError
from simulator.sics_server import FakeScale, ScaleServer
from simulator.world import Head, World


@pytest.fixture()
def rig():
    world = World(settle_sec=0.05)
    world.heads = [Head("LiPF6"), Head("LiFSI", remaining_doses=0)]
    world.mounted = 0
    scale = FakeScale(world, dose_seconds=0.2, stable_wait_sec=0.5)
    server = ScaleServer(scale)
    balance = Balance({"kind": "tcp", "host": "127.0.0.1", "port": server.port, "timeout_sec": 3})
    try:
        yield world, scale, balance, Quantos(balance, dose_timeout_sec=10, action_timeout_sec=5)
    finally:
        balance.link.close()
        server.stop()


def test_identity_weight_tare_and_units(rig):
    world, _, balance, _ = rig
    info = balance.identity()
    assert info["model"] == "XPE206DRQ" and info["serial"].startswith("ILCS-SIMULATOR") and info["simulator"]
    assert balance.stable_weight(2) == pytest.approx(world.vessel_g)
    assert balance.tare(2) == pytest.approx(world.vessel_g)
    world.add(1.2345)
    assert balance.stable_weight(2) == pytest.approx(1.2345, abs=1e-5)
    balance.ensure_ready()


def test_overload_and_unstable_readings_are_explicit(rig):
    world, scale, balance, _ = rig
    world.add(500)
    with pytest.raises(SicsError, match="过载"):
        balance.stable_weight(1)
    with pytest.raises(SicsError, match="过载"):
        balance.ensure_ready()
    world.place_vessel()
    world.settle_sec = 30  # 一直不稳定
    world.add(0.1)
    with pytest.raises(SicsBusy, match="不稳定"):
        balance.stable_weight(0.6)


def test_quantos_head_and_a_full_dose(rig):
    world, _, balance, quantos = rig
    head = quantos.head()
    assert head["substance"] == "LiPF6" and head["remaining_doses"] == 999 and head["remaining_g"] == pytest.approx(50)
    balance.tare(2)
    quantos.begin(0.5, 2, "ILCS-TEST")
    mass = quantos.finish()
    assert mass == pytest.approx(0.5, rel=0.01) and world.net() == pytest.approx(mass, abs=1e-6)
    assert quantos.head()["remaining_doses"] == 998


def test_quantos_refusals_happen_before_dosing(rig):
    world, _, _, quantos = rig
    world.mounted = 1  # 剂次用完的加样头
    with pytest.raises(QuantosError) as caught:
        quantos.begin(0.5, 2, "ILCS-TEST")
    assert caught.value.code == 11 and world.net() == pytest.approx(world.vessel_g)
    world.mounted = None
    assert quantos.head() is None


def test_powder_flow_error_and_stop(rig):
    world, scale, balance, quantos = rig
    balance.tare(2)
    scale.powder_flow_error = True
    quantos.begin(0.5, 2, "ILCS-TEST")
    with pytest.raises(QuantosError) as caught:
        quantos.finish()
    assert caught.value.code == 7 and "已加" in str(caught.value)
    scale.dose_seconds = 5
    quantos.begin(0.5, 2, "ILCS-TEST")
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(InterruptedError, match="终止"):
        quantos.finish(cancel)
