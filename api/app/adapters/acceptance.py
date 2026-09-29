"""设备接入验收：给定一个工位的驱动，把接入要守的规矩逐项跑一遍，输出报告。

驱动写完不等于接好了：设备身份对不对、重复指令会不会动两次、回执丢了系统会不会盲目重发、执行器重启后能不能
按指令号查回、保持与终止有没有效——这些都要在真机上线前有同一套验收口径。这里把它们固定成一份检查清单：

- 只读（缺省）：身份与方法目录、健康检查、契约声明、查询一个不存在的指令号——不会让设备动作；
- 动作（physical）：正常完成、同一指令号重复提交、重建驱动后按指令号查回、保持、终止——会让设备真的动作，
  真实设备必须经现场负责人批准后才跑（DEC-02）；
- 故障（需要故障注入器）：丢回执、设备忙、联锁、失联。模拟设备可以直接注入；真实设备要在网络路径上注入
  （中间代理丢应答），没有注入器时这些项目标为「跳过」并写明原因，不假装测过；模拟设备自己说注入不了的
  （点表设备没有回执可丢）同样判跳过。验收建的驱动实例结束时都关掉，留下没结束的验收指令收尾时逐条终止。

每项结论三态：通过 / 不通过 / 跳过。「不重复执行」只有能读到设备侧动作次数时才判通过，否则只能判「跳过」：
设备认 ILCS 指令号时按指令号数；不认的（串口命令、PLC 点表、天平、车队）数设备的总动作次数，前后没变才算。

三个入口共用这份清单：执行器（界面发起、报告入库，见 services/acceptance_service.py）、
`scripts/device-acceptance.py`（按库里的工位，或不连库、只给一份适配器登记 JSON）、设备模块自己的测试。
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable, Protocol
from urllib.parse import quote

from .base import AdapterError, AdapterUnreachable, CommandRequest, CommandResult, DeviceAdapter

PASS, FAIL, SKIP = "pass", "fail", "skip"
STATE_LABELS = {PASS: "通过", FAIL: "不通过", SKIP: "跳过"}
TERMINAL = {"done", "failed"}
# 报告里的配置摘要不带这些键的值：凭据只以引用形式出现在配置之外，这里再保险一次
SECRET_HINTS = ("password", "secret", "token", "key", "credential", "authorization")
# 验收指令的编号前缀：一眼能认出是验收留下的，不是生产指令
PREFIX = "ACC"
PHYSICAL_KEYS = (
    ("complete", "正常完成"), ("duplicate", "同一指令号重复提交"), ("restart_query", "重建驱动后按指令号查回"),
    ("hold", "保持"), ("abort", "终止"),
)
FAULT_KEYS = (("lost_receipt", "回执丢失"), ("busy", "设备忙"), ("interlock", "联锁"), ("offline", "失联"))


class FaultInjector(Protocol):
    """模拟设备的故障注入与动作计数。真实设备没有这个，对应项目在报告里标为跳过。

    `executions` 按 ILCS 指令号数设备真正动作了几次，设备不认指令号时返回 None；
    `motions`（可选）是设备侧的总动作次数，不认指令号的设备靠它判断「重投有没有让设备再动一次」；
    `unsupported`（可选）是这台模拟设备注入不了的故障与原因（点表设备没有回执可丢），对应项目判跳过。
    """

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
    # 设备自报为模拟器（健康检查或身份里的 simulator 标记；内置模拟适配器也算）
    simulator: bool = False
    # 验收结束时还没有结论的验收指令（设备可能仍在动作）：调用方据此挡住这台设备，现场核查后重新验收
    leftovers: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(check.state != FAIL for check in self.checks)

    @property
    def physical_ran(self) -> bool:
        """动作项目真的跑了（正常完成通过）。只有它能证明动作级：动作项目全被跳过的报告只算只读级证据。"""
        return any(check.key == "complete" and check.state == PASS for check in self.checks)

    def add(self, key: str, label: str, state: str, detail: str = "", **evidence: Any) -> Check:
        check = Check(key, label, state, detail, evidence)
        self.checks.append(check)
        return check

    def as_dict(self) -> dict[str, Any]:
        return {
            "station_id": self.station_id, "driver": self.driver, "protocol": self.protocol,
            "contract": self.contract, "identity": self.identity, "config_digest": self.config_digest,
            "physical": self.physical, "faults": self.faults, "started_at": self.started_at, "ok": self.ok,
            "simulator": self.simulator, "physical_ran": self.physical_ran, "leftovers": list(self.leftovers),
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
            f"固件 {identity.get('firmware') or '—'}" + ("，模拟器" if self.simulator else ""),
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


# ---------- 验收对象与验收指令 ----------

@dataclass
class AcceptanceRecord:
    """驱动构造只读这些字段：库里的 `Adapter`、一份适配器登记 JSON 都能变成它（不连库也能验收）。"""

    station_id: str
    kind: str = "real"
    driver: str = ""
    protocol: str = ""
    version: str = ""
    config: dict[str, Any] = field(default_factory=dict)
    credential_ref: str = ""
    supports_hold: bool = True
    supports_abort: bool = True
    supports_query: bool = True
    supports_dedup: bool = True
    note: str = ""
    config_version: int = 0

    @classmethod
    def of(cls, source: Any) -> "AcceptanceRecord":
        """从 ORM 对象（或任何带同名属性的对象）复制一份：验收在执行器线程里跑，不拿着会话里的对象。"""
        values = {name: getattr(source, name) for name in cls.__dataclass_fields__ if hasattr(source, name)}
        values["config"] = json.loads(json.dumps(values.get("config") or {}))
        return cls(**values)

    @classmethod
    def from_registration(cls, data: dict[str, Any], station_id: str = "") -> "AcceptanceRecord":
        """docs/设备适配器配置模板.md「通用结构」那份适配器登记 JSON。"""
        if not isinstance(data, dict) or not isinstance(data.get("config", {}), dict):
            raise ValueError("适配器登记必须是 JSON 对象，config 也必须是对象")
        known = {name: data[name] for name in cls.__dataclass_fields__ if name in data}
        known.setdefault("station_id", station_id or str(data.get("station_id") or "") or "STANDALONE")
        return cls(**known)


def default_template(station_id: str, limits: dict[str, Any], capability: str = "",
                     params: dict[str, Any] | None = None) -> CommandRequest:
    """一条正常的动作指令：缺省用工位第一个能力、参数取工位极限的中点——落在承接范围内，
    不会因为参数越界被拒而测不到后面的项目。指令号由 `run_acceptance` 生成。"""
    capability = capability or next(iter(sorted(limits or {})), "")
    window = (limits or {}).get(capability)
    if params is None:
        params = {name: round((span[0] + span[1]) / 2, 6) for name, span in (window or {}).items() if span}
    return CommandRequest(
        command_id="", station_id=station_id, capability=capability, params=dict(params), type="dispatch",
        batch_id="ACCEPTANCE", step_index=0, step_id="acceptance",
    )


# ---------- 故障注入器 ----------

class SimulatorControlInjector:
    """模拟设备统一控制口（`simulators/common/control.py`）的客户端：故障注入与动作计数。

    `spec` 取自适配器配置里的 `simulator_control`：`{"url": "http://line-sim-oven:9900", "token_ref": "file://…",
    "unit": "AGV-01"}`（`unit` 给一个进程模拟多台设备的车队用）。主机同样要在设备白名单里；只对自报为模拟器的设备、
    非正式环境使用（由调用方判断）。HTTPS 网关模拟器的控制接口就在它自己的 API 上，路径与这里相同。
    """

    def __init__(self, spec: dict[str, Any], *, credential_ref: str = "", label: str = "模拟设备控制口"):
        from .http_client import HttpTransport

        config = {
            "base_url": str(spec.get("url") or spec.get("base_url") or ""),
            "request_timeout_sec": float(spec.get("request_timeout_sec") or 5),
            "connect_timeout_sec": float(spec.get("connect_timeout_sec") or 3),
            "allow_insecure_http": True, "verify_tls": spec.get("verify_tls", True),
            **({"ca_file": spec["ca_file"]} if spec.get("ca_file") else {}),
        }
        self.transport = HttpTransport(config, str(spec.get("token_ref") or credential_ref or ""),
                                       driver="simulator_control", label=label)
        self.unit = str(spec.get("unit") or "")

    def _state(self) -> dict[str, Any]:
        suffix = f"?unit={quote(self.unit)}" if self.unit else ""
        state = self.transport.request("GET", f"/simulator/state{suffix}")
        return state if isinstance(state, dict) else {}

    def set(self, mode: str, parameter: float = 0.0) -> None:
        self.transport.request("POST", "/simulator/fault", {"mode": mode, "parameter": parameter, "unit": self.unit})

    def executions(self, command_id: str) -> int | None:
        state = self._state()
        counts = state.get("executions")
        if state.get("knows_command_ids") is False or not isinstance(counts, dict):
            return None
        return int(counts.get(command_id, 0))

    def motions(self) -> int | None:
        state = self._state()
        if isinstance(state.get("motions"), (int, float)):
            return int(state["motions"])
        counts = state.get("executions")
        return sum(int(value) for value in counts.values()) if isinstance(counts, dict) else None

    def unsupported(self) -> dict[str, str]:
        raw = self._state().get("unsupported")
        return {str(mode): str(reason) for mode, reason in raw.items()} if isinstance(raw, dict) else {}


def injector_for(record: Any, capability: str) -> tuple[FaultInjector | None, str]:
    """按适配器配置找故障注入器。返回 (注入器, 没有注入器时的原因)。

    - 配置里登记了 `simulator_control`：用统一控制口；
    - 组合工位：用承接这项能力的那条路由的配置；
    - HTTPS 网关（`http_json_v1`）没登记控制口时，用网关自己的 `/simulator/*`（同一套 TLS 与凭据）。
    """
    config, driver, credential_ref = dict(getattr(record, "config", {}) or {}), getattr(record, "driver", ""), \
        getattr(record, "credential_ref", "") or ""
    if driver == "composite_v1":
        route = next((item for item in config.get("routes") or []
                      if isinstance(item, dict) and capability in (item.get("capabilities") or [])), None)
        if route is None:
            return None, f"组合工位没有承接 {capability or '（未指定能力）'} 的路由"
        config, driver = dict(route.get("config") or {}), str(route.get("driver") or "")
        credential_ref = str(route.get("credential_ref") or "")
    spec = config.get("simulator_control")
    if isinstance(spec, dict) and (spec.get("url") or spec.get("base_url")):
        return SimulatorControlInjector(spec), ""
    if driver == "http_json_v1" and config.get("base_url"):
        return SimulatorControlInjector({**config, "url": config["base_url"]}, credential_ref=credential_ref,
                                        label="模拟设备控制接口"), ""
    return None, "适配器配置里没有登记模拟设备控制口（simulator_control）"


# ---------- 检查清单 ----------

def run_acceptance(
    record: Any, factory: Callable[[], DeviceAdapter], template: CommandRequest, *,
    contract: dict[str, Any], describe: Callable[[DeviceAdapter], dict[str, Any]],
    physical: bool = False, injector: FaultInjector | None = None,
    poll_timeout: float = 30.0, poll_interval: float = 0.2, prefix: str = PREFIX,
    on_progress: Callable[[], None] | None = None, fault_note: str = "",
) -> Report:
    """`factory` 每次造一个新的驱动实例（「重建驱动后按指令号查回」要用）；`template` 是一条正常的动作指令，
    验收用它的能力与参数，指令号由这里生成（带 `prefix`，一眼能认出是验收留下的）。

    `on_progress` 在等待与各项之间被调用：执行器串行模式下用它续写存活记录，长时间的动作级验收不会让执行门关闭。
    `fault_note` 是没有注入器时故障项目标「跳过」的原因。
    """
    report = Report(
        station_id=str(getattr(record, "station_id", "")), driver=str(getattr(record, "driver", "") or ""),
        protocol=str(getattr(record, "protocol", "") or ""), contract=contract, identity={},
        config_digest=config_digest(getattr(record, "config", {}) or {}), physical=physical,
        faults=injector is not None, started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        simulator=getattr(record, "kind", "real") != "real",
    )

    def pause(seconds: float) -> None:
        if on_progress is not None:
            on_progress()
        time.sleep(seconds)

    # 验收过程中建的驱动实例最后都关掉：OPC UA 这类每个实例占一个会话，设备的会话数有上限
    created: list[Any] = []

    def build() -> DeviceAdapter:
        built = factory()
        created.append(built)
        return built

    try:
        return _checklist(report, build, template, contract=contract, describe=describe, physical=physical,
                          injector=injector, poll_timeout=poll_timeout, poll_interval=poll_interval, prefix=prefix,
                          pause=pause, fault_note=fault_note)
    finally:
        for built in created:
            close = getattr(built, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:  # noqa: BLE001  关不掉的连接不影响报告
                    pass


def _checklist(
    report: Report, factory: Callable[[], DeviceAdapter], template: CommandRequest, *, contract: dict[str, Any],
    describe: Callable[[DeviceAdapter], dict[str, Any]], physical: bool, injector: FaultInjector | None,
    poll_timeout: float, poll_interval: float, prefix: str, pause: Callable[[float], None], fault_note: str,
) -> Report:
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
    not_ready = ""  # 设备此刻不能动作的原因：动作级验收一项都不跑
    try:
        health = instance.healthcheck() or {}
        connected = health.get("connected", True) is not False
        interlocked = bool(health.get("site_interlock") or health.get("interlock"))
        refusing = health.get("accepts_commands") is False
        report.simulator = report.simulator or bool(health.get("simulator"))
        if not connected:
            not_ready = "设备不在线"
        elif interlocked or refusing:
            not_ready = "设备联锁触发" if interlocked else "设备暂不接受指令"
        report.add(
            "health", "健康检查", FAIL if not connected or (physical and not_ready) else PASS,
            "在线" + ("，联锁触发" if interlocked else "") + ("，暂不接受指令" if refusing else "")
            + ("：动作级验收要求设备在线、无联锁、接受指令" if physical and not_ready else ""),
            health=health,
        )
    except (AdapterError, AdapterUnreachable) as exc:
        not_ready = f"健康检查失败：{exc}"
        report.add("health", "健康检查", FAIL, not_ready)
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
        for key, label in PHYSICAL_KEYS:
            report.add(key, label, SKIP, "只读验收：会让设备动作的项目要申请动作级验收，并经现场负责人批准")
    elif not_ready:
        for key, label in PHYSICAL_KEYS:
            report.add(key, label, SKIP, f"{not_ready}：动作项目一项都没跑，设备没有动作")
    else:
        _physical_checks(report, instance, factory, command, contract, injector, poll_timeout, poll_interval, pause)

    if injector is None:
        for key, label in FAULT_KEYS:
            report.add(key, label, SKIP, fault_note or "没有故障注入器：模拟设备可直接注入；真实设备要在网络路径上注入")
    elif not_ready:
        for key, label in FAULT_KEYS:
            report.add(key, label, SKIP, f"{not_ready}：故障项目没跑")
    else:
        _fault_checks(report, factory, command, injector, poll_timeout, poll_interval, pause)
    return report


def _clean_up(report: Report, instance: DeviceAdapter | None, submitted: list[str], contract: dict[str, Any],
              command: Callable[..., CommandRequest], *, pause: Callable[[float], None] = time.sleep,
              interval: float = 0.2, settle: float = 5.0, key: str = "cleanup") -> None:
    """验收收尾：发过的验收指令逐条按指令号查，还没结论的试着终止，终止不了的记成残留——设备可能仍在动作。

    终止之后给设备几秒钟停下来（按指令号查到终态才算停住）；不支持状态查询的设备，终止回执确认了就算停住
    （契约：终止确认 = 设备已在安全状态）。`instance` 为空（连驱动实例都建不起来）时发过的全算残留。

    残留让报告不通过（`cleanup`），调用方据此挡住这台设备：ILCS 的指令表里没有这些 ACC- 指令，
    不挡的话生产指令会撞上一台其实还在忙的设备。
    """
    queryable = contract.get("supports_query", True)
    leftovers = []
    for index, command_id in enumerate(dict.fromkeys(submitted)):
        if instance is None:
            leftovers.append(command_id)
            continue
        state = _query_settled(instance, command_id, settle, interval, pause) if queryable else None
        if state is not None and state.state in TERMINAL:
            continue
        if contract.get("supports_abort", True):
            try:
                # 终止指令号按序号区分：不同目标的收尾终止撞了号，设备会把后一条当重投回放、不去停第二个目标
                receipt = instance.abort(command(f"{key}-{index}", type="abort", target_command_id=command_id))
                if queryable:
                    after, _ = _wait(instance, command_id, settle, interval, pause)
                    if after is not None and after.state in TERMINAL:
                        continue
                elif receipt.state == "done":
                    continue
            except (AdapterError, AdapterUnreachable):
                pass
        leftovers.append(command_id)
    report.leftovers = leftovers
    if leftovers:
        report.add(
            key, "验收指令收尾", FAIL,
            f"验收留下了没结束的动作 {'、'.join(leftovers)}：设备可能仍在动作，要现场核查、处理后重新验收",
            leftovers=leftovers,
        )


def _query_settled(instance: DeviceAdapter, command_id: str, settle: float, interval: float,
                   pause: Callable[[float], None]) -> CommandResult | None:
    """按指令号查一次；连不上时在 `settle` 秒内重试（设备刚从失联里恢复，连接还没建立起来）。"""
    deadline = time.monotonic() + settle
    while True:
        try:
            return instance.query(command_id)
        except (AdapterError, AdapterUnreachable):
            if time.monotonic() >= deadline:
                return None
            pause(interval)


def _motion_counter(injector: FaultInjector | None) -> Callable[[], int | None]:
    counter = getattr(injector, "motions", None) if injector is not None else None

    def read() -> int | None:
        if counter is None:
            return None
        try:
            return counter()
        except (AdapterError, AdapterUnreachable):
            return None

    return read


def _moved(injector: FaultInjector | None, command_id: str, before: int | None,
           motions: Callable[[], int | None]) -> tuple[int | None, str]:
    """这条指令让设备动作了几次：设备认指令号就按指令号数，不认就看总动作次数的变化。读不到返回 None。"""
    count = injector.executions(command_id) if injector is not None else None
    if count is not None:
        return count, f"设备实际动作 {count} 次"
    after = motions()
    if before is None or after is None:
        return None, ""
    return after - before, f"设备总动作次数 {before} → {after}"


def _wait(instance: DeviceAdapter, command_id: str, timeout: float, interval: float,
          pause: Callable[[float], None]) -> tuple[CommandResult | None, list[str]]:
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
        pause(interval)
    return result, seen


def _physical_checks(
    report: Report, instance: DeviceAdapter, factory: Callable[[], DeviceAdapter],
    command: Callable[..., CommandRequest], contract: dict[str, Any], injector: FaultInjector | None,
    timeout: float, interval: float, pause: Callable[[float], None],
) -> None:
    motions = _motion_counter(injector)
    queryable = contract.get("supports_query", True)
    submitted: list[str] = []

    def submit(request: CommandRequest) -> CommandResult:
        submitted.append(request.command_id)
        return instance.submit(request)

    try:
        first = command("run")
        try:
            submit(first)
        except AdapterError as exc:
            report.add("complete", "正常完成", FAIL, f"提交被拒：{exc}", command_id=first.command_id)
            for key, label in PHYSICAL_KEYS[1:]:
                report.add(key, label, SKIP, "正常完成没有通过，后面的动作项目不再跑")
            submitted.remove(first.command_id)  # 明确拒绝：设备没动
            return
        except AdapterUnreachable as exc:
            report.add("complete", "正常完成", FAIL, f"提交结果未知：{exc}", command_id=first.command_id)
            for key, label in PHYSICAL_KEYS[1:]:
                report.add(key, label, SKIP, "正常完成没有通过，后面的动作项目不再跑")
            return
        if not queryable:
            report.add("complete", "正常完成", SKIP, "契约声明不支持状态查询：做没做完要现场核对（这份报告不能当动作级证据）",
                       command_id=first.command_id)
            for key, label in PHYSICAL_KEYS[1:]:
                report.add(key, label, SKIP, "契约声明不支持状态查询，后面的动作项目不再跑")
            return
        result, seen = _wait(instance, first.command_id, timeout, interval, pause)
        done = result is not None and result.state == "done"
        report.add(
            "complete", "正常完成", PASS if done else FAIL,
            f"经过 {' → '.join(seen) or '—'}" + ("" if done else f"；{timeout:g} 秒内没有完成"),
            command_id=first.command_id,
        )
        if not done:
            for key, label in PHYSICAL_KEYS[1:]:
                report.add(key, label, SKIP, "正常完成没有通过，后面的动作项目不再跑")
            return

        try:
            before = motions()
            replay = instance.submit(first)
            count = injector.executions(first.command_id) if injector is not None else None
            after = motions() if count is None else None
            if count is not None:
                report.add(
                    "duplicate", "同一指令号重复提交", PASS if count == 1 else FAIL,
                    f"重投回 {replay.state}，设备实际动作 {count} 次" + ("" if count == 1 else "：同一指令动作了不止一次"),
                )
            elif before is not None and after is not None:
                report.add(
                    "duplicate", "同一指令号重复提交", PASS if after == before else FAIL,
                    f"重投回 {replay.state}，设备总动作次数 {before} → {after}"
                    + ("" if after == before else "：重投让设备又动作了"),
                )
            else:
                report.add(
                    "duplicate", "同一指令号重复提交", SKIP,
                    f"重投回 {replay.state}；读不到设备侧动作次数，是否只动作一次要现场核对",
                )
        except (AdapterError, AdapterUnreachable) as exc:
            report.add("duplicate", "同一指令号重复提交", FAIL, f"重投出错（应回放原结论）：{exc}")

        try:
            again = factory().query(first.command_id)
            same = again is not None and again.state == "done"
            report.add(
                "restart_query", "重建驱动后按指令号查回", PASS if same else FAIL,
                "新实例按指令号查回已完成" if same else f"新实例查回 {again.state if again else '没有这条指令'}",
            )
        except (AdapterError, AdapterUnreachable) as exc:
            report.add("restart_query", "重建驱动后按指令号查回", FAIL, f"新实例查询出错：{exc}")

        _hold_check(report, instance, submit, command, contract, timeout, interval, pause)
        _abort_check(report, instance, submit, command, contract)
    finally:
        _clean_up(report, instance, submitted, contract, command, pause=pause, interval=interval)


def _hold_check(report: Report, instance: DeviceAdapter, submit, command, contract, timeout, interval, pause) -> None:
    """保持一个在途动作，再把它放掉：先续跑到做完（生产上保持之后就是续跑；有的设备保持的是整机，
    只终止作业不会让它回到就绪），续跑不了再终止。放不掉的由收尾记成残留。"""
    if not contract.get("supports_hold", True):
        report.add("hold", "保持", SKIP, "契约声明不支持保持")
        return
    target = command("hold-target")
    try:
        submit(target)
        receipt = instance.hold(command("hold", type="hold", target_command_id=target.command_id))
        after = instance.query(target.command_id)
        state = after.state if after is not None else "not_found"
        ok = receipt.state in {"done", "accepted"} and state != "done"
        check = report.add("hold", "保持", PASS if ok else FAIL, f"保持回执 {receipt.state}；目标动作 {state}")
    except AdapterError as exc:
        after = _state_of(instance, target.command_id)
        if after in TERMINAL:
            report.add("hold", "保持", SKIP, f"设备拒绝保持，目标动作已经 {after}（来不及保持）：{exc}")
        else:
            report.add("hold", "保持", FAIL, f"设备拒绝保持，目标动作还在 {after}：{exc}")
        return
    except AdapterUnreachable as exc:
        report.add("hold", "保持", FAIL, f"保持指令结果未知：{exc}")
        return
    try:
        submit(command("hold-resume", type="resume", target_command_id=target.command_id))
        result, seen = _wait(instance, target.command_id, timeout, interval, pause)
        check.detail += f"；续跑后 {' → '.join(seen) or '—'}"
        if result is not None and result.state in TERMINAL:
            return
    except (AdapterError, AdapterUnreachable) as exc:
        check.detail += f"；续跑不了（{exc}）"
    if contract.get("supports_abort", True):
        try:
            instance.abort(command("hold-release", type="abort", target_command_id=target.command_id))
            check.detail += "，已终止放掉"
        except (AdapterError, AdapterUnreachable):
            pass  # 放不掉：收尾会按指令号再查，还没结论就记成残留


def _abort_check(report: Report, instance: DeviceAdapter, submit, command, contract) -> None:
    if not contract.get("supports_abort", True):
        report.add("abort", "终止", SKIP, "契约声明不支持终止")
        return
    target = command("abort-target")
    try:
        submit(target)
        receipt = instance.abort(command("abort", type="abort", target_command_id=target.command_id))
        state = _state_of(instance, target.command_id)
        ok = state == "failed"
        report.add("abort", "终止", PASS if ok else FAIL,
                   f"终止回执 {receipt.state}；目标动作 {state}" + ("" if ok else "：目标没有停下"))
    except AdapterError as exc:
        state = _state_of(instance, target.command_id)
        if state in TERMINAL:
            report.add("abort", "终止", SKIP, f"设备拒绝终止，目标动作已经 {state}（来不及终止）：{exc}")
        else:
            report.add("abort", "终止", FAIL, f"设备拒绝终止，目标动作还在 {state}：{exc}")
    except AdapterUnreachable as exc:
        report.add("abort", "终止", FAIL, f"终止指令结果未知：{exc}")


def _state_of(instance: DeviceAdapter, command_id: str) -> str:
    try:
        result = instance.query(command_id)
    except (AdapterError, AdapterUnreachable):
        return "unknown"
    return result.state if result is not None else "not_found"


def _fault_checks(
    report: Report, factory: Callable[[], DeviceAdapter], command: Callable[..., CommandRequest],
    injector: FaultInjector, timeout: float, interval: float, pause: Callable[[float], None],
) -> None:
    """回执丢失、设备忙、联锁、失联。

    - 模拟设备报注入不了的故障（点表设备没有回执可丢、中间表插入作业行就是交接）判跳过，不硬判不通过；
    - 驱动受理了的探针一律记下，收尾逐条核对、终止；丢回执的探针先等它结束（或终止）再测后面的——
      单作业设备在它跑完之前对新指令一律报忙，测到的就不是注入的故障了；
    - 提交只是交接的驱动（`handoff = "async"`：中间表插入作业行、PLC 点表写下启动沿）：设备忙、联锁的拒绝是设备
      之后才判断、异步回写的，等它回写成失败、且设备没动作才算通过；
    - 失联按驱动的判定时延（`offline_after_sec`，例如心跳超时）断开足够久，等它判出来。
    """
    motions = _motion_counter(injector)
    try:
        unsupported = dict(getattr(injector, "unsupported", lambda: {})() or {})
    except (AdapterError, AdapterUnreachable):
        unsupported = {}
    instance = factory()
    handoff = getattr(instance, "handoff", "") == "async"
    probes: list[str] = []
    try:
        lost = command("lost")
        if "lost_receipt" in unsupported:
            report.add("lost_receipt", "回执丢失", SKIP, f"这台模拟设备注入不了：{unsupported['lost_receipt']}")
        else:
            _lost_receipt_check(report, instance, factory, lost, injector, motions, probes, timeout, interval, pause)
        _settle(factory, command, probes, timeout, interval, pause)

        for mode, key, label in (("busy", "busy", "设备忙"), ("interlock", "interlock", "联锁")):
            if mode in unsupported:
                report.add(key, label, SKIP, f"这台模拟设备注入不了：{unsupported[mode]}")
                continue
            _rejection_check(report, instance, command(mode), injector, mode, key, label, motions, probes, handoff,
                             timeout, interval, pause)

        if "offline" in unsupported:
            report.add("offline", "失联", SKIP, f"这台模拟设备注入不了：{unsupported['offline']}")
        else:
            _offline_check(report, instance, factory, injector, timeout, interval, pause)
    finally:
        try:
            injector.set("none")
        except (AdapterError, AdapterUnreachable):
            pass  # 设备还没恢复：故障模式随模拟器重启复位，报告已经写完
        leftovers = list(report.leftovers)
        try:
            cleaner = factory()
        except (AdapterError, AdapterUnreachable):
            cleaner = None
        _clean_up(report, cleaner, probes, {"supports_query": True, "supports_abort": True}, command,
                  pause=pause, interval=interval, key="cleanup_faults")
        report.leftovers = leftovers + report.leftovers


def _lost_receipt_check(report, instance, factory, lost, injector, motions, probes, timeout, interval, pause) -> None:
    before = motions()
    injector.set("lost_receipt")
    try:
        instance.submit(lost)
        probes.append(lost.command_id)
        report.add("lost_receipt", "回执丢失", FAIL, "设备没回执，驱动却报告受理：应当判为结果未知")
    except AdapterUnreachable:
        probes.append(lost.command_id)
        injector.set("none")
        found, seen = _wait(factory(), lost.command_id, timeout, interval, pause)
        moved, detail = _moved(injector, lost.command_id, before, motions)
        ok = found is not None and found.state in TERMINAL and moved in (None, 1)
        report.add(
            "lost_receipt", "回执丢失", PASS if ok else FAIL,
            "驱动判为结果未知、不重发；恢复后按指令号查回 " + (" → ".join(seen) or "—")
            + (f"，{detail}" if detail else "")
            + ("" if found is not None and found.state in TERMINAL else f"；{timeout:g} 秒内没有结论"),
        )
    except AdapterError as exc:
        report.add("lost_receipt", "回执丢失", FAIL, f"驱动把「回执丢失」判成明确失败：{exc}（设备其实可能已动作）")
    finally:
        injector.set("none")


def _rejection_check(report, instance, probe, injector, mode, key, label, motions, probes, handoff,
                     timeout, interval, pause) -> None:
    before = motions()
    injector.set(mode)
    try:
        result = instance.submit(probe)
        probes.append(probe.command_id)
        if handoff and result.state in {"accepted", "running", "unknown"}:
            # 提交只是交接：设备之后才判断，拒绝异步回写。故障保持注入，等它回写
            final, seen = _wait(instance, probe.command_id, timeout, interval, pause)
            moved, detail = _moved(injector, probe.command_id, before, motions)
            ok = final is not None and final.state == "failed" and moved in (None, 0)
            report.add(
                key, label, PASS if ok else FAIL,
                f"交给设备后，设备回写 {' → '.join(seen) or '—'}"
                + (f"：{final.error}" if final is not None and final.error else "") + (f"，{detail}" if detail else "")
                + ("" if ok else f"：设备处于{label}状态，应当回写拒绝、设备不动作"),
            )
        else:
            report.add(key, label, FAIL, f"设备处于{label}状态，驱动却报告受理")
    except AdapterError as exc:
        moved, detail = _moved(injector, probe.command_id, before, motions)
        report.add(
            key, label, PASS if moved in (None, 0) else FAIL,
            f"明确失败、设备未动作：{exc}" if moved in (None, 0) else f"报失败但{detail}",
        )
    except AdapterUnreachable as exc:
        probes.append(probe.command_id)
        report.add(key, label, FAIL, f"应是明确失败，驱动却判为结果未知：{exc}")
    finally:
        injector.set("none")


def _offline_check(report, instance, factory, injector, timeout, interval, pause) -> None:
    # 心跳类的判定要等心跳超时才看得出失联：断开得比判定时延久，也等得比它久
    latency = float(getattr(instance, "offline_after_sec", 0) or 0)
    seconds = latency + 3
    injected = time.monotonic()
    injector.set("offline", seconds)
    offline_error = None
    # 断开可能是异步的（模拟器先把控制应答送回再停听）：反复探测，直到判为失联
    deadline = injected + latency + 5
    while time.monotonic() < deadline:
        try:
            factory().healthcheck()
        except (AdapterUnreachable, AdapterError) as exc:
            offline_error = exc
            break
        pause(interval)
    if offline_error is None:
        report.add("offline", "失联", FAIL, "设备已断开，健康检查仍报在线")
        return
    report.add("offline", "失联", PASS, f"健康检查判为失联：{offline_error}")
    # 等断开的时长过去、设备连续两次答得上来，后面的清理才连得上（模拟器停听有延迟，刚判失联时可能还在关）
    while time.monotonic() < injected + seconds:
        pause(interval)
    back, answered = time.monotonic() + max(timeout, 10), 0
    while time.monotonic() < back and answered < 2:
        try:
            factory().healthcheck()
            answered += 1
        except (AdapterUnreachable, AdapterError):
            answered = 0
        pause(interval)


def _settle(factory, command, command_ids, timeout, interval, pause) -> None:
    """等这些探针结束；超时还在动的先终止——后面的项目要对着空闲的设备测。结论不写报告：收尾再统一核对。"""
    for index, command_id in enumerate(list(command_ids)):
        try:
            instance = factory()
            result, _ = _wait(instance, command_id, timeout, interval, pause)
            if result is None or result.state not in TERMINAL:
                instance.abort(command(f"settle-{index}", type="abort", target_command_id=command_id))
                _wait(instance, command_id, min(timeout, 10), interval, pause)
        except (AdapterError, AdapterUnreachable):
            pass
