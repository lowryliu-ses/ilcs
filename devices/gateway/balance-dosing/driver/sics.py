"""梅特勒托利多 MT-SICS：天平（身份、称量、去皮、清零）与 Quantos 自动加粉（QRD / QRA 命令）。

MT-SICS 一问一答，命令大写、`\\r\\n` 结尾，应答 `<命令号> <状态> [参数]`：

- 状态：`A` 做完 / `B` 已收下、后面还有行 / `S` 稳定、`D` 动态（重量）/ `I` 现在执行不了（忙、等不到稳定）/
  `L` 参数或逻辑不对 / `+` 过载 / `-` 欠载；不带状态的 `ES` 语法错、`ET` 传输错、`EL` 执行不了。
- 重量：`S S     100.00 g`，按空白切开，最后一段是单位。
- `@` 复位（不清皮重），应答是 `I4 A "<序列号>"`；开机时天平也会主动发这一行，连上后先丢掉缓冲区里的。

Quantos（XPE 平台的 QX / Q2 自动加粉）在同一条链路上多一组 `QRD`（设置、查询）/ `QRA`（动作）命令：
目标质量用 mg，`QRA 61 1` 开始加粉先回 `B`、加完再回 `A`（出错回 `I <代码>`），结果与加样头信息是多行 XML。
梅特勒没有公开 Quantos 的命令手册，这里的命令取自公开的开源驱动（heingroup/mtbalance，以及另外两个独立实现，三者一致）：
上线前按梅特勒提供的正式文档核对。XPR 平台的自动加样（如 XPR226Q）走 Web Service，不是这一套命令。
"""
from __future__ import annotations

import re
import threading
import time
from typing import Any
from xml.etree import ElementTree

from .errors import DeviceBusy, DeviceError
from .link import Link, LinkError

UNITS = {"g": 1.0, "mg": 0.001, "kg": 1000.0, "ug": 1e-6, "µg": 1e-6}
GENERAL = {"ES": "天平不认识这条命令（语法错误）", "ET": "天平报传输错误", "EL": "天平现在不能执行这条命令"}
QUANTOS_ERRORS = {
    1: "加样头没装好", 2: "另一个作业在跑", 3: "超时", 4: "没有选中", 5: "现在不允许这个动作", 6: "重量不稳定",
    7: "出粉故障（粉末流动异常）", 8: "被外部操作停止", 9: "安全位置错误", 10: "这个加样头不允许使用",
    11: "加样头剂次已到上限", 12: "加样头过期", 13: "样品转盘卡住",
}
BUSY_CODES = {2, 5}
QUOTED = re.compile(r'^\S+ A "(.*)"$')
SIMULATOR_MARK = "ILCS-SIMULATOR"


class SicsError(DeviceError):
    """天平明确报错。"""


class SicsBusy(SicsError, DeviceBusy):
    """天平忙、或一直等不到稳定读数：命令没执行。"""


class QuantosError(SicsError):
    def __init__(self, code: int | None, during: str):
        reason = QUANTOS_ERRORS.get(code, "未知错误") if code is not None else "命令参数不对"
        super().__init__(f"Quantos {during}报错{f' {code}' if code is not None else ''}：{reason}")
        self.code = code


class QuantosBusy(QuantosError, DeviceBusy):
    pass


def _quantos_error(code: int | None, during: str) -> QuantosError:
    return QuantosBusy(code, during) if code in BUSY_CODES else QuantosError(code, during)


