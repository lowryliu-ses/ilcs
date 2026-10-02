"""真实接口（driver/seabreeze_spec.py）对着一个假的 python-seabreeze 包测：后端选择、只开配置的那一台、报错归一、
断开重连、改积分时间后丢谱，以及驱动经它跑完一次采谱。

不用装 seabreeze：假包只有本模块用到的高层接口（`seabreeze.use`、`Spectrometer.from_serial_number`、
`integration_time_micros`、`integration_time_micros_limits`、`intensities`、`wavelengths`、`max_intensity`、`close`），
核对的是本模块对它的用法；库在真机上的行为要在现场核对（见 README「还没做 / 要现场核对的」）。
"""
from __future__ import annotations

import sys
import threading
import time
import types

import pytest

from ilcs_gateway import Job

from driver import seabreeze_spec
from driver.config import Config
from driver.device import Instrument
from driver.seabreeze_spec import SeaBreezeSpectrometer
from driver.spectro_api import SpectrometerError
from simulator.fake_spectrometer import default_config


class SeaBreezeError(Exception):
    pass


@pytest.fixture()
def library(monkeypatch):
    """假的 python-seabreeze：`seabreeze.use` 必须在取 `seabreeze.spectrometers` 之前调用，和真库一样。"""
    state = types.SimpleNamespace(plugged=True, opened=[], serials=[], used=[], imported=False)

    class Spectrometer:
        def __init__(self, serial: str):
            self.serial_number = serial
            self.model = "QE-PRO"
            self.integration_time_micros_limits = (8_000, 3_600_000_000)
            self.max_intensity = 200000.0
            self.integration = None
            self.reads: list[tuple] = []
            self.fail_next: Exception | None = None
            self.closed = False

        @classmethod
        def from_serial_number(cls, serial=None):
            state.serials.append(serial)
            if not state.plugged:
                raise SeaBreezeError("No unopened device found.")
            spec = cls(serial or "QEP00001")
            state.opened.append(spec)
            return spec

        def wavelengths(self):
            return (800.0, 800.5, 801.0)  # 真库给的是 numpy 数组

        def integration_time_micros(self, us):
            self.integration = us

        def intensities(self, correct_dark_counts=False, correct_nonlinearity=False):
            if self.fail_next is not None:
                error, self.fail_next = self.fail_next, None
                raise error
            if correct_nonlinearity:
                raise SeaBreezeError("This device does not support nonlinearity correction.")
            self.reads.append((self.integration, correct_dark_counts))
            return (1000.0, 1500.0, 1200.0)

        def close(self):
            self.closed = True

    spectrometers = types.ModuleType("seabreeze.spectrometers")
    spectrometers.Spectrometer = Spectrometer
    spectrometers.SeaBreezeError = SeaBreezeError
    spectrometers.list_devices = lambda: list(state.opened)
    package = types.ModuleType("seabreeze")

    def use(backend, force=False, **kwargs):
        if state.imported:
            raise RuntimeError("seabreeze.use has to be called before importing seabreeze.spectrometers")
        state.used.append(backend)

    def attribute(name):
        if name == "spectrometers":
            state.imported = True
            return spectrometers
        raise AttributeError(name)

    package.use = use
    package.__getattr__ = attribute
    monkeypatch.setitem(sys.modules, "seabreeze", package)
    monkeypatch.delitem(sys.modules, "seabreeze.spectrometers", raising=False)
    monkeypatch.setattr(seabreeze_spec, "_BACKEND", None)
    return state


def test_backend_is_chosen_before_the_library_is_loaded_and_only_the_configured_serial_opens(library):
    spec = SeaBreezeSpectrometer("QEP01234", "pyseabreeze")
    info = spec.identity()
    assert library.used == ["pyseabreeze"] and library.imported and library.serials == ["QEP01234"]
    assert info["serial"] == "QEP01234" and info["model"] == "QE-PRO" and info["pixels"] == 3
    assert "pyseabreeze" in info["library"]
    spec.identity()
    assert library.serials == ["QEP01234"], "已经开着：身份读缓存，不去重开"
    SeaBreezeSpectrometer(backend="pyseabreeze").identity()
    assert library.serials[-1] is None, "没配序列号：开第一台找到的"


