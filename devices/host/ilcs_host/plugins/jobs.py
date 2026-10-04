"""没有 ILCS 任务契约的设备：驱动自己记作业台账（从 ILCS 的 `adapters/jobs.py` 抽出，错误带 SiLA 错误码）。

串口命令仪器、PLC 点表、厂家 REST 接口这类设备不认识 ILCS 指令号，不会按指令号查询，也不会去重。
`MappedJobAdapter` 在驱动侧补上这一层，子类只写设备 I/O：

- 每台设备一份作业台账（驱动宿主 `state_dir` 下的 `<设备>.json`），**先落盘再动设备**：驱动宿主重启后仍能
  按原指令号回答「这条指令在设备上怎样了」；同一指令号重复投递回放原作业，不再动作。
- 同一时刻只有一个在途作业。设备在运行或保持中就明确拒绝（DeviceBusy），不排队、不覆盖；
  设备停在上一个作业的完成 / 故障状态时先复位，复位不了也拒绝。
- 结论看设备状态：见到运行后回到空闲或报完成 → 完成；报故障 → 失败（带实测值与故障说明）。
  设备停在空闲、报了登记为「拒绝启动」的故障码（`start_refused`，子类实现）→ 设备明确没动，明确失败；
  发了启动命令却一直没见到运行、也没有完成信号，超过 `start_timeout_sec` 就是**结果未知**，转人工核查，不猜。
- 启动命令发出之前的失败（参数写不进、回复报错）设备没有动作 → 明确失败；启动命令发出之后
  没拿到确认 → 台账记「未确认」，回执是结果未知，之后见到设备在运行才按运行处理，质量标 uncertain。

设备没有时钟时，回执里的 device_ts 用驱动观测到状态的时间；遥测的设定值取指令参数里的同名数值。

**逐孔依次执行**：ILCS 矩阵条件让一条指令带上逐孔参数（`params.wells = {孔位: {参数: 值}}`，固定参数是缺省值）。
这类设备一次只做一个设定，驱动就按孔位顺序一个一个跑：每孔用固定参数叠上自己的参数，写参数、启动、等完成、取实测、
复位，再启动下一孔；回执按孔位回报（`delivered.wells[孔位]`）。提交时先把每一孔的参数都核对一遍，有一孔不对整条拒绝、
设备一次都没动。每孔有自己的运行号（`<指令号>/<序号>`，写指令号、回显、设备侧去重都按它），启动前先落盘：中途重启
按台账接着查当前孔、不重发已经启动过的孔。某一孔没做成（故障、拒绝启动、被终止）整条指令到此结束，回执写明是第几孔、
后面几孔没有执行。

**点位读写和任务执行是两层**：点表（`points`）登记了点就能按点读值；点上写了 `writable: true`（可带
`min` / `max`）的还能由人手动写一个值（`write_point_manually`：先读、再写、再回读）。只读写点位的设备不配能力映射
与状态，不参与自动流程；要参与自动流程（下发指令、确认做没做完），才要求能力映射与状态点。任务用的控制信号
（启动、状态、复位、指令号……）不能声明成可写：要动设备请走指令，免得绕过作业台账。
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import string
import threading
import time

from ..settings import settings
from .base import (
    AdapterContract, AdapterError, AdapterIndeterminate, AdapterUnreachable, CommandRequest,
    CommandResult,
)
from .contract import parse_receipt

# 模拟设备在身份里带这个标记（厂商、型号或序列号任一字段），正式环境一律拒绝
SIMULATOR_MARK = "ILCS-SIMULATOR"
# accepted：设备明确说「排队中、还没开始」（调度系统的任务队列）；排队期间不按启动超时判结果未知
NORMALIZED_STATES = {"idle", "accepted", "running", "held", "done", "failed"}
TERMINAL = {"done", "failed", "aborted", "rejected"}
KEEP_JOBS = 200
BUILTINS = {"command_id", "batch_id", "step_id", "capability", "program", "type"}
# 点上可以写的说明字段（和协议无关）：显示名、单位、可不可以手动写、手动写的范围
POINT_META = ("label", "unit", "writable", "min", "max")
_FORMATTER = string.Formatter()
_FIELD = re.compile(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)*")


def well_order(well: str) -> tuple:
    """孔位按板上的顺序排：A1、A2…A10、B1（字母行、数字列）；不是这种写法的排在后面、按原文。"""
    match = re.fullmatch(r"([A-Za-z]+)(\d+)", str(well))
    return (0, match.group(1).upper(), int(match.group(2)), "") if match else (1, "", 0, str(well))


def scalars(params: dict) -> dict:
    return {key: value for key, value in (params or {}).items() if not isinstance(value, (dict, list))}


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec="seconds")


# ---------- 模板 ----------

def placeholders(template: str) -> set[str]:
    """模板里引用的参数名（`{temp:.1f}` → temp）。"""
    names = set()
    for _, field, _, _ in _FORMATTER.parse(template):
        if field is not None:
            names.add(field)
    return names


def render(template: str, values: dict) -> str:
    """只做「按名字取值 + 格式说明」，不允许属性访问、下标与 !r 转换：模板来自配置，值来自指令。"""
    parts = []
    try:
        parsed = list(_FORMATTER.parse(template))
    except ValueError as exc:
        raise AdapterError(f"命令模板 {template!r} 格式错误：{exc}") from exc
    for literal, field, spec, conversion in parsed:
        parts.append(literal)
        if field is None:
            continue
        if conversion or not _FIELD.fullmatch(field) or "{" in (spec or ""):
            raise AdapterError(f"命令模板字段 {{{field}}} 不合法：只允许参数名与格式说明")
        if field not in values:
            raise AdapterError(f"命令模板需要参数 {field}，但指令里没有、配置也没有给缺省值")
        value = values[field]
        try:
            text = format(value, spec or "")
        except (TypeError, ValueError) as exc:
            raise AdapterError(f"参数 {field} = {value!r} 不能按格式 {spec!r} 输出") from exc
        if any(ord(char) < 32 for char in text):
            raise AdapterError(f"参数 {field} 含控制字符，拒绝发往设备")
        parts.append(text)
    return "".join(parts)


def render_value(template, values: dict):
    """JSON 模板：字符串逐个渲染；整串就是一个占位符时保留原值类型（数字不变字符串）。"""
    if isinstance(template, str):
        whole = re.fullmatch(r"\{([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\}", template)
        if whole:
            if whole.group(1) not in values:
                raise AdapterError(f"请求模板需要参数 {whole.group(1)}，但指令里没有、配置也没有给缺省值")
            return values[whole.group(1)]
        return render(template, values)
    if isinstance(template, list):
        return [render_value(item, values) for item in template]
    if isinstance(template, dict):
        return {key: render_value(item, values) for key, item in template.items()}
    return template


def template_fields(template) -> set[str]:
    if isinstance(template, str):
        return placeholders(template)
    if isinstance(template, list):
        return set().union(*(template_fields(item) for item in template)) if template else set()
    if isinstance(template, dict):
        return set().union(*(template_fields(item) for item in template.values())) if template else set()
    return set()


def point_meta(spec) -> dict:
    """点的说明字段（点写成字符串简写时没有）。"""
    return {key: spec.get(key) for key in POINT_META if key in spec} if isinstance(spec, dict) else {}


def check_point_meta(name: str, spec, control: set[str]) -> None:
    """配置检查：说明字段的类型、范围；任务用的控制信号不能声明成可写。"""
    meta = point_meta(spec)
    for key in ("label", "unit"):
        if key in meta and not isinstance(meta[key], str):
            raise AdapterError(f"点 {name} 的 {key} 必须是文字")
    if "writable" in meta and not isinstance(meta["writable"], bool):
        raise AdapterError(f"点 {name} 的 writable 只能是 true / false")
    for key in ("min", "max"):
        value = meta.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                  or not math.isfinite(value)):
            raise AdapterError(f"点 {name} 的 {key} 必须是数")
    if meta.get("min") is not None and meta.get("max") is not None and meta["min"] > meta["max"]:
        raise AdapterError(f"点 {name} 的 min 大于 max")
    if meta.get("writable") and name in control:
        raise AdapterError(f"点 {name} 是任务用的控制信号（启动、状态、复位、指令号这类），不能声明成可手动写："
                           "要让设备动作请走指令")


def plain(value):
    """读回来的值转成能放进 JSON 的样子：数、布尔、文字原样；其他（时间、字节、协议对象）转文字。"""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def same_value(written, read, tolerance: float = 0.0) -> bool:
    """回读值和写入值是不是一回事：数按容差比（单精度、比例换算会有尾差），其余按文字比。"""
    if isinstance(written, bool) or isinstance(read, bool):
        return _truthy(written) == _truthy(read)
    if isinstance(written, (int, float)) and isinstance(read, (int, float)):
        limit = max(tolerance, 1e-6 * max(1.0, abs(float(written))))
        return abs(float(written) - float(read)) <= limit
    return str(written) == str(read)


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"true", "1", "on"}


def flatten(params: dict, prefix: str = "") -> dict:
    """转运这类嵌套参数按点号展开：{"to": {"location_id": "X"}} → {"to.location_id": "X"}。"""
    flat = {}
    for key, value in (params or {}).items():
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(flatten(value, f"{name}."))
        else:
            flat[name] = value
    return flat


# ---------- 作业台账 ----------

def _safe(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._#-]", "_", name)
    if not cleaned or cleaned.startswith("."):
        raise AdapterError(f"工位编号 {name!r} 不能用作台账文件名")
    return cleaned


class JobJournal:
    """一台设备一份 JSON：作业、别名（恢复指令沿用原作业）、当前在途作业。原子替换写入。"""

    def __init__(self, key: str):
        root = Path(settings.adapter_state_dir)
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise AdapterError(f"作业台账目录 {root} 无法创建：{exc}；不能在没有台账的情况下驱动设备") from exc
        self.path = root / f"{_safe(key)}.json"
        self.jobs: dict[str, dict] = {}
        self.aliases: dict[str, str] = {}
        self.active = ""
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self.jobs = dict(raw.get("jobs") or {})
                self.aliases = dict(raw.get("aliases") or {})
                self.active = str(raw.get("active") or "")
            except (OSError, ValueError, AttributeError) as exc:
                # 台账坏了就不知道设备上有哪些作业：不能当成空台账继续
                raise AdapterError(f"作业台账 {self.path} 无法读取（{exc}）；请人工核对设备后再处理") from exc

    def find(self, command_id: str) -> dict | None:
        return self.jobs.get(self.aliases.get(command_id, command_id))

    def put(self, job: dict, *, active: bool | None = None) -> None:
        self.jobs[job["id"]] = job
        if active is True:
            self.active = job["id"]
        elif active is False and self.active == job["id"]:
            self.active = ""

    def alias(self, command_id: str, job_id: str) -> None:
        self.aliases[command_id] = job_id

    def current(self) -> dict | None:
        return self.jobs.get(self.active) if self.active else None

    def save(self) -> None:
        # 只留在途作业与最近的已结束作业
        finished = sorted(
            (job for job in self.jobs.values() if job["state"] in TERMINAL and job["id"] != self.active),
            key=lambda job: job.get("updated_at", 0),
        )
        for job in finished[:-KEEP_JOBS] if len(finished) > KEEP_JOBS else []:
            self.jobs.pop(job["id"], None)
        self.aliases = {alias: target for alias, target in self.aliases.items() if target in self.jobs}
        payload = json.dumps({"jobs": self.jobs, "aliases": self.aliases, "active": self.active},
                             ensure_ascii=False, indent=1)
        temporary = self.path.with_suffix(".tmp")
        try:
            with open(temporary, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except OSError as exc:
            raise AdapterError(f"作业台账 {self.path} 写不进去：{exc}") from exc


# ---------- 驱动基类 ----------

class MappedJobAdapter:
    """子类实现：`read_identity`、`start_job`、`read_status`、`read_actuals`，按需实现
    `hold_job` / `resume_job` / `abort_job` / `acknowledge` / `precheck`。"""

    DRIVER = ""
    PROTOCOL = ""
    NOTE = ""
    # 称重这类即时动作：start_job 直接返回结果，作业当场完成
    SYNCHRONOUS = False

    def __init__(self, record, journal_key: str = ""):
        self.record = record
        self.station_id = record.station_id
        self.config = dict(record.config or {})
        self.credential_ref = getattr(record, "credential_ref", "") or ""
        self.expected_device_id = str(self.config.get("expected_device_id") or "")
        self.start_timeout = self._seconds("start_timeout_sec", 30.0)
        self.material_map = self.config.get("material_map") or {}
        if not isinstance(self.material_map, dict):
            raise AdapterError("material_map 必须是 {实测参数: {material, unit, factor}}")
        self._lock = threading.RLock()
        self.contract = AdapterContract(
            kind="real", protocol=record.protocol or self.PROTOCOL, version=record.version or "1.0",
            supports_hold=bool(record.supports_hold), supports_abort=bool(record.supports_abort),
            supports_query=bool(record.supports_query), supports_dedup=bool(record.supports_dedup),
            note=record.note or self.NOTE,
        )
        self.journal = JobJournal(journal_key or self.station_id)

    # ---------- 配置工具 ----------

    def _seconds(self, key: str, default: float, *, maximum: float = 24 * 3600, source: dict | None = None) -> float:
        source = self.config if source is None else source
        try:
            value = float(source.get(key, default))
        except (TypeError, ValueError) as exc:
            raise AdapterError(f"{key} 必须是正数") from exc
        if not math.isfinite(value) or value <= 0 or value > maximum:
            raise AdapterError(f"{key} 超出允许范围（0–{maximum:g}）")
        return value

    def capability_spec(self, capability: str) -> dict:
        specs = self.config.get("capabilities") or {}
        if not isinstance(specs, dict):
            raise AdapterError("capabilities 必须是 {能力: 映射}")
        if not specs:
            raise AdapterError(f"这台设备只配了点表（只读写点位），不参与自动流程：要下发 {capability}，先在适配器配置里"
                               "配能力映射与状态；设备没有动作", code="NotSupported")
        spec = specs.get(capability)
        if not isinstance(spec, dict):
            raise AdapterError(f"能力 {capability} 没有在适配器配置 capabilities 里映射到设备命令，设备没有动作", code="NotSupported")
        return spec

    # ---------- 子类钩子 ----------

    def read_identity(self) -> dict:
        raise NotImplementedError

    def accepted_params(self, spec: dict) -> set[str] | None:
        """这项能力接受哪些参数；None 表示不检查（例如转运指令的系统参数）。"""
        return None

    def precheck(self, spec: dict) -> None:
        """启动前的就绪 / 联锁检查；不满足抛 AdapterError（设备没动）。"""

    def check_start(self, spec: dict, values: dict) -> None:
        """不碰设备，把这一次启动要发的东西（写点、请求、命令）先整套拼一遍：缺参数、格式不对、选项没登记代码，在发出第一条
        之前就抛 AdapterError。提交时每一次运行都核对——逐孔执行不会跑完前几孔才发现后面的孔拼不出来。"""

    def start_job(self, job: dict, spec: dict, values: dict) -> dict | None:
        raise NotImplementedError

    def read_status(self, job: dict) -> tuple[str, str]:
        raise NotImplementedError

    def read_actuals(self, job: dict, spec: dict) -> dict:
        return dict(job.get("actuals") or {})

    def device_state(self) -> str:
        """设备整体状态（空闲 / 运行 / 保持 / 完成 / 故障），投递新作业前判忙用。缺省就是读状态点。"""
        return self.read_status({})[0]

    def hold_job(self, job: dict) -> None:
        raise AdapterError("该设备的映射没有配置保持命令", code="NotSupported")

    def resume_job(self, job: dict) -> None:
        raise AdapterError("该设备的映射没有配置恢复命令", code="NotSupported")

    def abort_job(self, job: dict | None) -> None:
        raise AdapterError("该设备的映射没有配置终止命令", code="NotSupported")

    def acknowledge(self) -> bool:
        """复位完成 / 故障状态；没有配置复位命令返回 False。"""
        return False

    def lookup(self, job: dict) -> bool:
        """启动未确认的作业能否在设备侧找回（REST 按请求里带的指令号查）；找回了返回 True。"""
        return False

    def close(self) -> None:
        """释放连接；注册表在配置换版本时调用。"""

    # ---------- 身份与健康 ----------

    def identity(self) -> dict:
        with self._lock:
            raw = dict(self.read_identity() or {})
        marker = " ".join(str(raw.get(key) or "") for key in ("vendor", "model", "serial", "device_id", "firmware"))
        raw["simulator"] = bool(raw.get("simulator")) or SIMULATOR_MARK in marker
        raw.setdefault("vendor", self.config.get("vendor", ""))
        return raw

    def healthcheck(self) -> dict:
        identity = self.identity()
        if identity["simulator"] and settings.environment == "production":
            raise AdapterError(f"该设备自报为模拟器（{SIMULATOR_MARK}）；正式环境不接入模拟设备")
        actual = str(identity.get("device_id") or identity.get("serial") or "")
        if self.expected_device_id and actual != self.expected_device_id:
            raise AdapterError(f"设备身份不匹配：期望 {self.expected_device_id}，实际 {actual or '缺失'}")
        return {
            "reachable": True, "driver": self.DRIVER, "protocol": self.contract.protocol,
            "device_id": actual, "model": identity.get("model", ""), "simulator": identity["simulator"],
            "interlock": bool(identity.get("interlock")),
            "accepts_commands": bool(identity.get("accepts_commands", True)),
        }

    # ---------- 点位读写（不参与自动流程也能用）----------

    @property
    def tasks(self) -> bool:
        """配了能力映射：这台设备参与自动流程（接指令）。没配就是只读写点位。"""
        return bool(self.config.get("capabilities"))

    def point_specs(self) -> dict:
        """{点名: 点的定义}；没有点表返回 {}。子类给。"""
        return {}

    def control_points(self) -> set[str]:
        """任务用的控制信号点（启动、状态、复位、指令号……）：不能手动写。子类给。"""
        return set()

    def read_point_value(self, name: str):
        """读一个点的工程值。读不到抛 AdapterUnreachable / AdapterIndeterminate。子类给。"""
        raise AdapterError(f"{self.PROTOCOL} 没有点表")

    def write_point_value(self, name: str, value) -> None:
        """写一个点。设备明确不收抛 AdapterError；没拿到结论抛 AdapterUnreachable / AdapterIndeterminate。子类给。"""
        raise AdapterError(f"{self.PROTOCOL} 没有点表")

    def write_tolerance(self, name: str) -> float:
        """回读比较的容差（比例换算、整数寄存器会有尾差）。"""
        return 0.0

    def point_catalog(self) -> list[dict]:
        control = self.control_points()
        rows = []
        for name, spec in self.point_specs().items():
            meta = point_meta(spec)
            rows.append({
                "name": name, "label": str(meta.get("label") or ""), "unit": str(meta.get("unit") or ""),
                "writable": bool(meta.get("writable")), "min": meta.get("min"), "max": meta.get("max"),
                "control": name in control,
            })
        return rows

    def io_timeout(self) -> float:
        """一次设备请求的超时（秒）。读点位时据此判断设备是不是不回话了。"""
        return float(getattr(self, "request_timeout", 0) or 0)

    def read_points(self, names: list[str] | None = None) -> list[dict]:
        """按点表逐个读；某个点读不到只在那一行写明，不影响别的点。

        设备不回话（连不上、超时，而且确实等了大半个超时）就不再读后面的点：设备停了，每个点都要等满超时，读一次点表
        就是「点数 × 超时」，调用方（界面、SiLA 客户端）早就超时了。设备很快回了错（地址不对、节点不存在、HTTP 5xx）
        只算那一个点，接着读别的。
        """
        rows, silent = [], None
        for row in self.point_catalog():
            if names and row["name"] not in names:
                continue
            if silent is not None:
                rows.append({**row, "value": None, "error": f"设备没有回话（{silent}），没有再读"})
                continue
            started = time.monotonic()
            try:
                with self._lock:
                    value = self.read_point_value(row["name"])
                rows.append({**row, "value": plain(value), "error": ""})
            except (AdapterError, AdapterUnreachable, AdapterIndeterminate) as exc:
                rows.append({**row, "value": None, "error": str(exc)})
                waited = time.monotonic() - started
                if isinstance(exc, AdapterUnreachable) and not isinstance(exc, AdapterIndeterminate) \
                        and waited >= self.io_timeout() / 2:
                    silent = exc
        return rows

    def check_manual_write(self, name: str, value) -> None:
        """手动写之前的核对（不碰设备）：点登记了、声明了可写、不是控制信号、值是单个值、在范围里。"""
        specs = self.point_specs()
        if name not in specs:
            raise AdapterError(f"点 {name} 没有在点表里登记", code="UnknownPoint")
        meta = point_meta(specs[name])
        if not meta.get("writable"):
            raise AdapterError(f"点 {name} 没有声明可写（writable: true），不能手动写", code="NotWritable")
        if name in self.control_points():
            raise AdapterError(f"点 {name} 是任务用的控制信号，不能手动写：要让设备动作请走指令", code="ControlPoint")
        if value is None or isinstance(value, (dict, list)):
            raise AdapterError("一次只能写一个值（数、布尔或文字）", code="InvalidValue")
        if isinstance(value, float) and not math.isfinite(value):
            raise AdapterError("值必须是有限的数", code="InvalidValue")
        low, high = meta.get("min"), meta.get("max")
        if low is not None or high is not None:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise AdapterError(f"点 {name} 限定了范围（{low if low is not None else '—'}–{high if high is not None else '—'}），要写一个数", code="InvalidValue")
            if (low is not None and value < low) or (high is not None and value > high):
                raise AdapterError(f"{value:g} 超出点 {name} 允许的范围 {low if low is not None else '—'}–"
                                   f"{high if high is not None else '—'}", code="OutOfRange")

    def write_point_manually(self, name: str, value) -> dict:
        """先读当前值、再写、再回读。

        - 写之前出的错（核对不过、读不到当前值、设备明确不收）：设备没动 → AdapterError；
        - 写出去了却没拿到结论、写完读不回来：结果未知 → AdapterIndeterminate，要人核对设备上的值。
        返回 {before, after, matches}：`matches` 为假说明设备收下了、但回读值和写入值不同（被设备限幅、换算）。
        """
        self.check_manual_write(name, value)
        with self._lock:
            try:
                before = self.read_point_value(name)
            except (AdapterError, AdapterUnreachable, AdapterIndeterminate) as exc:
                raise AdapterError(f"读不到 {name} 的当前值（{exc}），没有写入", code="DeviceUnreachable") from exc
            try:
                self.write_point_value(name, value)
            except AdapterError as exc:
                raise AdapterError(str(exc), code="WriteRejected") from exc
            except (AdapterUnreachable, AdapterIndeterminate) as exc:
                raise AdapterIndeterminate(f"写 {name} = {value!r} 没有结论（{exc}）：设备上的值可能已经变了，请核对") from exc
            try:
                after = self.read_point_value(name)
            except (AdapterError, AdapterUnreachable, AdapterIndeterminate) as exc:
                raise AdapterIndeterminate(f"写 {name} = {value!r} 之后读不回来（{exc}）：设备上的值请现场核对") from exc
        return {"before": plain(before), "after": plain(after),
                "matches": same_value(value, after, self.write_tolerance(name))}

    # ---------- 参数与模板取值 ----------

    def values_for(self, request: CommandRequest, spec: dict, params: dict | None = None) -> dict:
        params = (request.params or {}) if params is None else params
        accepted = self.accepted_params(spec)
        if request.type != "transfer" and accepted is not None:
            for name, value in params.items():
                if isinstance(value, (dict, list)):
                    raise AdapterError(
                        f"{self.PROTOCOL} 映射只接受标量参数，不支持 {name} 这类结构化参数（如孔位矩阵）；"
                        f"请改用能传 JSON 的驱动（SiLA 2 / OPC UA TaskExecution / HTTPS 网关）"
                    )
                if name not in accepted:
                    raise AdapterError(f"参数 {name} 在设备映射里没有对应的写入点或命令，拒绝下发（不静默丢弃设定值）")
        values = {**(spec.get("defaults") or {}), **flatten(params)}
        default = spec.get("program") if isinstance(spec.get("program"), str) else ""
        program = str((request.method or {}).get("program") or default or "")
        values.update({
            "command_id": request.command_id, "batch_id": request.batch_id, "step_id": request.step_id,
            "capability": request.capability, "program": program, "type": request.type,
        })
        return values

    def runs_for(self, request: CommandRequest, spec: dict) -> list[dict]:
        """这条指令要在设备上跑几次：一般一次；带了逐孔参数（`params.wells`）就按孔位顺序每孔一次，每孔用固定参数叠上
        自己的参数、带自己的运行号（`<指令号>/<序号>`，模板里还能用 `{well}`）。每一孔都先核对参数、再把整套启动拼一遍
        （`check_start`）：有一孔不对就整条拒绝，设备一次都没动。"""
        params = dict(request.params or {})
        wells = params.pop("wells", None)
        if wells is None:
            values = self.values_for(request, spec, params)
            self.check_start(spec, values)
            return [{"well": "", "params": params, "values": values}]
        if not isinstance(wells, dict) or not wells or not all(isinstance(row, dict) for row in wells.values()):
            raise AdapterError("逐孔参数 wells 必须是 {孔位: {参数: 值}}，设备没有动作", code="InvalidParameters")
        runs = []
        for index, well in enumerate(sorted(wells, key=well_order), start=1):
            merged = {**params, **wells[well]}
            try:
                values = self.values_for(request, spec, merged)
                values.update({"command_id": f"{request.command_id}/{index}", "well": str(well)})
                self.check_start(spec, values)
            except AdapterError as exc:
                raise AdapterError(f"孔位 {well} 的参数不对：{exc}；整条指令没有下发，设备没有动作", code="InvalidParameters") from exc
            runs.append({"well": str(well), "params": merged, "values": values})
        return runs

    # ---------- 回执 ----------

    def _receipt(self, job: dict, command_id: str) -> CommandResult:
        state = job["state"]
        mapped = {
            "accepted": "accepted", "running": "running", "done": "done", "failed": "failed",
            "aborted": "failed", "rejected": "failed", "unknown": "unknown", "unconfirmed": "unknown",
            "starting": "unknown",
        }[state]
        telemetry = [
            {"metric": metric, "value": value, "setpoint": setpoint}
            for metric, value, setpoint in job.get("telemetry") or []
        ]
        quality = job.get("quality") or "good"
        if mapped in {"failed", "unknown"} and quality == "good":
            quality = "bad" if mapped == "failed" else "uncertain"
        return parse_receipt({
            "command_id": command_id, "state": mapped, "quality": quality,
            "device_ts": _iso(job.get("updated_at") or time.time()),
            "delivered": job.get("delivered") or {}, "telemetry": telemetry, "error": job.get("error") or "",
        }, command_id, f"real:{self.DRIVER}")

    def _control_receipt(self, command_id: str, note: str) -> CommandResult:
        return parse_receipt({
            "command_id": command_id, "state": "done", "quality": "good", "device_ts": _iso(time.time()),
            "delivered": {"note": note}, "telemetry": [], "error": "",
        }, command_id, f"real:{self.DRIVER}")

    def _materials(self, actuals: dict) -> list[dict]:
        rows = []
        for name, mapping in self.material_map.items():
            if name in actuals and isinstance(mapping, dict) and mapping.get("material"):
                rows.append({
                    "material": mapping["material"], "unit": mapping.get("unit", ""),
                    "quantity": round(float(actuals[name]) * float(mapping.get("factor", 1)), 6),
                })
        return rows

    def _finish(self, job: dict, state: str, error: str = "") -> None:
        spec = self.capability_spec(job["capability"]) if job.get("capability") else {}
        # 数值实测值进遥测并参与物料折算；文本结果（如读码器读到的条码）只进回执
        raw = self.read_actuals(job, spec) or {}
        actuals = {name: round(float(value), 6) for name, value in raw.items()
                   if isinstance(value, (int, float)) and not isinstance(value, bool)}
        texts = {name: value for name, value in raw.items() if name not in actuals}
        if job.get("wells"):
            self._finish_well(job, spec, state, error, actuals, texts)
            return
        delivered = {**(job.get("delivered") or {}), **texts, **actuals}
        materials = self._materials(actuals)
        if materials:
            delivered["materials"] = materials
        setpoints = job.get("params") or {}
        job["telemetry"] = [
            [name, value, float(setpoints[name]) if isinstance(setpoints.get(name), (int, float))
             and not isinstance(setpoints.get(name), bool) else None]
            for name, value in actuals.items()
        ] if state == "done" else []
        job["delivered"] = delivered
        job["state"] = state
        job["phase"] = ""
        if error:
            job["error"] = error
        job["updated_at"] = time.time()
        self.journal.put(job, active=False)

    # ---------- 逐孔依次执行 ----------

    def _finish_well(self, job: dict, spec: dict, state: str, error: str, actuals: dict, texts: dict) -> None:
        """记下当前这一孔的结论；做完了、后面还有孔就复位并启动下一孔，否则结束整条指令。"""
        index = job["well_index"]
        run = job["wells"][index]
        run.update({
            "state": state, "error": error, "quality": job.get("quality") or "good", "actuals": actuals,
            "delivered": {**(job.get("delivered") or {}), **texts, **actuals}, "finished_at": time.time(),
        })
        if state == "done" and index + 1 < len(job["wells"]):
            if self._start_next_well(job, spec):
                return
            state = "failed"
        self._close_wells(job, state)

    def _start_next_well(self, job: dict, spec: dict) -> bool:
        """上一孔做完：复位、核对就绪，先落盘再启动下一孔。设备明确不接（没就绪、联锁、参数写不进）→ 这一孔没有动作，
        返回 False；发出启动却没拿到确认 → 这一孔记「未确认」并照常抛出（下一轮按回显 / 状态再判）。"""
        index = job["well_index"] + 1
        run = job["wells"][index]
        try:
            pre_state = self.device_state()
            if pre_state in {"done", "failed"} and self.acknowledge():
                pre_state = self.device_state()
            if pre_state in {"running", "held"}:
                raise AdapterError("上一孔做完了，设备却还在运行 / 保持，没有启动下一孔")
            self.precheck(spec)
        except AdapterError as exc:
            job["well_index"] = index
            run.update({"state": "rejected", "error": f"没有启动：{exc}"})
            return False
        job.update({
            "well_index": index, "run_id": run["values"]["command_id"], "state": "starting", "phase": "",
            "seen_running": False, "pre_state": pre_state, "unconfirmed": False, "quality": run.get("quality") or "good",
            "params": dict(run["params"]), "started_at": time.time(), "updated_at": time.time(), "delivered": {},
            "actuals": {},
        })
        job.pop("handle", None)
        run["state"] = "starting"
        self.journal.put(job)
        self.journal.save()  # 先落盘再动设备：重启后知道这一孔可能已经启动
        try:
            result = self.start_job(job, spec, run["values"])
        except AdapterUnreachable as exc:
            job.update({"state": "unconfirmed", "unconfirmed": True, "updated_at": time.time(),
                        "error": f"第 {index + 1} 孔（{run['well']}）的启动命令已发出但没有拿到确认：{exc}"})
            self.journal.put(job)
            self._save_quietly()
            raise
        except AdapterError as exc:
            run.update({"state": "rejected", "error": f"没有启动：{exc}"})
            return False
        except Exception as exc:
            job.update({"state": "unconfirmed", "unconfirmed": True, "error": f"驱动内部错误：{exc}"})
            self.journal.put(job)
            self._save_quietly()
            raise AdapterIndeterminate(f"驱动内部错误（{exc.__class__.__name__}），第 {index + 1} 孔的启动命令可能已发出") from exc
        if isinstance(result, dict):  # 即时动作：这一孔当场有结果，接着做下一孔
            job.update({"delivered": dict(result.get("delivered") or {}), "actuals": dict(result.get("actuals") or {}),
                        "seen_running": True})
            self._finish(job, "failed" if result.get("error") else "done", result.get("error", ""))
            return True
        job["state"] = "accepted"
        job["updated_at"] = time.time()
        self.journal.put(job)
        self._saved(f"第 {index + 1} 孔的启动")
        return True

    def _close_wells(self, job: dict, state: str) -> None:
        """整条指令结束：按孔位回报做完的孔；没做成的写明是第几孔、为什么、后面几孔没有执行。"""
        runs = job["wells"]
        index = job["well_index"]
        finished = [run for run in runs if run.get("state") == "done"]
        job["delivered"] = {"wells": {run["well"]: run.get("delivered") or {} for run in runs if run.get("delivered")}}
        totals: dict[tuple[str, str], float] = {}
        for run in finished:
            for row in self._materials(run.get("actuals") or {}):
                key = (row["material"], row["unit"])
                totals[key] = round(totals.get(key, 0.0) + row["quantity"], 6)
        if totals:
            job["delivered"]["materials"] = [{"material": material, "unit": unit, "quantity": quantity}
                                             for (material, unit), quantity in totals.items()]
        job["telemetry"] = [
            [name, value, float(run["params"][name]) if isinstance(run["params"].get(name), (int, float))
             and not isinstance(run["params"].get(name), bool) else None]
            for run in finished for name, value in (run.get("actuals") or {}).items()
        ]
        if any(run.get("quality") == "uncertain" for run in runs[:index + 1]):
            job["quality"] = "uncertain"
        if state != "done":
            current = runs[index]
            left = len(runs) - index - 1
            job["error"] = (f"第 {index + 1}/{len(runs)} 孔（{current['well']}）：{current.get('error') or '没有做成'}"
                            + (f"；后面 {left} 孔没有执行" if left else ""))
        job["state"] = state
        job["phase"] = ""
        job["updated_at"] = time.time()
        self.journal.put(job, active=False)

    # ---------- 状态推进 ----------

    def _poll(self, job: dict) -> None:
        """按设备状态推进一次；读不到状态照常抛 AdapterUnreachable（执行器下一轮再查）。"""
        if job["state"] in TERMINAL:
            return
        state, detail = self.read_status(job)
        if state not in NORMALIZED_STATES:
            raise AdapterIndeterminate(f"设备状态 {state!r} 没有映射到 idle/running/held/done/failed")
        elapsed = time.time() - job["started_at"]
        before = (job["state"], job.get("phase"), job.get("seen_running"))
        unconfirmed = job["state"] == "unconfirmed" or job.get("unconfirmed")
        stale_done = job.get("pre_state") == "done" and not job.get("seen_running")
        if state == "accepted":
            pass
        elif state in {"running", "held"}:
            job["seen_running"] = True
            job["state"] = "running"
            job["phase"] = "held" if state == "held" else ""
        elif state == "idle" and not job.get("seen_running") and (refused := self.start_refused(job, elapsed)):
            self._finish(job, "failed", refused)
        elif state == "failed" and not (job.get("pre_state") == "failed" and not job.get("seen_running")):
            if unconfirmed:
                job["quality"] = "uncertain"
            self._finish(job, "failed", detail or "设备报告故障")
        elif (state == "done" and not stale_done) or (state == "idle" and (
            job.get("seen_running") or (job.get("idle_after_start") == "done" and not unconfirmed)
        )):
            if unconfirmed:
                job["quality"] = "uncertain"
            self._finish(job, "done")
        elif elapsed > self.start_timeout and job["state"] not in {"unconfirmed", "unknown"}:
            job["state"] = "unknown"
            job["error"] = (
                f"发出启动命令 {elapsed:.0f} s 后设备仍未进入运行，也没有完成信号（当前 {state}）；"
                f"结果未知，转人工核查"
            )
        if unconfirmed and job["state"] == "running":
            job["quality"] = "uncertain"
        if (job["state"], job.get("phase"), job.get("seen_running")) != before:
            job["updated_at"] = time.time()
            self.journal.put(job)
            try:
                self.journal.save()
            except AdapterError as exc:
                raise AdapterIndeterminate(str(exc)) from exc

    def start_refused(self, job: dict, elapsed: float) -> str:
        """启动命令发出后设备停在空闲、明确表示拒绝了这次启动时，返回原因；缺省不判（等启动超时）。"""
        return ""

    # ---------- 契约 ----------

    def submit(self, request: CommandRequest) -> CommandResult:
        with self._lock:
            existing = self.journal.find(request.command_id)
            if existing is not None:
                if existing["state"] == "rejected":
                    raise AdapterError(existing.get("error") or "设备曾明确拒绝这条指令", code=existing.get("code") or "InvalidParameters")
                return self._receipt(existing, request.command_id)  # 重复投递：回放原作业，不再动作
            if request.type == "resume":
                held = self._held_for(request)
                if held is not None:
                    return self._resume(held, request)
            spec = self.capability_spec(request.capability)
            runs = self.runs_for(request, spec)
            values = runs[0]["values"]
            pre_state = self._ensure_idle()
            self.precheck(spec)
            job = {
                "id": request.command_id, "capability": request.capability, "program": values.get("program", ""),
                "params": scalars(runs[0]["params"]),
                "context": {"batch_id": request.batch_id, "step_id": request.step_id},
                "state": "starting", "phase": "", "seen_running": False, "quality": "good",
                "pre_state": pre_state, "idle_after_start": spec.get("idle_after_start")
                or self.config.get("idle_after_start") or "unknown",
                "started_at": time.time(), "updated_at": time.time(), "delivered": {}, "telemetry": [], "error": "",
            }
            if runs[0]["well"]:  # 逐孔：每孔一次运行，先跑第一孔
                job["wells"] = [{"well": run["well"], "params": scalars(run["params"]), "values": run["values"],
                                 "state": "pending"} for run in runs]
                job["wells"][0]["state"] = "starting"
                job["well_index"] = 0
                job["run_id"] = values["command_id"]
            # 先落盘再动设备：执行器在这之后任何时刻重启，都知道这条指令可能已经发给设备
            self.journal.put(job, active=True)
            self.journal.save()
            try:
                result = self.start_job(job, spec, values)
            except AdapterUnreachable as exc:
                job["state"] = "unconfirmed"
                job["unconfirmed"] = True
                job["error"] = f"启动命令已发出但没有拿到确认：{exc}"
                job["updated_at"] = time.time()
                self.journal.put(job)
                self._save_quietly()
                raise
            except AdapterError as exc:
                job["state"] = "rejected"
                job["error"] = str(exc)
                job["code"] = exc.code
                job["updated_at"] = time.time()
                self.journal.put(job, active=False)
                self.journal.save()
                raise
            except Exception as exc:
                job["state"] = "unconfirmed"
                job["unconfirmed"] = True
                job["error"] = f"驱动内部错误：{exc}"
                self.journal.put(job)
                self._save_quietly()
                raise AdapterIndeterminate(f"驱动内部错误（{exc.__class__.__name__}），启动命令可能已发出") from exc
            try:
                if isinstance(result, dict):  # 即时动作（称重、读码）：结果随启动命令一起回来
                    job["delivered"] = dict(result.get("delivered") or {})
                    job["actuals"] = dict(result.get("actuals") or {})
                    job["seen_running"] = True
                    self._finish(job, "failed" if result.get("error") else "done", result.get("error", ""))
                else:
                    job["state"] = "accepted"
                    job["updated_at"] = time.time()
                    self.journal.put(job)
                self.journal.save()
            except AdapterError as exc:
                # 设备已经确认启动，台账却写不进去：结论只能是未知，不能报「设备没动」
                raise AdapterIndeterminate(f"设备已确认启动，但作业台账没有记下：{exc}") from exc
            return self._receipt(job, request.command_id)

    def _ensure_idle(self) -> str:
        """在途作业先按设备状态推进；设备仍在运行就明确拒绝，停在结束状态就先复位。返回启动前的设备状态。"""
        current = self.journal.current()
        if current is not None and current["state"] not in TERMINAL:
            self._poll(current)
        if self.SYNCHRONOUS:
            return "idle"
        state = self.device_state()
        if state in {"running", "held"}:
            busy = current["id"][:8] if current is not None and current["state"] not in TERMINAL else "未登记的作业"
            raise AdapterError(f"设备忙（DeviceBusy）：正在执行 {busy}，未接受新作业", code="DeviceBusy")
        if state in {"done", "failed"} and self.acknowledge():
            state = self.device_state()
        return state

    def _held_for(self, request: CommandRequest) -> dict | None:
        current = self.journal.current()
        if current is None or current["state"] != "running" or current.get("phase") != "held":
            return None
        context = current.get("context") or {}
        if context.get("batch_id") != request.batch_id or context.get("step_id") != request.step_id:
            return None
        return current

    def _save_quietly(self) -> None:
        """已经要报「结果未知」时再写台账：写不进去也不能把结论改成别的（落盘前的「启动中」记录仍在）。"""
        try:
            self.journal.save()
        except AdapterError:
            pass

    def _saved(self, action: str) -> None:
        """设备已经执行了动作之后再写台账：写不进去只能报结果未知，不能说「设备没动」。"""
        try:
            self.journal.save()
        except AdapterError as exc:
            raise AdapterIndeterminate(f"{action}已发给设备，但作业台账没有记下：{exc}") from exc

    def _resume(self, job: dict, request: CommandRequest) -> CommandResult:
        self.resume_job(job)
        job["phase"] = ""
        job["updated_at"] = time.time()
        self.journal.alias(request.command_id, job["id"])
        self.journal.put(job)
        self._saved("恢复")
        return self._receipt(job, request.command_id)

    def query(self, command_id: str) -> CommandResult | None:
        if not self.contract.supports_query:
            return None
        with self._lock:
            job = self.journal.find(command_id)
            if job is None:
                return None
            if job.get("control"):
                return self._control_receipt(command_id, job.get("error") or "已执行")
            if job["state"] == "unconfirmed" and not job.get("handle") and self.lookup(job):
                job["updated_at"] = time.time()
                self.journal.put(job)
                self.journal.save()
            if job["state"] not in TERMINAL:
                self._poll(job)
            return self._receipt(job, command_id)

    def _control(self, request: CommandRequest, kind: str, action) -> CommandResult:
        with self._lock:
            existing = self.journal.find(request.command_id)
            if existing is not None and existing.get("control"):
                return self._control_receipt(request.command_id, existing.get("error") or "已执行")
            target = self.journal.find(request.target_command_id) if request.target_command_id else self.journal.current()
            note = action(target)
            self.journal.put({
                "id": request.command_id, "control": kind, "state": "done", "error": note,
                "updated_at": time.time(), "started_at": time.time(),
            })
            self._saved({"hold": "保持", "abort": "终止"}.get(kind, kind))
            return self._control_receipt(request.command_id, note)

    def hold(self, request: CommandRequest) -> CommandResult:
        if not self.contract.supports_hold:
            raise AdapterError("设备声明不支持保持", code="NotSupported")

        def act(target):
            if target is None or target["state"] not in {"accepted", "running"} or target.get("phase") == "held":
                raise AdapterError(f"没有可保持的在途作业 {request.target_command_id[:8] or '（未指定）'}")
            self.hold_job(target)
            target["phase"] = "held"
            target["state"] = "running"
            target["updated_at"] = time.time()
            self.journal.put(target)
            return f"作业 {target['id'][:8]} 已保持"

        return self._control(request, "hold", act)

    def abort(self, request: CommandRequest) -> CommandResult:
        if not self.contract.supports_abort:
            raise AdapterError("设备声明不支持终止", code="NotSupported")

        def act(target):
            self.abort_job(target)
            if target is not None and target["state"] not in TERMINAL:
                try:
                    self._finish(target, "aborted", f"被 {request.command_id[:8]} 终止")
                except AdapterUnreachable:
                    target["state"] = "aborted"
                    target["error"] = f"被 {request.command_id[:8]} 终止（实测值未读到）"
                    target["updated_at"] = time.time()
                    self.journal.put(target, active=False)
            return "设备已终止并处于安全状态"

        return self._control(request, "abort", act)