class Balance:
    def __init__(self, spec: dict[str, Any]):
        self.link = Link(spec, timeout=float(spec.get("timeout_sec") or 5), read_terminator=b"\n",
                         write_terminator=b"\r\n")
        self.cached: dict[str, Any] | None = None

    # ---------- 收发 ----------

    def ask(self, command: str, *, timeout: float | None = None) -> str:
        line = self.link.ask(command, timeout=timeout)
        if line in GENERAL:
            raise SicsError(f"{command}：{GENERAL[line]}")
        return line

    def _quoted(self, command: str) -> str:
        line = self.ask(command)
        match = QUOTED.match(line)
        if match:
            return match.group(1)
        if line.split()[-1:] == ["I"]:
            raise SicsBusy(f"天平忙，{command} 没答")
        raise SicsError(f"{command} 的应答读不懂：{line!r}")

    @staticmethod
    def _grams(tokens: list[str], line: str) -> float:
        if len(tokens) < 4:
            raise SicsError(f"天平回了读不懂的重量：{line!r}")
        unit = tokens[-1]
        try:
            value = float(tokens[-2])
        except ValueError as exc:
            raise SicsError(f"天平报错：{line!r}") from exc  # 如 "S S  Error 10b"
        if unit not in UNITS:
            raise SicsError(f"天平的单位 {unit} 不认识：把天平的称量单位设成 g")
        return value * UNITS[unit]

    # ---------- 身份 ----------

    def identity(self) -> dict[str, Any]:
        model_text = self._quoted("I2")  # "XPR6U 6.1 g"：型号可能带空格，从右边去掉量程与单位
        parts = model_text.rsplit(" ", 2)
        model = parts[0] if len(parts) == 3 else model_text
        serial = self._quoted("I4")
        firmware = self._quoted("I3")
        self.cached = {
            "model": model, "capacity": " ".join(parts[1:]) if len(parts) == 3 else "", "serial": serial,
            "firmware": firmware,
            "simulator": any(SIMULATOR_MARK in text for text in (model_text, serial, firmware)),
        }
        return dict(self.cached)

    # ---------- 称量 ----------

    def _status(self, line: str, during: str) -> tuple[str, list[str]]:
        tokens = line.split()
        status = tokens[1] if len(tokens) > 1 else ""
        if status == "+":
            raise SicsError(f"天平{during}时过载")
        if status == "-":
            raise SicsError(f"天平{during}时欠载（秤盘没放好？）")
        if status == "L":
            raise SicsError(f"天平{during}：参数或状态不对，不能执行")
        return status, tokens

    def ensure_ready(self) -> None:
        """动作之前：读一次即时重量。过载 / 欠载 / 忙都在这里明确报出，设备不动。"""
        line = self.ask("SI")
        status, _ = self._status(line, "自检")
        if status == "I":
            raise SicsBusy("天平忙，设备未接受作业")
        if status not in {"S", "D"}:
            raise SicsError(f"天平自检应答读不懂：{line!r}")

    def stable_weight(self, timeout: float = 30.0) -> float:
        """稳定净重（g）。天平等不到稳定会回 `S I`：在 timeout 内重试，过了就报忙。"""
        deadline = time.monotonic() + timeout
        while True:
            line = self.ask("S", timeout=max(self.link.timeout, 10.0))
            status, tokens = self._status(line, "称量")
            if status == "S":
                return self._grams(tokens, line)
            if status != "I":
                raise SicsError(f"天平称量应答读不懂：{line!r}")
            if time.monotonic() > deadline:
                raise SicsBusy(f"{timeout:.0f} 秒内天平读数一直不稳定")
            time.sleep(0.2)

    def weight_now(self) -> float:
        line = self.ask("SI")
        status, tokens = self._status(line, "读数")
        if status not in {"S", "D"}:
            raise SicsBusy("天平忙，读不到即时重量")
        return self._grams(tokens, line)

    def tare(self, timeout: float = 30.0) -> float:
        deadline = time.monotonic() + timeout
        while True:
            line = self.ask("T", timeout=max(self.link.timeout, 10.0))
            status, tokens = self._status(line, "去皮")
            if status == "S":
                return self._grams(tokens, line)
            if status != "I":
                raise SicsError(f"天平去皮应答读不懂：{line!r}")
            if time.monotonic() > deadline:
                raise SicsBusy(f"{timeout:.0f} 秒内去皮一直等不到稳定")
            time.sleep(0.2)

    def zero(self) -> None:
        line = self.ask("Z", timeout=max(self.link.timeout, 10.0))
        status, _ = self._status(line, "清零")
        if status == "I":
            raise SicsBusy("天平忙，没能清零")
        if status != "A":
            raise SicsError(f"天平清零应答读不懂：{line!r}")


