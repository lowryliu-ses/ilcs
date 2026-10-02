"""网关配置（`--config` 指的那份 JSON，样例见 config.example.json）：一个工位上的几块加热搅拌器，每块一条链路。

    {
      "device_id": "EL-D-MIX-01", "model": "RCT digital", "vendor": "IKA", "capability": "cap.ely.stir",
      "ambient_c": 25,
      "positions": {
        "1": {"name": "左前", "link": {"kind": "serial", "port": "COM5"}, "max_temp_c": 80, "max_rpm": 1500,
              "sensor": "external"},
        "2": {"name": "右前", "link": {"kind": "tcp", "host": "192.168.10.31", "port": 4001}}
      },
      "programs": {"STIR-FINAL": {"name": "终混"}}, "default_program": "STIR-FINAL",
      "watchdog_sec": 0
    }

- `positions`：一个位置一块板、一个瓶。键是位置号（ILCS 指令里 `position` 写序号 1 起，或直接写键）；
  链路 `serial` 缺省 9600 / 7 / E / 1（NAMUR），`tcp` 是串口服务器（透明转发）。
- `max_temp_c` 不要高于这块板背面安全温度旋钮的限值（设定值超过它，设备会自己压低）；`min_rpm` 是设备能设的最低转速
  （RCT digital 50 rpm），0 rpm 表示不开搅拌。`sensor`：`external` 报外置 PT1000 探头的温度（瓶里液体），`plate` 报加热盘温度。
- `ambient_c`：这些板只能加热、不能制冷。要求的温度低于它就拒绝；高于它才开加热，等于它只搅拌不加热。
- `auto_position`：指令没带位置时自动挑空闲的位置。只在每个位置都是验收用的空位 / 假瓶时打开。
- `programs`：ILCS 设备方法的「程序」登记在这里（加热板没有内置程序，温度、时间、转速都由指令参数给）。
- `poll_sec`（缺省 2）：计时线程多久读一次温度、转速；`lost_after_sec`（缺省 30）：运行中这么多秒（且至少连续 3 次）
  读不到某块板，算失去监视，提前停下这一瓶、判失败。
- `watchdog_sec`：0 关；20–1500 打开看门狗模式 2——网关挂了不再喂，板子的设定值在这么多秒后回落到
  `watchdog_temp_c`（缺省 = ambient_c）与 `watchdog_rpm`（缺省 0）。行为要在现场核对后再开。
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

SENSORS = {"external": 1, "plate": 2}
EOLS = ("\r\n", " \r\n")


@dataclass(frozen=True)
class Position:
    key: str
    link: dict[str, Any]
    name: str = ""
    max_temp_c: float = 310.0
    max_rpm: float = 1500.0
    min_rpm: float = 50.0
    sensor: str = "plate"

    @property
    def channel(self) -> int:
        """温度读哪个通道：外置探头 IN_PV_1，加热盘 IN_PV_2。"""
        return SENSORS[self.sensor]

    def label(self) -> str:
        return f"位置 {self.key}" + (f"（{self.name}）" if self.name and self.name != self.key else "")


@dataclass(frozen=True)
class Config:
    device_id: str
    positions: dict[str, Position]
    programs: dict[str, str]
    model: str = ""
    vendor: str = "IKA"
    capability: str = "cap.ely.stir"
    ambient_c: float = 25.0
    auto_position: bool = False
    default_program: str = ""
    watchdog_sec: int = 0
    watchdog_temp_c: float = 25.0
    watchdog_rpm: float = 0.0
    # 计时线程多久读一次实测值（遥测、失联判断）；运行中多久读不到某块板算失去监视
    poll_sec: float = 2.0
    lost_after_sec: float = 30.0

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(self.positions)

    @classmethod
    def parse(cls, data: dict[str, Any]) -> "Config":
        problems: list[str] = []
        device_id = str(data.get("device_id") or "").strip()
        if not device_id:
            problems.append("缺 device_id（这个工位在 ILCS 登记的设备编号）")
        ambient = _number(data.get("ambient_c"), 25.0)
        if ambient is None:
            problems.append("ambient_c 要写数（℃）")
            ambient = 25.0
        positions: dict[str, Position] = {}
        raw = data.get("positions")
        if not isinstance(raw, dict) or not raw:
            problems.append("positions 是空的：至少登记一个位置（一块加热板）")
            raw = {}
        for key, item in raw.items():
            key = str(key).strip()
            position, issues = _position(key, item, ambient)
            problems.extend(issues)
            if position is not None:
                positions[key] = position
        programs: dict[str, str] = {}
        for code, item in (data.get("programs") or {}).items():
            if not isinstance(item, dict):
                problems.append(f"程序 {code} 要写成对象，如 {{\"name\": \"终混\"}}")
                continue
            programs[str(code)] = str(item.get("name") or code)
        if not programs:
            problems.append("programs 是空的：至少登记一个程序（ILCS 设备方法的「程序」）")
        default = str(data.get("default_program") or "")
        if default and default not in programs:
            problems.append(f"default_program {default} 不在 programs 里")
        watchdog = data.get("watchdog_sec") or 0
        if isinstance(watchdog, bool) or not isinstance(watchdog, (int, float)) or not (
                watchdog == 0 or 20 <= watchdog <= 1500):
            problems.append("watchdog_sec 只能是 0（关）或 20–1500 秒")
            watchdog = 0
        safe_temp = _number(data.get("watchdog_temp_c"), ambient)
        safe_rpm = _number(data.get("watchdog_rpm"), 0.0)
        if safe_temp is None or safe_rpm is None or safe_rpm < 0:
            problems.append("watchdog_temp_c、watchdog_rpm 要写非负的数")
        poll = _number(data.get("poll_sec"), 2.0)
        if poll is None or poll <= 0:
            problems.append("poll_sec 要写正数（秒）")
        lost = _number(data.get("lost_after_sec"), 30.0)
        if lost is None or lost < 0:
            problems.append("lost_after_sec 要写非负的数（秒）")
        capability = str(data.get("capability") or "cap.ely.stir").strip()
        if problems:
            raise ValueError("网关配置有问题：" + "；".join(problems))
        return cls(
            device_id=device_id, positions=positions, programs=programs, model=str(data.get("model") or ""),
            vendor=str(data.get("vendor") or "IKA"), capability=capability, ambient_c=float(ambient),
            auto_position=bool(data.get("auto_position")), default_program=default, watchdog_sec=int(watchdog),
            watchdog_temp_c=float(safe_temp), watchdog_rpm=float(safe_rpm), poll_sec=float(poll),
            lost_after_sec=float(lost),
        )

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        return cls.parse(json.loads(Path(path).read_text(encoding="utf-8")))


def _number(value: Any, default: float) -> float | None:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _position(key: str, item: Any, ambient: float) -> tuple[Position | None, list[str]]:
    where = f"位置 {key}"
    if not key:
        return None, ["positions 里有空的位置号"]
    if not isinstance(item, dict):
        return None, [f"{where} 要写成对象（link、name、max_temp_c、max_rpm、sensor）"]
    problems: list[str] = []
    link = item.get("link")
    if not isinstance(link, dict) or link.get("kind") not in {"tcp", "serial"}:
        problems.append(f"{where} 的 link 要写 {{\"kind\": \"serial\", \"port\": \"COM5\"}} 或 "
                        "{\"kind\": \"tcp\", \"host\", \"port\"}（串口服务器）")
        link = {}
    elif link["kind"] == "tcp" and not (link.get("host") and link.get("port")):
        problems.append(f"{where} 的 TCP 链路要写 host 与 port")
    elif link["kind"] == "serial":
        if not link.get("port"):
            problems.append(f"{where} 的串口链路要写 port（COM5、/dev/ttyUSB0、rfc2217://主机:端口）")
        link = {"baudrate": 9600, "bytesize": 7, "parity": "E", "stopbits": 1, **link}
        if link["bytesize"] not in (7, 8) or str(link["parity"]).upper() not in {"N", "E", "O"} \
                or link["stopbits"] not in (1, 2):
            problems.append(f"{where} 的串口参数不对：NAMUR 是 9600 波特、7 数据位、偶校验（E）、1 停止位")
    if link.get("eol") is not None and link.get("eol") not in EOLS:
        problems.append(f"{where} 的 eol 只能是 \\r\\n 或「空格 \\r\\n」")
    limits = {}
    for name, default in (("max_temp_c", 310.0), ("max_rpm", 1500.0), ("min_rpm", 50.0)):
        value = _number(item.get(name), default)
        if value is None or value < 0:
            problems.append(f"{where} 的 {name} 要写非负的数")
            value = default
        limits[name] = value
    if limits["max_temp_c"] < ambient:
        problems.append(f"{where} 的 max_temp_c {limits['max_temp_c']:g} 低于 ambient_c {ambient:g}：这块板什么温度都做不了")
    if limits["min_rpm"] > limits["max_rpm"]:
        problems.append(f"{where} 的 min_rpm 大于 max_rpm")
    sensor = str(item.get("sensor") or "plate")
    if sensor not in SENSORS:
        problems.append(f"{where} 的 sensor 只能是 external（外置 PT1000 探头）或 plate（加热盘）")
        sensor = "plate"
    if problems:
        return None, problems
    return Position(key=key, link=dict(link), name=str(item.get("name") or ""), sensor=sensor, **limits), []
