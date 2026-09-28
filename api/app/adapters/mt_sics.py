"""梅特勒-托利多 MT-SICS 天平驱动（`mt_sics_v1`）。

MT-SICS 是梅特勒天平公开的标准命令集（RS232 或以太网 TCP，行结束符 CR LF）。驱动只用 Level 0/1 的
通用命令，不依赖某个型号的扩展命令：

| 命令 | 用途 | 回复 |
|---|---|---|
| `I2` / `I3` / `I4` / `I10` | 型号与量程 / 软件版本 / 序列号 / 天平编号 | `I4 A "B123456789"` |
| `S` | 稳定重量 | `S S     12.34567 g`；`S I` 不能执行（不稳定或忙）；`S +` / `S -` 超载 / 欠载 |
| `SI` | 立即读数（不等稳定） | `S S …` 或 `S D …`（动态值） |
| `T` / `Z` | 去皮 / 置零 | `T S 100.00000 g` / `Z A`；`I`、`+`、`-` 同上 |
| `@` | 复位（取消未完成的命令） | `I4 A "<序列号>"` |

任何命令都可能回 `ES`（语法错误）/ `ET`（传输错误）/ `EL`（逻辑错误）。

称重是即时动作：读数到手作业就完成，回执 `delivered` 带重量、单位与是否稳定，遥测带同名设定值。
`S I` / 超载 / 欠载是明确失败（天平没给出读数，重复称重没有副作用）；发出命令后没回复是结果未知。
去重与按指令号查询由作业台账负责：同一指令号重投回放原读数，不再称第二次。

不同型号的扩展功能（例如自动加料头）不在 MT-SICS 通用命令里：需要时按厂家文档经
「厂家 SDK 接口服务」（HTTPS JSON 网关）接入，或确认命令后在配置里扩展。
"""
from __future__ import annotations

import re

from .base import AdapterError, AdapterIndeterminate, AdapterUnreachable
from .jobs import MappedJobAdapter
from .line_command import LineTransport

DRIVER = "mt_sics_v1"
ACTIONS = {"weigh", "tare", "zero"}
UNITS = {"g": 1.0, "mg": 1e-3, "kg": 1e3, "ug": 1e-6, "µg": 1e-6}
REPLY = re.compile(r'^(?P<command>\S+)(?:\s+(?P<status>\S))?(?:\s+(?P<rest>.*))?$')
ERRORS = {"ES": "语法错误（ES）", "ET": "传输错误（ET）", "EL": "逻辑错误（EL）"}


def parse(reply: str) -> tuple[str, str, str]:
    """(命令, 状态, 其余)。ES / ET / EL 单独成行，没有状态位。"""
    match = REPLY.match(reply.strip())
    if match is None:
        raise AdapterIndeterminate(f"天平回复 {reply!r} 不是 MT-SICS 格式")
    return match.group("command"), match.group("status") or "", (match.group("rest") or "").strip()


def quoted(rest: str) -> str:
    match = re.search(r'"([^"]*)"', rest)
    return match.group(1).strip() if match else rest.strip()


def weight(rest: str) -> tuple[float, str]:
    parts = rest.split()
    if len(parts) < 2:
        raise AdapterIndeterminate(f"天平读数 {rest!r} 缺少数值或单位")
    try:
        return float(parts[0]), parts[1]
    except ValueError as exc:
        raise AdapterIndeterminate(f"天平读数 {parts[0]!r} 不是数值") from exc


