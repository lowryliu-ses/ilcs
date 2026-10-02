"""真实接口：经开源的 python-seabreeze（MIT，https://github.com/ap--/python-seabreeze）连 Ocean Insight 光谱仪（USB）。

python-seabreeze 有两个后端：cseabreeze（缺省，带 Ocean 的 C++ 库）与 pyseabreeze（纯 Python + pyusb）。后端只能在导入
`seabreeze.spectrometers` 之前选一次（`seabreeze.use`），所以这里到第一次连设备时才导入，一个进程只用一个后端。
这里补上它没管的几件事：

1. **报错归一**：库的 `SeaBreezeError`、USB 错误一律换成 `SpectrometerError`，消息里写明原因；
2. **断开重连**：任何一次调用出错都丢掉这条连接，下次调用重新打开设备——USB 拔插之后不用重启网关；
3. **改积分时间后丢谱**：Ocean 光谱仪多是连续积分，改积分时间后的第一张谱可能还是按旧积分时间采的，
   先丢 `flush_scans` 张（积分时间没变就不丢）；
4. **只开一台、只开一次**：配了序列号就只开那一台；同一时刻只有一个线程在跟设备说话（采谱期间健康检查读缓存的身份，
   不去抢 USB）。

seabreeze 的 `intensities()` 没有超时、也停不下来：一次采谱阻塞约一个积分时间；USB 读出卡死时只能重启网关（见 README）。
"""
from __future__ import annotations

import threading
from typing import Any, Callable

from .spectro_api import SpectrometerError

# 这个进程用的 seabreeze 后端：只能在导入 seabreeze.spectrometers 之前选一次
_BACKEND: str | None = None


def _library(backend: str):
    global _BACKEND
    try:
        import seabreeze

        if _BACKEND is None:
            seabreeze.use(backend)
            _BACKEND = backend
        elif _BACKEND != backend:
            raise SpectrometerError(f"这个进程已经用 {_BACKEND} 后端连过光谱仪，不能再换成 {backend}")
        from seabreeze import spectrometers
    except SpectrometerError:
        raise
    except Exception as exc:  # noqa: BLE001  没装、后端装不上：说清楚是库的问题
        raise SpectrometerError(f"装不上 python-seabreeze（后端 {backend}）：{exc}") from exc
    return spectrometers


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("seabreeze")
    except Exception:  # noqa: BLE001
        return ""


class SeaBreezeSpectrometer:
    def __init__(self, serial: str = "", backend: str = "cseabreeze", *, flush_scans: int = 1):
        self.serial = serial
        self.backend = backend
        self.flush_scans = flush_scans
        self.lock = threading.RLock()
        self._spec: Any = None
        self._info: dict[str, Any] | None = None
        self._integration_us: int | None = None
        self._stale = 0

    # ---------- 连接 ----------

    def _open(self) -> Any:
        if self._spec is not None:
            return self._spec
        library = _library(self.backend)
        try:
            spec = library.Spectrometer.from_serial_number(self.serial or None)
        except Exception as exc:  # noqa: BLE001  没插、被别的程序（OceanView）占着、序列号不对
            where = f"序列号 {self.serial} 的" if self.serial else ""
            raise SpectrometerError(f"打不开{where}光谱仪：{exc}") from exc
        try:
            # 固件版本 seabreeze 的通用接口读不到（不同型号走不同的 feature），留空如实报「未报」
            self._info = {"serial": str(spec.serial_number), "model": str(spec.model),
                          "pixels": len(spec.wavelengths()), "max_intensity": float(spec.max_intensity), "firmware": "",
                          "library": " ".join(filter(None, ["python-seabreeze", _version()])) + f"（{self.backend}）"}
        except Exception as exc:  # noqa: BLE001
            _close(spec)
            raise SpectrometerError(f"光谱仪打开了，但读不到序列号 / 型号 / 波长：{exc}") from exc
        self._spec = spec
        self._integration_us = None  # 新连接：积分时间以设备为准，下次照设
        self._stale = 0
        return spec

    def _drop(self) -> None:
        spec, self._spec, self._info = self._spec, None, None
        if spec is not None:
            _close(spec)

    def _call(self, action: Callable[[Any], Any]) -> Any:
        with self.lock:
            spec = self._open()
            try:
                return action(spec)
            except Exception as exc:  # noqa: BLE001  连接状态不明：丢掉，下次重连
                self._drop()
                raise SpectrometerError(str(exc) or type(exc).__name__) from exc

    # ---------- 调用面（driver/spectro_api.py） ----------

    def identity(self) -> dict[str, Any]:
        info = self._info
        if info is not None and self._spec is not None:
            return dict(info)  # 采谱期间不去抢 USB：身份在打开时读过
        with self.lock:
            self._open()
            return dict(self._info or {})

    def integration_limits_us(self) -> tuple[int, int]:
        low, high = self._call(lambda spec: spec.integration_time_micros_limits)
        return int(low), int(high)

    def set_integration_us(self, us: int) -> None:
        with self.lock:
            if self._spec is not None and self._integration_us == int(us):
                return
            self._call(lambda spec: spec.integration_time_micros(int(us)))
            self._integration_us = int(us)
            self._stale = self.flush_scans

    def wavelengths(self) -> list[float]:
        return [float(value) for value in self._call(lambda spec: spec.wavelengths())]

    def intensities(self, dark: bool, nonlinearity: bool) -> list[float]:
        with self.lock:
            if self._spec is None or self._integration_us is None:
                # 连接断开过：重连后的积分时间是设备的缺省值，不能当成这次要的积分时间去采
                raise SpectrometerError("这条连接上还没设积分时间（刚断开重连过）：这张谱不采")
            while self._stale > 0:
                self._call(lambda spec: spec.intensities())  # 丢掉：可能还是按旧积分时间采的
                self._stale -= 1
            values = self._call(lambda spec: spec.intensities(correct_dark_counts=dark,
                                                              correct_nonlinearity=nonlinearity))
        return [float(value) for value in values]

    def max_intensity(self) -> float:
        return float(self._call(lambda spec: spec.max_intensity))

    def close(self) -> None:
        with self.lock:
            self._drop()


def _close(spec: Any) -> None:
    try:
        spec.close()
    except Exception:  # noqa: BLE001  关不掉不影响下次重开
        pass
