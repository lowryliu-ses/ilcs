#!/usr/bin/env python
"""模拟手套箱：Modbus TCP 上给出水、氧读数（ppm），给环境读数连接器联调与测试用。

寄存器（输入寄存器与保持寄存器同一份，0 基地址，float32，高位字在前）：

    0–1  O2 ppm        2–3  H2O ppm        4–5  箱压 mbar（相对）       6  状态字（0 正常，1 再生中，2 传感器故障）

读数在设定值附近小幅波动；`leak()` 模拟漏气（氧、水往上走），`sensor_fault()` 让读数变成故障码 -9999。

    python devices/connectors/environment/glovebox_sim.py --port 5021
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import random
import struct
import threading
import time

from pymodbus.datastore import ModbusSequentialDataBlock, ModbusServerContext, ModbusSlaveContext
from pymodbus.server import ModbusTcpServer

log = logging.getLogger("ilcs.glovebox-sim")
FAULT = -9999.0


def float_registers(value: float) -> list[int]:
    raw = struct.pack(">f", value)
    return [int.from_bytes(raw[0:2], "big"), int.from_bytes(raw[2:4], "big")]


class Glovebox:
    def __init__(self, *, o2_ppm: float = 0.5, h2o_ppm: float = 0.3, pressure_mbar: float = 2.0, seed: int = 3,
                 address: str = "127.0.0.1", port: int = 5021, tick_sec: float = 1.0):
        self.o2, self.h2o, self.pressure = o2_ppm, h2o_ppm, pressure_mbar
        self.status = 0
        self.random = random.Random(seed)
        self.address, self.port, self.tick = address, port, tick_sec
        self.block = ModbusSequentialDataBlock(0, [0] * 16)
        self.context = ModbusServerContext(slaves=ModbusSlaveContext(ir=self.block, hr=self.block), single=True)
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.server: ModbusTcpServer | None = None
        self.closed = threading.Event()
        self.lock = threading.Lock()
        self._write()

    # ---------- 状态 ----------

    def set(self, *, o2_ppm: float | None = None, h2o_ppm: float | None = None, status: int | None = None) -> None:
        with self.lock:
            self.o2 = self.o2 if o2_ppm is None else o2_ppm
            self.h2o = self.h2o if h2o_ppm is None else h2o_ppm
            self.status = self.status if status is None else status
        self._write()

    def leak(self, o2_ppm: float = 12.0, h2o_ppm: float = 8.0) -> None:
        self.set(o2_ppm=o2_ppm, h2o_ppm=h2o_ppm)

    def sensor_fault(self) -> None:
        self.set(o2_ppm=FAULT, h2o_ppm=FAULT, status=2)

    def _write(self) -> None:
        with self.lock:
            values = float_registers(self.o2) + float_registers(self.h2o) + float_registers(self.pressure) + [self.status]
        self.block.setValues(1, values)  # 数据块 0 基，setValues 的地址从 1 起（pymodbus 的约定）

    def _drift(self) -> None:
        while not self.closed.wait(self.tick):
            with self.lock:
                if self.status == 0:
                    self.o2 = max(0.05, self.o2 + self.random.uniform(-0.02, 0.02))
                    self.h2o = max(0.05, self.h2o + self.random.uniform(-0.02, 0.02))
            self._write()

    # ---------- 服务 ----------

    async def _listen(self) -> ModbusTcpServer:
        server = ModbusTcpServer(self.context, address=(self.address, self.port))
        await server.serve_forever(background=True)
        return server

    def start(self, *, drift: bool = False) -> "Glovebox":
        self.server = asyncio.run_coroutine_threadsafe(self._listen(), self.loop).result(10)
        if drift:
            threading.Thread(target=self._drift, daemon=True).start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:  # 等端口真的能连
            import socket

            try:
                socket.create_connection((self.address, self.port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.05)
        log.info("模拟手套箱已启动：%s:%s", self.address, self.port)
        return self

    def stop(self) -> None:
        self.closed.set()
        if self.server is not None:
            asyncio.run_coroutine_threadsafe(self.server.shutdown(), self.loop).result(10)
            self.server = None
        self.loop.call_soon_threadsafe(self.loop.stop)


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--address", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=5021)
    parser.add_argument("--o2", type=float, default=0.5)
    parser.add_argument("--h2o", type=float, default=0.3)
    args = parser.parse_args(argv)
    box = Glovebox(o2_ppm=args.o2, h2o_ppm=args.h2o, address=args.address, port=args.port).start(drift=True)
    stop = threading.Event()
    try:
        stop.wait()
    except KeyboardInterrupt:
        box.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
