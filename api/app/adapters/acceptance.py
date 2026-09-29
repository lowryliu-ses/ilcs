"""设备接入验收：给定一个工位的驱动，把接入要守的规矩逐项跑一遍，输出报告。

驱动写完不等于接好了：设备身份对不对、重复指令会不会动两次、回执丢了系统会不会盲目重发、执行器重启后能不能
按指令号查回、保持与终止有没有效——这些都要在真机上线前有同一套验收口径。这里把它们固定成一份检查清单：

- 只读（缺省）：身份与方法目录、健康检查、契约声明、查询一个不存在的指令号——不会让设备动作；
- 动作（physical）：正常完成、同一指令号重复提交、重建驱动后按指令号查回、保持、终止——会让设备真的动作，
  真实设备必须经现场负责人批准后才跑（DEC-02）；
- 故障（需要故障注入器）：丢回执、设备忙、联锁、失联。模拟设备可以直接注入；真实设备要在网络路径上注入
  （中间代理丢应答），没有注入器时这些项目标为「跳过」并写明原因，不假装测过。

每项结论三态：通过 / 不通过 / 跳过。「不重复执行」只有能读到设备侧动作次数时才判通过，否则只能判「跳过」。
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

from .base import AdapterError, AdapterUnreachable, CommandRequest, CommandResult, DeviceAdapter

PASS, FAIL, SKIP = "pass", "fail", "skip"
STATE_LABELS = {PASS: "通过", FAIL: "不通过", SKIP: "跳过"}
TERMINAL = {"done", "failed"}
# 报告里的配置摘要不带这些键的值：凭据只以引用形式出现在配置之外，这里再保险一次
SECRET_HINTS = ("password", "secret", "token", "key", "credential", "authorization")


class FaultInjector(Protocol):
    """模拟设备的故障注入与动作计数。真实设备没有这个，对应项目在报告里标为跳过。"""

    def set(self, mode: str, parameter: float = 0.0) -> None: ...

    def executions(self, command_id: str) -> int | None: ...


@dataclass
class Check:
    key: str
    label: str
    state: str
    detail: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class Report:
    station_id: str
    driver: str
    protocol: str
    contract: dict[str, Any]
    identity: dict[str, Any]
    config_digest: str
    physical: bool
    faults: bool
    started_at: str
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(check.state != FAIL for check in self.checks)

    def add(self, key: str, label: str, state: str, detail: str = "", **evidence: Any) -> Check:
        check = Check(key, label, state, detail, evidence)
        self.checks.append(check)
        return check

    def as_dict(self) -> dict[str, Any]:
        return {
            "station_id": self.station_id, "driver": self.driver, "protocol": self.protocol,
            "contract": self.contract, "identity": self.identity, "config_digest": self.config_digest,
            "physical": self.physical, "faults": self.faults, "started_at": self.started_at, "ok": self.ok,
            "checks": [check.__dict__ for check in self.checks],
        }

    def markdown(self) -> str:
        identity = self.identity or {}
        lines = [
            f"# 设备接入验收报告：{self.station_id}",
            "",
            f"- 时间：{self.started_at}",
            f"- 驱动：{self.driver}（协议 {self.protocol}，契约版本 {self.contract.get('version') or '—'}）",
            f"- 设备自报：厂商 {identity.get('vendor') or '—'}，型号 {identity.get('reported_model') or '—'}，"
            f"固件 {identity.get('firmware') or '—'}",
            f"- 配置摘要：{self.config_digest[:16]}（不含凭据）",
            f"- 范围：只读{' + 动作' if self.physical else ''}{' + 故障注入' if self.faults else ''}",
            f"- 结论：{'全部通过（跳过项见下表）' if self.ok else '有不通过的项目'}",
            "",
            "| 项目 | 结论 | 说明 |",
            "|---|---|---|",
        ]
        for check in self.checks:
            detail = check.detail.replace("|", "／").replace("\n", " ")
            lines.append(f"| {check.label} | {STATE_LABELS[check.state]} | {detail} |")
        return "\n".join(lines) + "\n"


def config_digest(config: dict[str, Any]) -> str:
    """配置的内容摘要：键名里像凭据的值抹掉再算，报告里只放摘要，不放配置原文。"""

    def scrub(value: Any, key: str = "") -> Any:
        if any(hint in key.lower() for hint in SECRET_HINTS):
            return "***"
        if isinstance(value, dict):
            return {k: scrub(v, str(k)) for k, v in sorted(value.items())}
        if isinstance(value, list):
            return [scrub(item) for item in value]
        return value

    text = json.dumps(scrub(config or {}), ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(text.encode()).hexdigest()


def run_acceptance(
    record: Any, factory: Callable[[], DeviceAdapter], template: CommandRequest, *,
    contract: dict[str, Any], describe: Callable[[DeviceAdapter], dict[str, Any]],
    physical: bool = False, injector: FaultInjector | None = None,
    poll_timeout: float = 30.0, poll_interval: float = 0.2, prefix: str = "ACC",
) -> Report:
    """`factory` 每次造一个新的驱动实例（「重建驱动后按指令号查回」要用）；`template` 是一条正常的动作指令，
    验收用它的能力与参数，指令号由这里生成（带 `prefix`，一眼能认出是验收留下的）。"""
    report = Report(
        station_id=str(getattr(record, "station_id", "")), driver=str(getattr(record, "driver", "") or ""),
        protocol=str(getattr(record, "protocol", "") or ""), contract=contract, identity={},
        config_digest=config_digest(getattr(record, "config", {}) or {}), physical=physical,
        faults=injector is not None, started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    instance = factory()
    run_id = uuid.uuid4().hex[:8]

    def command(tag: str, **changes: Any) -> CommandRequest:
        return replace(template, command_id=f"{prefix}-{run_id}-{tag}", **changes)

    # ---------- 只读 ----------
    try:
        report.identity = describe(instance)
        report.add(
            "identity", "设备身份与方法目录", PASS,
            f"型号 {report.identity.get('reported_model') or '未报'}，固件 {report.identity.get('firmware') or '未报'}，"
            f"方法目录 {len(report.identity.get('methods') or [])} 项（{report.identity.get('described_from') or '—'}）",
        )
    except (AdapterError, AdapterUnreachable) as exc:
        report.add("identity", "设备身份与方法目录", FAIL, f"读不到设备身份：{exc}")
        return report
    try:
        health = instance.healthcheck() or {}
        connected = health.get("connected", True) is not False
        report.add(
            "health", "健康检查", PASS if connected else FAIL,
            "在线" + ("，联锁触发" if health.get("site_interlock") else "")
            + ("，暂不接受指令" if health.get("accepts_commands") is False else ""),
            health=health,
        )
    except (AdapterError, AdapterUnreachable) as exc:
        report.add("health", "健康检查", FAIL, f"健康检查失败：{exc}")
    unsupported = [label for key, label in (
        ("supports_query", "状态查询"), ("supports_dedup", "设备端去重"),
        ("supports_hold", "保持"), ("supports_abort", "终止"),
    ) if not contract.get(key, True)]
    report.add(
        "contract", "契约声明", PASS,
        f"协议 {contract.get('protocol') or report.protocol}，"
        + (f"声明不支持：{'、'.join(unsupported)}" if unsupported else "查询、去重、保持、终止均声明支持"),
    )
    if contract.get("supports_query", True):
        probe_id = f"{prefix}-{run_id}-unknown"
        try:
            found = instance.query(probe_id)
            known = found is not None and found.state in TERMINAL | {"accepted", "running"}
            report.add(
                "query_unknown", "查询不存在的指令号", FAIL if known else PASS,
                f"返回了 {found.state}：设备把不存在的指令当成了已有任务" if known else "如实回答没有这条指令",
            )
        except (AdapterError, AdapterUnreachable) as exc:
            report.add("query_unknown", "查询不存在的指令号", FAIL, f"查询出错：{exc}")
    else:
        report.add("query_unknown", "查询不存在的指令号", SKIP, "契约声明不支持状态查询")

    if not physical:
        for key, label in (
            ("complete", "正常完成"), ("duplicate", "同一指令号重复提交"), ("restart_query", "重建驱动后按指令号查回"),
            ("hold", "保持"), ("abort", "终止"),
        ):
            report.add(key, label, SKIP, "只读验收：会让设备动作的项目要加 --physical，并经现场负责人批准")
    else:
        _physical_checks(report, instance, factory, command, contract, injector, poll_timeout, poll_interval)

    if injector is None:
        for key, label in (
            ("lost_receipt", "回执丢失"), ("busy", "设备忙"), ("interlock", "联锁"), ("offline", "失联"),
        ):
            report.add(key, label, SKIP, "没有故障注入器：模拟设备可直接注入；真实设备要在网络路径上注入")
    else:
        _fault_checks(report, factory, command, injector, poll_timeout, poll_interval)
    return report


def _wait(instance: DeviceAdapter, command_id: str, timeout: float, interval: float) -> tuple[CommandResult | None, list[str]]:
    """按指令号轮询到终态（或超时），记下经过的状态。"""
    seen: list[str] = []
    deadline = time.monotonic() + timeout
    result = None
    while time.monotonic() < deadline:
        result = instance.query(command_id)
        state = result.state if result is not None else "not_found"
        if not seen or seen[-1] != state:
            seen.append(state)
        if result is not None and result.state in TERMINAL:
            return result, seen
        time.sleep(interval)
    return result, seen


def _physical_checks(
    report: Report, instance: DeviceAdapter, factory: Callable[[], DeviceAdapter],
    command: Callable[..., CommandRequest], contract: dict[str, Any], injector: FaultInjector | None,
    timeout: float, interval: float,
) -> None:
    first = command("run")
    try:
        instance.submit(first)
        result, seen = _wait(instance, first.command_id, timeout, interval)
        done = result is not None and result.state == "done"
        report.add(
            "complete", "正常完成", PASS if done else FAIL,
            f"经过 {' → '.join(seen) or '—'}" + ("" if done else f"；{timeout:g} 秒内没有完成"),
            command_id=first.command_id,
        )
    except (AdapterError, AdapterUnreachable) as exc:
        report.add("complete", "正常完成", FAIL, f"提交失败：{exc}", command_id=first.command_id)
        return

    try:
        replay = instance.submit(first)
        count = injector.executions(first.command_id) if injector is not None else None
        if count is None:
            report.add(
                "duplicate", "同一指令号重复提交", SKIP,
                f"重投回 {replay.state}；读不到设备侧动作次数，是否只动作一次要现场核对",
            )
        else:
            report.add(
                "duplicate", "同一指令号重复提交", PASS if count == 1 else FAIL,
                f"重投回 {replay.state}，设备实际动作 {count} 次" + ("" if count == 1 else "：同一指令动作了不止一次"),
            )
    except (AdapterError, AdapterUnreachable) as exc:
        report.add("duplicate", "同一指令号重复提交", FAIL, f"重投出错（应回放原结论）：{exc}")

    if contract.get("supports_query", True):
        try:
            again = factory().query(first.command_id)
            same = again is not None and again.state == "done"
            report.add(
                "restart_query", "重建驱动后按指令号查回", PASS if same else FAIL,
                "新实例按指令号查回已完成" if same else f"新实例查回 {again.state if again else '没有这条指令'}",
            )
        except (AdapterError, AdapterUnreachable) as exc:
            report.add("restart_query", "重建驱动后按指令号查回", FAIL, f"新实例查询出错：{exc}")
    else:
        report.add("restart_query", "重建驱动后按指令号查回", SKIP, "契约声明不支持状态查询")

    for key, label, kind, supported in (
        ("hold", "保持", "hold", contract.get("supports_hold", True)),
        ("abort", "终止", "abort", contract.get("supports_abort", True)),
    ):
        if not supported:
            report.add(key, label, SKIP, f"契约声明不支持{label}")
            continue
        target = command(f"{kind}-target")
        try:
            instance.submit(target)
            control = command(kind, type=kind, target_command_id=target.command_id)
            receipt = instance.hold(control) if kind == "hold" else instance.abort(control)
            after = instance.query(target.command_id)
            state = after.state if after is not None else "not_found"
            if kind == "abort":
                ok = state == "failed"
                detail = f"终止回执 {receipt.state}；目标动作 {state}" + ("" if ok else "：目标没有停下")
            else:
                ok = receipt.state in {"done", "accepted"} and state != "done"
                detail = f"保持回执 {receipt.state}；目标动作 {state}"
            report.add(key, label, PASS if ok else FAIL, detail)
            if kind == "hold":
                instance.abort(command("hold-release", type="abort", target_command_id=target.command_id))
        except AdapterError as exc:
            report.add(key, label, SKIP, f"设备拒绝（多半是动作已结束、来不及{label}）：{exc}")
        except AdapterUnreachable as exc:
            report.add(key, label, FAIL, f"{label}指令结果未知：{exc}")


def _fault_checks(
    report: Report, factory: Callable[[], DeviceAdapter], command: Callable[..., CommandRequest],
    injector: FaultInjector, timeout: float, interval: float,
) -> None:
    instance = factory()
    try:
        injector.set("lost_receipt")
        lost = command("lost")
        try:
            instance.submit(lost)
            report.add("lost_receipt", "回执丢失", FAIL, "设备没回执，驱动却报告受理：应当判为结果未知")
        except AdapterUnreachable:
            injector.set("none")
            found, seen = _wait(factory(), lost.command_id, timeout, interval)
            count = injector.executions(lost.command_id)
            ok = found is not None and count in (None, 1)
            report.add(
                "lost_receipt", "回执丢失", PASS if ok else FAIL,
                "驱动判为结果未知、不重发；恢复后按指令号查回 "
                + (' → '.join(seen) or '—') + (f"，设备动作 {count} 次" if count is not None else ""),
            )
        except AdapterError as exc:
            report.add("lost_receipt", "回执丢失", FAIL, f"驱动把「回执丢失」判成明确失败：{exc}（设备其实可能已动作）")

        for mode, key, label in (("busy", "busy", "设备忙"), ("interlock", "interlock", "联锁")):
            injector.set(mode)
            probe = command(mode)
            try:
                instance.submit(probe)
                report.add(key, label, FAIL, f"设备处于{label}状态，驱动却报告受理")
            except AdapterError as exc:
                count = injector.executions(probe.command_id)
                report.add(
                    key, label, PASS if count in (None, 0) else FAIL,
                    f"明确失败、设备未动作：{exc}" if count in (None, 0) else f"报失败但设备动作了 {count} 次",
                )
            except AdapterUnreachable as exc:
                report.add(key, label, FAIL, f"应是明确失败，驱动却判为结果未知：{exc}")
            finally:
                injector.set("none")

        # 断开可能是异步的（模拟器先把控制应答送回再停听）：几秒内反复探测，直到判为失联
        injector.set("offline", 3)
        offline_error = None
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                factory().healthcheck()
            except (AdapterUnreachable, AdapterError) as exc:
                offline_error = exc
                break
            time.sleep(interval)
        if offline_error is None:
            report.add("offline", "失联", FAIL, "设备已断开，健康检查仍报在线")
        else:
            report.add("offline", "失联", PASS, f"健康检查判为失联：{offline_error}")
            # 等设备恢复在线，后面的清理才连得上
            back = time.monotonic() + max(timeout, 10)
            while time.monotonic() < back:
                try:
                    factory().healthcheck()
                    break
                except (AdapterUnreachable, AdapterError):
                    time.sleep(interval)
    finally:
        try:
            injector.set("none")
        except (AdapterError, AdapterUnreachable):
            pass  # 设备还没恢复：故障模式随模拟器重启复位，报告已经写完