class MtSicsAdapter(MappedJobAdapter):
    DRIVER = DRIVER
    PROTOCOL = "MT-SICS"
    NOTE = "梅特勒 MT-SICS 天平"
    SYNCHRONOUS = True

    def __init__(self, record, journal_key: str = ""):
        super().__init__(record, journal_key)
        self.transport = LineTransport(self.config, encoding="ascii", write_terminator="\r\n", read_terminator="\r\n")
        self.device_id_source = str(self.config.get("device_id_source") or "serial")
        if self.device_id_source not in {"serial", "balance_id"}:
            raise AdapterError("device_id_source 只能是 serial（I4 序列号）或 balance_id（I10 天平编号）")
        for capability, spec in (self.config.get("capabilities") or {}).items():
            if not isinstance(spec, dict) or spec.get("action", "weigh") not in ACTIONS:
                raise AdapterError(f"capabilities.{capability}.action 只能是 weigh / tare / zero")
            if str(spec.get("unit") or "g") not in UNITS:
                raise AdapterError(f"capabilities.{capability}.unit 只能是 {' / '.join(UNITS)}")
            self._seconds("stable_timeout_sec", 15.0, maximum=600, source=spec)

    def close(self) -> None:
        self.transport.close()

    # ---------- 收发 ----------

    def _command(self, session, line: str, expect: str, *, timeout: float | None = None,
                 before_action: bool = False) -> tuple[str, str]:
        try:
            reply = session.exchange(line, timeout=timeout)
        except AdapterUnreachable as exc:
            if before_action:
                raise AdapterError(f"{line} 没有回复（{exc}）；称重命令还没发出") from exc
            raise
        command, status, rest = parse(reply)
        if command in ERRORS:
            raise AdapterError(f"天平对 {line} 回复{ERRORS[command]}")
        if command != expect:
            raise AdapterIndeterminate(f"天平对 {line} 的回复 {reply!r} 不是 {expect} 应答")
        return status, rest

    # ---------- 钩子 ----------

    def accepted_params(self, spec: dict) -> set[str]:
        # 称重的参数是期望值（写进遥测的设定值），不下发给天平
        return {str(spec.get("result") or "mass")} | set(spec.get("accept") or [])

    def read_identity(self) -> dict:
        identity = {"vendor": self.config.get("vendor") or "Mettler Toledo", "accepts_commands": True}
        with self.transport.session() as session:
            status, rest = self._command(session, "I2", "I2")
            if status == "A":
                text = quoted(rest)
                identity["model"] = text.split()[0] if text else ""
                identity["capacity"] = text
            status, rest = self._command(session, "I3", "I3")
            if status == "A":
                identity["firmware"] = quoted(rest)
            status, rest = self._command(session, "I4", "I4")
            if status != "A":
                raise AdapterIndeterminate("天平没有返回序列号（I4）")
            identity["serial"] = quoted(rest)
            if self.device_id_source == "balance_id":
                status, rest = self._command(session, "I10", "I10")
                if status != "A":
                    raise AdapterIndeterminate("天平没有返回天平编号（I10）")
                identity["device_id"] = quoted(rest)
        identity.setdefault("device_id", identity["serial"])
        return identity

    def start_job(self, job: dict, spec: dict, values: dict) -> dict:
        action = spec.get("action", "weigh")
        unit = str(spec.get("unit") or "g")
        result = str(spec.get("result") or "mass")
        timeout = self._seconds("stable_timeout_sec", 15.0, maximum=600, source=spec) + self.transport.request_timeout
        with self.transport.session() as session:
            if spec.get("zero_first") and action == "weigh":
                self._expect_done(*self._command(session, "Z", "Z", timeout=timeout, before_action=True), "置零")
            if spec.get("tare_first") and action == "weigh":
                self._expect_done(*self._command(session, "T", "T", timeout=timeout, before_action=True), "去皮")
            if action == "zero":
                self._expect_done(*self._command(session, "Z", "Z", timeout=timeout), "置零")
                return {"delivered": {"zeroed": True}, "actuals": {}}
            line = "T" if action == "tare" else ("SI" if spec.get("immediate") else "S")
            status, rest = self._command(session, line, "T" if action == "tare" else "S", timeout=timeout)
        if status in {"I", "+", "-"}:
            self._expect_done(status, rest, "去皮" if action == "tare" else "称重")
        if status not in {"S", "D"}:
            raise AdapterIndeterminate(f"天平称重状态位 {status!r} 不在 MT-SICS 约定内")
        value, reported_unit = weight(rest)
        if reported_unit not in UNITS:
            raise AdapterIndeterminate(f"天平读数单位 {reported_unit!r} 无法换算")
        converted = value * UNITS[reported_unit] / UNITS[unit]
        name = "tare" if action == "tare" else result
        return {
            "delivered": {"unit": unit, "stable": status == "S", "raw": f"{value} {reported_unit}"},
            "actuals": {name: converted},
        }

    @staticmethod
    def _expect_done(status: str, rest: str, action: str) -> None:
        if status in {"A", "S"}:
            return
        reasons = {"I": "天平不能执行（称量不稳定或正忙）", "+": "超载或超出上限", "-": "欠载或低于下限", "L": "参数错误"}
        raise AdapterError(f"{action}失败：{reasons.get(status, f'状态位 {status}')}；天平没有给出有效读数")

    def read_status(self, job: dict) -> tuple[str, str]:
        return "idle", ""

    def hold_job(self, job: dict) -> None:
        raise AdapterError("天平没有「保持」：称重是即时动作")

    def abort_job(self, job: dict | None) -> None:
        with self.transport.session() as session:
            status, _ = self._command(session, "@", "I4")
        if status != "A":
            raise AdapterIndeterminate("天平复位（@）没有正常应答")
