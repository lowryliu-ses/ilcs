"""网关配置（`--config` 指的那份 JSON，样例见 config.example.json）：连哪台光谱仪、激光波长、积分时间与采谱程序。

    {
      "device_id": "RAMAN-01", "model": "QE Pro", "vendor": "Ocean Insight", "capability": "cap.ely.raman",
      "spectrometer": {"backend": "cseabreeze", "serial": "QEP01234", "flush_scans": 1},
      "laser": {"kind": "external", "wavelength_nm": 785},
      "integration_ms": 1000, "max_integration_ms": 10000, "max_repeats": 5,
      "shift_range_cm1": [150, 2000], "correct_dark_counts": false, "correct_nonlinearity": false,
      "programs": {"RAMAN": {"name": "拉曼谱（积分 1 s）", "integration_ms": 1000}},
      "default_program": "RAMAN", "max_points": 20000
    }

- `spectrometer.serial`：只开这一台（USB 上插着几台时必须写）；空 = 第一台找到的。`backend`：python-seabreeze 的
  cseabreeze（缺省）或 pyseabreeze。`flush_scans`：改积分时间后先丢几张谱（第一张可能还是按旧积分时间采的）。
- `laser`：激光由外部开关与联锁（`kind` 只能是 external），网关只用标称波长把波长换算成拉曼位移。
- `integration_ms`：程序没写积分时间时用它；`max_integration_ms`：指令带的积分时间不能超过它（也不能超出光谱仪自己的范围）。
- `max_repeats`：一条指令最多采几张谱取平均，和 ILCS 工位能力极限里 `repeats` 的上限一致。
- `shift_range_cm1`：回报的谱图裁到这个拉曼位移范围（与光谱仪覆盖范围取交集）；`max_points`：超过就合并相邻像素。
- `programs`：ILCS 设备方法的「程序」选用哪一个（积分时间预设）；没带方法的指令（包括接入验收）用 `default_program`。
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
from typing import Any

BACKENDS = ("cseabreeze", "pyseabreeze")
# ILCS 曲线型指标每条曲线缺省最多 2 万点（api/app/domain/series.py 的 MAX_POINTS）
MAX_POINTS = 20000


@dataclass(frozen=True)
class Program:
    name: str
    integration_ms: float | None = None


@dataclass(frozen=True)
class Config:
    device_id: str
    laser_nm: float
    programs: dict[str, Program]
    model: str = ""
    vendor: str = "Ocean Insight"
    capability: str = "cap.ely.raman"
    serial: str = ""
    backend: str = "cseabreeze"
    flush_scans: int = 1
    integration_ms: float = 1000.0
    max_integration_ms: float = 10000.0
    max_repeats: int = 10
    shift_range: tuple[float, float] = (150.0, 2000.0)
    correct_dark_counts: bool = False
    correct_nonlinearity: bool = False
    default_program: str = ""
    max_points: int = MAX_POINTS

    @classmethod
    def parse(cls, data: dict[str, Any]) -> "Config":
        problems: list[str] = []
        device_id = str(data.get("device_id") or "").strip()
        if not device_id:
            problems.append("缺 device_id（这台光谱仪在 ILCS 登记的设备编号）")
        capability = str(data.get("capability") or "cap.ely.raman").strip()
        spectrometer = data.get("spectrometer") or {}
        if not isinstance(spectrometer, dict):
            problems.append("spectrometer 要写成 {\"backend\", \"serial\", \"flush_scans\"}")
            spectrometer = {}
        backend = str(spectrometer.get("backend") or "cseabreeze")
        if backend not in BACKENDS:
            problems.append(f"spectrometer.backend 只能是 {' / '.join(BACKENDS)}，不是 {backend}")
        flush = spectrometer.get("flush_scans", 1)
        if not _integer(flush) or flush < 0:
            problems.append("spectrometer.flush_scans 要写不小于 0 的整数")
        laser = data.get("laser") or {}
        laser_nm = laser.get("wavelength_nm") if isinstance(laser, dict) else None
        if not isinstance(laser, dict) or laser.get("kind", "external") != "external":
            problems.append("laser.kind 只能是 external：激光由外部开关与联锁，网关不控制激光")
        if not _positive(laser_nm):
            problems.append("laser.wavelength_nm 要写激光的标称波长（nm，如 785）：拉曼位移按它换算")
        integration = data.get("integration_ms", 1000)
        ceiling = data.get("max_integration_ms", 10000)
        if not _positive(ceiling):
            problems.append("max_integration_ms 要写正数（毫秒）")
            ceiling = math.inf
        if not _positive(integration) or integration > ceiling:
            problems.append(f"integration_ms 要写不超过 max_integration_ms 的正数（毫秒），不是 {integration!r}")
        repeats = data.get("max_repeats", 10)
        if not _integer(repeats) or repeats < 1:
            problems.append("max_repeats 要写不小于 1 的整数（和 ILCS 工位能力极限里 repeats 的上限一致）")
        window = data.get("shift_range_cm1", [150, 2000])
        if not (isinstance(window, (list, tuple)) and len(window) == 2 and all(_number(v) for v in window)
                and 0 <= window[0] < window[1]):
            problems.append("shift_range_cm1 要写 [下限, 上限]（cm-1，0 ≤ 下限 < 上限）")
            window = (150, 2000)
        points = data.get("max_points", MAX_POINTS)
        if not _integer(points) or not 2 <= points <= MAX_POINTS:
            problems.append(f"max_points 要写 2–{MAX_POINTS} 的整数（ILCS 曲线每条最多 {MAX_POINTS} 点）")
        flags = {}
        for key in ("correct_dark_counts", "correct_nonlinearity"):
            flags[key] = data.get(key, False)
            if not isinstance(flags[key], bool):
                problems.append(f"{key} 要写 true / false")
        programs: dict[str, Program] = {}
        raw_programs = data.get("programs") or {}
        if not isinstance(raw_programs, dict):
            problems.append("programs 要写成 {程序键: {\"name\", \"integration_ms\"}}")
            raw_programs = {}
        for code, item in raw_programs.items():
            if not isinstance(item, dict):
                problems.append(f"程序 {code} 要写成 {{\"name\", \"integration_ms\"}}")
                continue
            unknown = sorted(set(item) - {"name", "integration_ms"})
            if unknown:
                problems.append(f"程序 {code} 只认 name 与 integration_ms，不认 {', '.join(unknown)}")
            value = item.get("integration_ms")
            if value is not None and (not _positive(value) or value > ceiling):
                problems.append(f"程序 {code} 的 integration_ms 要写不超过 max_integration_ms 的正数（毫秒）")
                continue
            programs[str(code)] = Program(name=str(item.get("name") or code),
                                          integration_ms=float(value) if value is not None else None)
        if not programs:
            problems.append("programs 是空的：至少登记一个采谱程序")
        default = str(data.get("default_program") or "")
        if default and default not in programs:
            problems.append(f"default_program {default} 不在 programs 里")
        if problems:
            raise ValueError("网关配置有问题：" + "；".join(problems))
        return cls(
            device_id=device_id, laser_nm=float(laser_nm), programs=programs, model=str(data.get("model") or ""),
            vendor=str(data.get("vendor") or "Ocean Insight"), capability=capability,
            serial=str(spectrometer.get("serial") or "").strip(), backend=backend, flush_scans=int(flush),
            integration_ms=float(integration), max_integration_ms=float(ceiling), max_repeats=int(repeats),
            shift_range=(float(window[0]), float(window[1])), correct_dark_counts=flags["correct_dark_counts"],
            correct_nonlinearity=flags["correct_nonlinearity"], default_program=default, max_points=int(points),
        )

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        return cls.parse(json.loads(Path(path).read_text(encoding="utf-8")))


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _positive(value: Any) -> bool:
    return _number(value) and value > 0


def _integer(value: Any) -> bool:
    return _number(value) and float(value).is_integer()
