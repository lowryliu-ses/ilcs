"""网关配置（`--config` 指的那份 JSON，样例见 config.example.json）：一台冷水机，加可选的几块搅拌板（制冷搅拌）。

    {
      "device_id": "EL-D-STIR-01", "model": "Unichiller 012", "vendor": "Huber",
      "chiller": {"kind": "huber", "link": {"kind": "serial", "port": "COM3"}, "min_c": -20, "max_c": 25,
                  "tolerance_c": 0.5, "settle_sec": 60, "reach_timeout_sec": 3600, "after": "keep", "standby_c": 20},
      "capabilities": {"thermostat": "cap.thermostat", "stir": "cap.ely.stir"},
      "stirrers": {"1": {"name": "左前", "link": {"kind": "serial", "port": "COM5"}, "max_rpm": 1500}},
      "programs": {"CHILL": {"name": "控温", "action": "thermostat"}, "STIR-CHILL": {"name": "制冷搅拌", "action": "stir"}},
      "default_program": "CHILL"
    }

- `chiller.kind`：`huber`（PB 命令）、`julabo`、`lauda`。链路 `serial` 的缺省串口参数按厂家填：Huber 9600 8N1、
  LAUDA 9600 8N1（都不带握手），Julabo 4800 7E1 + 硬件握手（RTS/CTS）；和冷水机菜单里设的不一样就在 link 里写明。
  `tcp`：串口服务器（透明转发），或冷水机自己的网口——Huber Pilot ONE 缺省端口 8101、LAUDA 网口模块缺省 54321，
  不写 port 就用这两个；Julabo 的网口端口要照设备菜单写。
- `min_c` / `max_c`：这一站接受的温度范围（ILCS 工位极限照它登记），不能超出冷水机和导热液的范围。
- `tolerance_c`（缺省 0.5）、`settle_sec`（缺省 60）：浴温连续 `settle_sec` 秒在设定值 ±`tolerance_c` 以内才算到温；
  `reach_timeout_sec`（缺省 3600）秒内没到温判失败。
- `after`：作业结束（包括失败、被终止）之后冷水机怎样。`keep`（缺省）照最后的设定值接着控温；`standby` 把设定值改成
  `standby_c`、接着控温（不关机：冷块突然回温会结露）。
- `uppercase`（只对 Julabo）：命令发大写。手册里 CF 系列写大写（IN_PV_00），FL 等写小写，缺省发小写。
- `stirrers`：制冷搅拌的搅拌板，一个位置一块 IKA 板、一瓶，放在冷水机冷着的冷块上，只用电机（NAMUR，缺省 9600 7E1）。
  键是位置号（ILCS 指令里 `position` 写序号 1 起，或直接写键）；`max_rpm`（缺省 1500）、`min_rpm`（缺省 50，0 rpm 表示不搅）。
- `capabilities`：动作 → ILCS 能力。`thermostat` 缺省 `cap.thermostat`；配了 `stirrers` 才有 `stir`（缺省 `cap.ely.stir`）。
- `programs`：ILCS 设备方法的「程序」登记在这里，每个程序写它是哪个动作（`action`，缺省 `thermostat`）；设备没有内置程序，
  温度、时间、转速都由指令参数给。没带方法的指令用 `default_program`（动作对得上时），否则用这个动作登记的第一个程序。
- `auto_position`：指令没带位置时自动挑空闲的位置。只在每个位置都是验收用的空位 / 假瓶时打开。
- `poll_sec`（缺省 2）：后台线程多久读一次冷水机、搅拌板；`lost_after_sec`（缺省 30）：运行中这么多秒（且至少连续 3 次）
  读不到冷水机 / 某块板，算失去监视。
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
from typing import Any

ACTIONS = ("thermostat", "stir")
DEFAULT_CAPABILITIES = {"thermostat": "cap.thermostat", "stir": "cap.ely.stir"}
AFTER = ("keep", "standby")
# 各厂家的链路缺省：串口参数、行尾、命令间隔、网口缺省端口
BRANDS: dict[str, dict[str, Any]] = {
    "huber": {"vendor": "Huber", "serial": {"baudrate": 9600, "bytesize": 8, "parity": "N", "stopbits": 1,
                                            "rtscts": False}, "eol": "\r\n", "gap_sec": 0.0, "tcp_port": 8101},
    "julabo": {"vendor": "Julabo", "serial": {"baudrate": 4800, "bytesize": 7, "parity": "E", "stopbits": 1,
                                              "rtscts": True}, "eol": "\r", "gap_sec": 0.25, "tcp_port": None},
    "lauda": {"vendor": "LAUDA", "serial": {"baudrate": 9600, "bytesize": 8, "parity": "N", "stopbits": 1,
                                            "rtscts": False}, "eol": "\r\n", "gap_sec": 0.0, "tcp_port": 54321},
}
NAMUR = {"serial": {"baudrate": 9600, "bytesize": 7, "parity": "E", "stopbits": 1, "rtscts": False},
         "eol": "\r\n", "gap_sec": 0.05, "tcp_port": None}
EOLS = ("\r", "\r\n", " \r\n")
TOP_KEYS = {"device_id", "model", "vendor", "chiller", "capabilities", "stirrers", "auto_position", "programs",
            "default_program", "poll_sec", "lost_after_sec", "simulation"}
CHILLER_KEYS = {"kind", "link", "name", "min_c", "max_c", "tolerance_c", "settle_sec", "reach_timeout_sec", "after",
                "standby_c", "uppercase"}


@dataclass(frozen=True)
class ChillerSpec:
    kind: str
    link: dict[str, Any]
    min_c: float
    max_c: float
    tolerance_c: float = 0.5
    settle_sec: float = 60.0
    reach_timeout_sec: float = 3600.0
    after: str = "keep"
    standby_c: float | None = None
    name: str = ""
    uppercase: bool = False

    def label(self) -> str:
        return self.name or {"huber": "Huber 冷水机", "julabo": "Julabo 冷水机", "lauda": "LAUDA 冷水机"}[self.kind]


@dataclass(frozen=True)
class Position:
    key: str
    link: dict[str, Any]
    name: str = ""
    max_rpm: float = 1500.0
    min_rpm: float = 50.0

    def label(self) -> str:
        return f"位置 {self.key}" + (f"（{self.name}）" if self.name and self.name != self.key else "")


@dataclass(frozen=True)
class Program:
    code: str
    name: str
    action: str


@dataclass(frozen=True)
class Config:
    device_id: str
    chiller: ChillerSpec
    capabilities: dict[str, str]
    programs: dict[str, Program]
    positions: dict[str, Position]
    model: str = ""
    vendor: str = ""
    auto_position: bool = False
    default_program: str = ""
    poll_sec: float = 2.0
    lost_after_sec: float = 30.0

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(self.positions)

    def action_of(self, capability: str) -> str | None:
        return next((action for action, cap in self.capabilities.items() if cap == capability), None)

    def program_for(self, action: str, code: str) -> Program | None:
        """指令的程序；没带程序时用 default_program（动作对得上），否则用这个动作登记的第一个程序。"""
        if not code:
            default = self.programs.get(self.default_program)
            if default is not None and default.action == action:
                return default
            return next((program for program in self.programs.values() if program.action == action), None)
        program = self.programs.get(code)
        return program if program is not None and program.action == action else None

    @classmethod
    def parse(cls, data: dict[str, Any]) -> "Config":
        if not isinstance(data, dict):
            raise ValueError("网关配置要写成 JSON 对象")
        problems: list[str] = []
        unknown = sorted(key for key in data if key not in TOP_KEYS and not str(key).startswith("_"))
        if unknown:
            problems.append(f"不认识的配置项 {', '.join(unknown)}")
        device_id = str(data.get("device_id") or "").strip()
        if not device_id:
            problems.append("缺 device_id（这个工位在 ILCS 登记的设备编号）")
        chiller, issues = _chiller(data.get("chiller"))
        problems.extend(issues)
        positions: dict[str, Position] = {}
        raw = data.get("stirrers") or {}
        if not isinstance(raw, dict):
            problems.append("stirrers 要写成 {位置号: {link, name, max_rpm}}")
            raw = {}
        for key, item in raw.items():
            position, issues = _position(str(key).strip(), item)
            problems.extend(issues)
            if position is not None:
                positions[position.key] = position
        capabilities, issues = _capabilities(data.get("capabilities"), bool(raw))
        problems.extend(issues)
        programs: dict[str, Program] = {}
        for code, item in (data.get("programs") or {}).items():
            if not isinstance(item, dict):
                problems.append(f"程序 {code} 要写成对象，如 {{\"name\": \"制冷搅拌\", \"action\": \"stir\"}}")
                continue
            action = str(item.get("action") or "thermostat")
            if action not in ACTIONS:
                problems.append(f"程序 {code} 的 action 只能是 thermostat 或 stir")
            elif action not in capabilities:
                problems.append(f"程序 {code} 是 {action}，但这台站没有这个动作（stir 要配 stirrers）")
            else:
                programs[str(code)] = Program(code=str(code), name=str(item.get("name") or code), action=action)
        if not programs:
            problems.append("programs 是空的：至少登记一个程序（ILCS 设备方法的「程序」）")
        default = str(data.get("default_program") or "")
        if default and default not in programs:
            problems.append(f"default_program {default} 不在 programs 里")
        poll = _number(data.get("poll_sec"), 2.0)
        if poll is None or poll <= 0:
            problems.append("poll_sec 要写正数（秒）")
        lost = _number(data.get("lost_after_sec"), 30.0)
        if lost is None or lost < 0:
            problems.append("lost_after_sec 要写非负的数（秒）")
        if problems:
            raise ValueError("网关配置有问题：" + "；".join(problems))
        return cls(
            device_id=device_id, chiller=chiller, capabilities=capabilities, programs=programs, positions=positions,
            model=str(data.get("model") or ""), vendor=str(data.get("vendor") or BRANDS[chiller.kind]["vendor"]),
            auto_position=bool(data.get("auto_position")), default_program=default, poll_sec=float(poll),
            lost_after_sec=float(lost),
        )

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        return cls.parse(json.loads(Path(path).read_text(encoding="utf-8")))


def _number(value: Any, default: float | None) -> float | None:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def normalize_link(where: str, raw: Any, brand: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """链路配置补上厂家缺省（串口参数、行尾、命令间隔、网口端口）。"""
    if not isinstance(raw, dict) or raw.get("kind") not in {"tcp", "serial"}:
        return {}, [f"{where} 的 link 要写 {{\"kind\": \"serial\", \"port\": \"COM3\"}} 或 "
                    "{\"kind\": \"tcp\", \"host\": …, \"port\": …}"]
    problems: list[str] = []
    link = {"eol": brand["eol"], "gap_sec": brand["gap_sec"], **raw}
    if link["kind"] == "tcp":
        link.setdefault("port", brand["tcp_port"])
        if not link.get("host") or not link.get("port"):
            problems.append(f"{where} 的 TCP 链路要写 host 与 port")
    else:
        if not link.get("port"):
            problems.append(f"{where} 的串口链路要写 port（COM3、/dev/ttyUSB0、rfc2217://主机:端口）")
        link = {**brand["serial"], **link}
        if link["bytesize"] not in (7, 8) or str(link["parity"]).upper() not in {"N", "E", "O"} \
                or link["stopbits"] not in (1, 2) or not isinstance(link["baudrate"], int) or link["baudrate"] <= 0:
            problems.append(f"{where} 的串口参数不对（baudrate 正整数、bytesize 7 / 8、parity N / E / O、stopbits 1 / 2）")
    if link.get("eol") not in EOLS:
        problems.append(f"{where} 的 eol 只能是 \\r、\\r\\n 或「空格 \\r\\n」")
    for name in ("gap_sec", "timeout_sec"):
        if link.get(name) is not None and (_number(link[name], None) is None or link[name] < 0):
            problems.append(f"{where} 的 {name} 要写非负的数（秒）")
    return link, problems


def _chiller(raw: Any) -> tuple[ChillerSpec | None, list[str]]:
    if not isinstance(raw, dict):
        return None, ["缺 chiller（冷水机：kind、link、min_c、max_c）"]
    problems: list[str] = []
    unknown = sorted(key for key in raw if key not in CHILLER_KEYS and not str(key).startswith("_"))
    if unknown:
        problems.append(f"chiller 里不认识的配置项 {', '.join(unknown)}")
    kind = str(raw.get("kind") or "")
    if kind not in BRANDS:
        return None, problems + ["chiller.kind 只能是 huber、julabo 或 lauda"]
    link, issues = normalize_link("chiller", raw.get("link"), BRANDS[kind])
    problems.extend(issues)
    values: dict[str, float] = {}
    for name, default in (("min_c", None), ("max_c", None), ("tolerance_c", 0.5), ("settle_sec", 60.0),
                          ("reach_timeout_sec", 3600.0)):
        value = _number(raw.get(name), default)
        if value is None:
            problems.append(f"chiller.{name} 要写数" + ("（这一站接受的温度范围，℃）" if name in {"min_c", "max_c"} else ""))
            continue
        values[name] = value
    if {"min_c", "max_c"} <= set(values) and values["min_c"] >= values["max_c"]:
        problems.append(f"chiller.min_c {values['min_c']:g} 要低于 max_c {values['max_c']:g}")
    if values.get("tolerance_c", 1) <= 0:
        problems.append("chiller.tolerance_c 要写正数（℃）")
    if values.get("settle_sec", 0) < 0:
        problems.append("chiller.settle_sec 要写非负的数（秒）")
    if values.get("reach_timeout_sec", 1) <= 0:
        problems.append("chiller.reach_timeout_sec 要写正数（秒）")
    after = str(raw.get("after") or "keep")
    standby = _number(raw.get("standby_c"), None) if raw.get("standby_c") is not None else None
    if after not in AFTER:
        problems.append("chiller.after 只能是 keep（照最后的设定值接着控温）或 standby（回到 standby_c）")
    elif raw.get("standby_c") is not None and standby is None:
        problems.append("chiller.standby_c 要写数（℃）")
    elif after == "standby":
        if standby is None:
            problems.append("chiller.after 是 standby 时要写 standby_c（待机温度，℃）")
        elif {"min_c", "max_c"} <= set(values) and not values["min_c"] <= standby <= values["max_c"]:
            problems.append(f"chiller.standby_c {standby:g} 不在 min_c–max_c 里")
    if problems:
        return None, problems
    return ChillerSpec(kind=kind, link=link, after=after, standby_c=standby, name=str(raw.get("name") or ""),
                       uppercase=bool(raw.get("uppercase")), **values), []


def _position(key: str, item: Any) -> tuple[Position | None, list[str]]:
    where = f"位置 {key}"
    if not key:
        return None, ["stirrers 里有空的位置号"]
    if not isinstance(item, dict):
        return None, [f"{where} 要写成对象（link、name、max_rpm、min_rpm）"]
    link, problems = normalize_link(where, item.get("link"), NAMUR)
    limits = {}
    for name, default in (("max_rpm", 1500.0), ("min_rpm", 50.0)):
        value = _number(item.get(name), default)
        if value is None or value < 0:
            problems.append(f"{where} 的 {name} 要写非负的数")
            value = default
        limits[name] = value
    if limits["max_rpm"] <= 0:
        problems.append(f"{where} 的 max_rpm 要写正数")
    if limits["min_rpm"] > limits["max_rpm"]:
        problems.append(f"{where} 的 min_rpm 大于 max_rpm")
    if problems:
        return None, problems
    return Position(key=key, link=link, name=str(item.get("name") or ""), **limits), []


def _capabilities(raw: Any, stirrers: bool) -> tuple[dict[str, str], list[str]]:
    if raw is None:
        return {action: DEFAULT_CAPABILITIES[action] for action in ACTIONS if action == "thermostat" or stirrers}, []
    if not isinstance(raw, dict):
        return {}, ["capabilities 要写成 {\"thermostat\": \"cap.thermostat\", \"stir\": \"cap.ely.stir\"}"]
    problems: list[str] = []
    capabilities: dict[str, str] = {}
    for action, capability in raw.items():
        if action not in ACTIONS:
            problems.append(f"capabilities 里的 {action} 不是动作：只能是 thermostat、stir")
        elif not isinstance(capability, str) or not capability.strip():
            problems.append(f"capabilities.{action} 要写 ILCS 能力 id")
        else:
            capabilities[action] = capability.strip()
    if "thermostat" not in capabilities and not problems:
        problems.append("capabilities 里要有 thermostat（控温）")
    if "stir" in capabilities and not stirrers:
        problems.append("capabilities 里有 stir，但没配 stirrers（搅拌板）")
    if stirrers and "stir" not in capabilities and not problems:
        problems.append("配了 stirrers，capabilities 里却没有 stir")
    if len(set(capabilities.values())) != len(capabilities):
        problems.append("thermostat 与 stir 不能是同一个能力")
    return capabilities, problems
