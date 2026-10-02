"""网关配置（`--config` 指的那份 JSON，样例见 config.example.json）：连哪台仪器、接的什么电池、有哪些测量程序。

    {
      "device_id": "ECHEM-01", "model": "EmStat4 HR", "vendor": "PalmSens", "capability": "cap.echem",
      "backend": {"kind": "methodscript", "link": {"kind": "serial", "port": "COM5"}, "timeout_sec": 3,
                  "abort_timeout_sec": 10},
      "cell": {"area_cm2": 2.01},
      "limits": {"e_min_V": -0.5, "e_max_V": 6.0},
      "params": {"scan_rate_V_s": [0.0001, 0.01]},
      "programs": {"OCP-60": {"name": "开路电位 60 s", "technique": "ocp", "duration_s": 60, "interval_s": 1}},
      "default_program": "OCP-60", "max_points": 20000
    }

- `backend.link`：串口（USB 虚拟串口 `COM5` / `/dev/ttyACM0`，或 `rfc2217://主机:端口`）或 TCP（`host`、`port`）。
  串口缺省按型号：EmStat Pico / Sensit 230400 波特 + XON/XOFF，其他（EmStat4、Nexus）921600 波特；直连 UART 的
  EmStat4 再加 `"rtscts": true`。USB 虚拟串口这些设置不起作用。
- `abort_timeout_sec`：终止后等仪器结束测量最多多久（缺省 10 s），等不到回结果未知；要比 ILCS 接入模板的
  `request_timeout_sec` 小（模板写的 20 s），否则 ILCS 那边先超时。
- `cell`：接在这个通道上的电池——`area_cm2` 电极面积（电流换算成电流密度 mA/cm²），`cell_constant_per_cm`
  电导池常数 K（cm⁻¹，σ = K / R_b）。程序里的 `cell` 覆盖这里的。
- `limits`：网关允许加到电池上的电位（V），和 ILCS 工位能力极限一致；仪器自己的极限另按型号核对。
- `params`：ILCS 指令能改的参数及范围（[下限, 上限]），只对用得上它的技术有效（见 driver/techniques.py 的
  OVERRIDES）；不登记就一个都不接受。
- `programs`：ILCS 设备方法的「程序」选用哪一个；没带方法的指令（包括接入验收）用 `default_program`。
  各技术的参数见 driver/techniques.py。
- `max_points`：回报的每条曲线最多几个点（≤ 2 万，ILCS 曲线的缺省上限），超过就合并相邻的点。
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
from pathlib import Path
from typing import Any

from .techniques import INTEGER_KEYS, OVERRIDABLE, Cell, PlanError, Program, make_plan, parse_program

BACKENDS = ("methodscript",)
# ILCS 曲线型指标每条曲线缺省最多 2 万点（api/app/domain/series.py 的 MAX_POINTS）
MAX_POINTS = 20000


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def serial_defaults(model: str) -> dict[str, Any]:
    """串口缺省（EmStat Pico 通讯协议表 1、EmStat4 通讯协议表 1）。"""
    lowered = model.lower()
    if "pico" in lowered or "sensit" in lowered:
        return {"baudrate": 230400, "xonxoff": True, "rtscts": False}
    return {"baudrate": 921600, "xonxoff": False, "rtscts": False}


@dataclass(frozen=True)
class Config:
    device_id: str
    programs: dict[str, Program]
    link: dict[str, Any]
    model: str = ""
    vendor: str = "PalmSens"
    capability: str = "cap.echem"
    backend: str = "methodscript"
    timeout_sec: float = 3.0
    abort_timeout_sec: float = 10.0
    cell: Cell = field(default_factory=Cell)
    e_limits: tuple[float | None, float | None] = (None, None)
    params: dict[str, tuple[float, float]] = field(default_factory=dict)
    default_program: str = ""
    max_points: int = MAX_POINTS

    @classmethod
    def parse(cls, data: dict[str, Any]) -> "Config":
        problems: list[str] = []
        known = {"device_id", "model", "vendor", "capability", "backend", "cell", "limits", "params", "programs",
                 "default_program", "max_points"}
        problems += [f"不认的配置项 {key}" for key in sorted(set(data) - known)]
        device_id = str(data.get("device_id") or "").strip()
        if not device_id:
            problems.append("缺 device_id（这台工作站在 ILCS 登记的设备编号）")
        model = str(data.get("model") or "")
        backend = data.get("backend") or {}
        if not isinstance(backend, dict):
            problems.append("backend 要写成 {\"kind\": \"methodscript\", \"link\": {...}}")
            backend = {}
        kind = str(backend.get("kind") or "methodscript")
        if kind not in BACKENDS:
            problems.append(f"backend.kind 只能是 {' / '.join(BACKENDS)}，不是 {kind}")
        problems += [f"backend 不认 {key}" for key in sorted(set(backend) - {"kind", "link", "timeout_sec",
                                                                           "abort_timeout_sec"})]
        link = backend.get("link") or {}
        if not isinstance(link, dict):
            problems.append("backend.link 要写成 {\"kind\": \"serial\", \"port\": ...} 或 {\"kind\": \"tcp\", \"host\", \"port\"}")
            link = {}
        link_kind = str(link.get("kind") or "serial")
        if link_kind == "tcp" and not (link.get("host") and link.get("port")):
            problems.append("backend.link 是 TCP 时要写 host 与 port")
        elif link_kind == "serial" and not link.get("port"):
            problems.append("backend.link 是串口时要写 port（COM5、/dev/ttyACM0、rfc2217://主机:端口）")
        elif link_kind not in {"tcp", "serial"}:
            problems.append(f"backend.link.kind 只能是 serial 或 tcp，不是 {link_kind}")
        if link_kind == "serial":
            link = {**serial_defaults(model), **link}
        link = {**link, "kind": link_kind}
        timeouts = {}
        for key, default in (("timeout_sec", 3.0), ("abort_timeout_sec", 10.0)):
            value = backend.get(key, default)
            if not _number(value) or value <= 0:
                problems.append(f"backend.{key} 要写正数（秒）")
                value = default
            timeouts[key] = float(value)
        cell, found = Cell.parse(data.get("cell"))
        problems += found
        limits = data.get("limits") or {}
        if not isinstance(limits, dict) or set(limits) - {"e_min_V", "e_max_V"}:
            problems.append("limits 只认 {\"e_min_V\", \"e_max_V\"}（V）")
            limits = {}
        low, high = limits.get("e_min_V"), limits.get("e_max_V")
        if (low is not None and not _number(low)) or (high is not None and not _number(high)) or (
                low is not None and high is not None and low >= high):
            problems.append("limits.e_min_V / e_max_V 要写数，且 e_min_V < e_max_V")
            low = high = None
        params: dict[str, tuple[float, float]] = {}
        raw_params = data.get("params") or {}
        if not isinstance(raw_params, dict):
            problems.append("params 要写成 {参数: [下限, 上限]}")
            raw_params = {}
        for key, span in raw_params.items():
            if key not in OVERRIDABLE:
                problems.append(f"params 里的 {key} 不是能由 ILCS 指令改的参数；可选 {'、'.join(OVERRIDABLE)}")
                continue
            if not (isinstance(span, (list, tuple)) and len(span) == 2 and all(_number(v) for v in span)
                    and span[0] <= span[1]):
                problems.append(f"params.{key} 要写 [下限, 上限]")
                continue
            if key in INTEGER_KEYS and not all(float(v).is_integer() and v >= 1 for v in span):
                problems.append(f"params.{key} 的上下限要是不小于 1 的整数")
                continue
            params[key] = (float(span[0]), float(span[1]))
        programs: dict[str, Program] = {}
        raw_programs = data.get("programs") or {}
        if not isinstance(raw_programs, dict):
            problems.append("programs 要写成 {程序键: {\"name\", \"technique\", ...}}")
            raw_programs = {}
        for code, item in raw_programs.items():
            program, found = parse_program(str(code), item, cell)
            problems += found
            if program is None or found:
                continue
            try:
                make_plan(program, {}, allowed=params, e_limits=(low, high))
            except PlanError as exc:
                problems.append(f"程序 {code}：{exc}")
                continue
            programs[str(code)] = program
        if not raw_programs:
            problems.append("programs 是空的：至少登记一个测量程序")
        default = str(data.get("default_program") or "")
        if default and default not in raw_programs:
            problems.append(f"default_program {default} 不在 programs 里")
        points = data.get("max_points", MAX_POINTS)
        if not _number(points) or not float(points).is_integer() or not 2 <= points <= MAX_POINTS:
            problems.append(f"max_points 要写 2–{MAX_POINTS} 的整数（ILCS 曲线每条最多 {MAX_POINTS} 点）")
            points = MAX_POINTS
        if problems:
            raise ValueError("网关配置有问题：" + "；".join(problems))
        return cls(
            device_id=device_id, programs=programs, link=link, model=model,
            vendor=str(data.get("vendor") or "PalmSens"), capability=str(data.get("capability") or "cap.echem"),
            backend=kind, timeout_sec=timeouts["timeout_sec"], abort_timeout_sec=timeouts["abort_timeout_sec"],
            cell=cell, e_limits=(float(low) if low is not None else None, float(high) if high is not None else None),
            params=params, default_program=default, max_points=int(points),
        )

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        return cls.parse(json.loads(Path(path).read_text(encoding="utf-8")))
