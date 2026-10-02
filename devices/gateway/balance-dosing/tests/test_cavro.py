"""注射泵客户端（driver/cavro.py）对着假泵（simulator/cavro_server.py）走真实的 TCP 帧。"""
from __future__ import annotations

import threading

import pytest

from driver.cavro import CavroPump, PumpError
from simulator.cavro_server import FakePump, PumpServer
from simulator.world import Reservoir, World


@pytest.fixture()
def rig():
    world = World(settle_sec=0)
    world.reservoirs = {2: Reservoir("EMC", 1.0), 3: Reservoir("DMC", 1.07, volume_ul=100)}
    pump = FakePump(world, syringe_ul=5000, steps=3000, output_port=12, step_seconds=0.00001)
    server = PumpServer(pump)
    client = CavroPump({"link": {"kind": "tcp", "host": "127.0.0.1", "port": server.port}, "syringe_ul": 5000,
                        "steps": 3000})
    try:
        yield world, pump, client
    finally:
        client.link.close()
        server.stop()


def test_transfer_moves_liquid_from_the_source_port_onto_the_pan(rig):
    world, pump, client = rig
    client.check_ready()
    before = world.net()
    steps = client.transfer(2, 12, 1000)
    assert steps == 600 and client.position() == 0 and pump.valve == 12
    assert world.net() - before == pytest.approx(1.0, abs=1e-6)  # 1000 μL × 1.0 g/mL


def test_an_empty_reservoir_pushes_air(rig):
    world, _, client = rig
    before = world.net()
    client.transfer(3, 12, 1000)  # 3 号口只剩 100 μL：吸到的是空气
    assert world.net() == before


def test_uninitialized_pump_is_refused_not_homed(rig):
    world, pump, client = rig
    pump.error = 7
    with pytest.raises(PumpError) as caught:
        client.check_ready()
    assert caught.value.code == 7 and "废液口" in str(caught.value)
    assert pump.plunger == 0 and world.net() == pytest.approx(world.vessel_g)


def test_pump_errors_and_termination(rig):
    world, pump, client = rig
    with pytest.raises(PumpError) as caught:
        client.transfer(13, 12, 100)  # 没有 13 号口
    assert caught.value.code == 3
    pump.error = 0
    pump.step_seconds = 0.01  # 慢一点，来得及终止
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(InterruptedError):
        client.transfer(2, 12, 2500, cancel)