def test_a_process_sticks_to_one_backend(library):
    SeaBreezeSpectrometer(backend="cseabreeze").identity()
    with pytest.raises(SpectrometerError, match="不能再换成 pyseabreeze"):
        SeaBreezeSpectrometer(backend="pyseabreeze").identity()


def test_unplugged_spectrometer_is_a_module_error_and_reconnects_when_back(library):
    library.plugged = False
    spec = SeaBreezeSpectrometer("QEP01234")
    with pytest.raises(SpectrometerError, match="打不开序列号 QEP01234 的光谱仪"):
        spec.identity()
    library.plugged = True
    assert spec.identity()["serial"] == "QEP01234"


def test_integration_change_flushes_a_scan_and_errors_drop_the_connection(library):
    spec = SeaBreezeSpectrometer(flush_scans=1)
    spec.set_integration_us(500_000)
    assert spec.intensities(dark=True, nonlinearity=False) == [1000.0, 1500.0, 1200.0]
    device = library.opened[-1]
    assert device.reads == [(500_000, False), (500_000, True)], "改积分时间后先丢一张"
    spec.set_integration_us(500_000)
    spec.intensities(dark=True, nonlinearity=False)
    assert len(device.reads) == 3, "积分时间没变：不再丢"
    device.fail_next = OSError("USB 读出超时")
    with pytest.raises(SpectrometerError, match="USB 读出超时"):
        spec.intensities(dark=False, nonlinearity=False)
    assert device.closed, "出错就丢掉这条连接"
    with pytest.raises(SpectrometerError, match="还没设积分时间"):
        spec.intensities(dark=False, nonlinearity=False)  # 不拿设备缺省的积分时间冒充
    spec.set_integration_us(500_000)
    assert library.opened[-1] is not device and library.opened[-1].integration == 500_000, "重连、重设积分时间"


def test_health_does_not_wait_for_a_scan_in_progress(library):
    """一张谱积分几秒：这期间 ILCS 的健康检查读打开时缓存的身份，不去等 USB。"""
    spec = SeaBreezeSpectrometer()
    spec.set_integration_us(100_000)
    device, release = library.opened[-1], threading.Event()
    original = device.intensities

    def slow(**kwargs):
        release.wait(5)
        return original(**kwargs)

    device.intensities = slow
    reader = threading.Thread(target=spec.intensities, args=(False, False))
    reader.start()
    try:
        time.sleep(0.05)
        started = time.monotonic()
        assert spec.identity()["max_intensity"] == 200000.0
        assert time.monotonic() - started < 0.1
    finally:
        release.set()
        reader.join(5)


def test_unsupported_correction_is_a_spectrometer_error(library):
    spec = SeaBreezeSpectrometer()
    spec.set_integration_us(100_000)
    with pytest.raises(SpectrometerError, match="nonlinearity"):
        spec.intensities(dark=False, nonlinearity=True)


def test_limits_wavelengths_and_close(library):
    spec = SeaBreezeSpectrometer()
    assert spec.integration_limits_us() == (8_000, 3_600_000_000)
    assert spec.max_intensity() == 200000.0 and spec.wavelengths() == [800.0, 800.5, 801.0]
    spec.close()
    assert library.opened[-1].closed and spec.identity()["serial"] == "QEP00001" and len(library.opened) == 2


def test_the_driver_runs_a_measurement_through_the_real_interface(library):
    config = Config.parse({**default_config(), "spectrometer": {"serial": "QEP01234"}})
    device = Instrument(SeaBreezeSpectrometer(config.serial, config.backend, flush_scans=config.flush_scans), config)
    command = Job(command_id="CMD-REAL", capability=config.capability, params={"repeats": 2})
    command.handle = device.start(command)
    deadline = time.monotonic() + 5
    status = device.status(command)
    while status.state == "running" and time.monotonic() < deadline:
        time.sleep(0.005)
        status = device.status(command)
    assert status.state == "done", status.error
    assert status.actuals["spectrum"]["y"] == [1000.0, 1500.0, 1200.0] and status.actuals["saturated"] is False
    opened = library.opened[-1]
    assert opened.integration == 1_000_000 and len(opened.reads) == 3, "丢一张 + 采两张"
    assert device.identity()["simulator"] is False and device.fault_target() is None