class Quantos:
    """Quantos 自动加粉（MT-SICS 的 QRD / QRA 命令，与天平共用一条链路）。"""

    def __init__(self, balance: Balance, *, dose_timeout_sec: float = 900.0, action_timeout_sec: float = 60.0):
        self.balance = balance
        self.link = balance.link
        self.dose_timeout = dose_timeout_sec
        self.action_timeout = action_timeout_sec
        self.stop_sent = False
        self.start_sent = False

    # ---------- 应答 ----------

    @staticmethod
    def _parse(line: str, prefix: str) -> tuple[str, int | None, list[str]]:
        """「<前缀> [值…] <状态>」→ (状态, 错误码, 值)。前缀是命令本身（动作命令的应答不带最后那个参数）。"""
        if line in GENERAL:
            raise SicsError(f"{prefix}：{GENERAL[line]}")
        if not line.startswith(prefix):
            raise SicsError(f"{prefix} 的应答对不上：{line!r}")
        rest = line[len(prefix):].split()
        if len(rest) >= 2 and rest[-2] == "I" and rest[-1].isdigit():
            return "I", int(rest[-1]), rest[:-2]
        if rest and rest[-1] in {"A", "B", "I", "L"}:
            return rest[-1], None, rest[:-1]
        raise SicsError(f"{prefix} 的应答读不懂：{line!r}")

    def _setting(self, path: str, value: str, during: str) -> None:
        status, code, _ = self._parse(self.balance.ask(f"{path} {value}"), path)
        if status != "A":
            raise _quantos_error(code, during)

    def _query(self, path: str, during: str) -> str:
        status, code, values = self._parse(self.balance.ask(path), path)
        if status != "A" or not values:
            raise _quantos_error(code, during)
        return values[0]

    def _action(self, path: str, argument: str, during: str) -> None:
        """动作命令：先回 B（已收下），做完回 A；出错回 I <代码>。"""
        with self.link.lock:
            status, code, _ = self._parse(self.balance.ask(f"{path} {argument}"), path)
            deadline = time.monotonic() + self.action_timeout
            while status == "B":
                line = self.link.try_read_line(0.5)
                if line is None:
                    if time.monotonic() > deadline:
                        raise LinkError(f"Quantos {during}超过 {self.action_timeout:.0f} 秒没有做完", sent=True)
                    continue
                if line.startswith(path):
                    status, code, _ = self._parse(line, path)
        if status != "A":
            raise _quantos_error(code, during)

    def _xml(self, path: str, during: str) -> str | None:
        """多行 XML 的查询：`<path> B`、XML 行……、`<path> A`。加样头没装（错误 1）返回 None。"""
        with self.link.lock:
            status, code, _ = self._parse(self.balance.ask(path), path)
            if status == "I" and code == 1:
                return None
            if status != "B":
                raise _quantos_error(code, during)
            lines: list[str] = []
            deadline = time.monotonic() + self.action_timeout
            while True:
                line = self.link.try_read_line(0.5)
                if line is None:
                    if time.monotonic() > deadline:
                        raise LinkError(f"Quantos {during}的 XML 没收全", sent=True)
                    continue
                if line.startswith(path):
                    status, code, _ = self._parse(line, path)
                    if status != "A":
                        raise _quantos_error(code, during)
                    return "\n".join(lines)
                lines.append(line)

    @staticmethod
    def _fields(xml: str) -> dict[str, Any]:
        """XML 里的叶子节点 → {标签: 文本}；带 Unit 属性的再给一个 `<标签>@unit`。解析不了就逐个正则取。"""
        fields: dict[str, Any] = {}
        try:
            for element in ElementTree.fromstring(xml).iter():
                if len(element) == 0 and element.text is not None:
                    fields[element.tag] = element.text.strip()
                    if element.get("Unit"):
                        fields[f"{element.tag}@unit"] = element.get("Unit")
        except ElementTree.ParseError:
            for tag, attrs, text in re.findall(r"<([\w.]+)([^>]*)>([^<]*)</\1>", xml):
                fields[tag] = text.strip()
                unit = re.search(r'Unit="([^"]+)"', attrs)
                if unit:
                    fields[f"{tag}@unit"] = unit.group(1)
        return fields

    @staticmethod
    def _mass_g(fields: dict[str, Any], tag: str) -> float | None:
        raw = fields.get(tag)
        if raw in (None, ""):
            return None
        try:
            value = float(str(raw).replace(",", "."))
        except ValueError:
            return None
        return value * UNITS.get(str(fields.get(f"{tag}@unit") or "mg"), 0.001)

    # ---------- 加样头、门 ----------

    def head(self) -> dict[str, Any] | None:
        xml = self._xml("QRD 2 4 11", "读加样头")
        if xml is None:
            return None
        fields = self._fields(xml)
        limit, counter = fields.get("Dose_limit"), fields.get("Dosing_counter")
        remaining = None
        if str(limit or "").isdigit() and str(counter or "").isdigit():
            remaining = int(limit) - int(counter)
        return {"substance": fields.get("Substance", ""), "lot": fields.get("Lot_ID", ""),
                "remaining_doses": remaining, "remaining_g": self._mass_g(fields, "Rem._quantity"),
                "expiry": fields.get("Exp._date", "")}

    def door(self) -> int:
        return int(self._query("QRD 2 3 7", "读前门"))

    # ---------- 加粉 ----------

    def begin(self, target_g: float, tolerance_pct: float, sample_id: str) -> None:
        """加粉前的准备做完、发出开始命令、Quantos 回「已收下」就返回。这之前任何一步报错，粉都没加。"""
        self.start_sent = False
        if self.door() != 2:
            self._action("QRA 60 7", "2", "关前门")
        self._action("QRA 60 2", "4", "锁加样头")
        self._setting("QRD 1 1 5", f"{target_g * 1000:.3f}", "设目标质量")
        self._setting("QRD 1 1 6", f"{tolerance_pct:g}", "设容差")
        self._setting("QRD 1 1 7", "0", "设容差方式（±）")
        self._setting("QRD 1 1 8", sample_id[:20], "设样品号")
        self.stop_sent = False
        self.start_sent = True  # 从这里起开始加粉的命令发出去了：再断线就不知道开没开始
        status, code, _ = self._parse(self.balance.ask("QRA 61 1"), "QRA 61 1")
        if status != "B":
            raise _quantos_error(code, "开始加粉")

    def finish(self, cancel: threading.Event | None = None) -> float:
        """等加粉的结论（A 或 I <代码>），然后读这次实际加了多少（g）。收到终止信号发 `QRA 61 4`。"""
        deadline = time.monotonic() + self.dose_timeout
        while True:
            if cancel is not None and cancel.is_set() and not self.stop_sent:
                self.stop()
            with self.link.lock:
                line = self.link.try_read_line(0.2)
            if line is None:
                if time.monotonic() > deadline:
                    raise LinkError(f"Quantos 加粉超过 {self.dose_timeout:.0f} 秒没有结论", sent=True)
                time.sleep(0.02)  # 锁不公平：留个空当，让终止命令拿得到链路
                continue
            if not line.startswith("QRA 61 1"):
                continue  # 停止命令自己的应答（QRA 61 4 A）之类
            status, code, _ = self._parse(line, "QRA 61 1")
            if status == "A":
                break
            if status == "I":
                mass = self.dosed_g()
                done = f"（已加 {mass:.4f} g，未入账）" if mass else ""
                if code == 8 and self.stop_sent:
                    raise InterruptedError(f"加粉被终止{done}")
                raise QuantosError(code, f"加粉{done}")
        mass = self.dosed_g()
        if mass is None:
            raise SicsError("Quantos 加完了，但结果里没有实际加粉量")
        try:
            self._action("QRA 60 2", "3", "松开加样头")
        except (SicsError, LinkError):
            pass  # 松不开不影响这次的量；下一次换头时再处理
        return mass

    def dosed_g(self) -> float | None:
        try:
            xml = self._xml("QRD 2 4 12", "读加粉结果")
        except (SicsError, LinkError):
            return None
        return self._mass_g(self._fields(xml), "Content") if xml else None

    def stop(self) -> None:
        self.stop_sent = True
        try:
            self.link.write("QRA 61 4")
        except LinkError:
            pass
