"""三组设备特性的实现：把 SiLA 调用转给协议插件，把插件的结论译成契约里的回执与错误码。

译法（`devices/contracts/sila2/README.md`「错误与 ILCS 的处理」）：
- 插件明确失败（`AdapterError`）→ 按它的 `code` 报对应的定义错误，设备没动；
- 提交时连不上设备：作业台账里还没有这条指令 → `DeviceUnreachable`（设备没动）；台账里有了 → 可能已经动作，
  报未定义错误，ILCS 按结果未知处理；
- 查询时连不上 → `DeviceUnreachable`（现在问不到）；有响应但无法确认 → 未定义错误；
- 写点位写出去却没拿到结论 → `WriteUnconfirmed`。
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from pathlib import Path
import threading
from types import SimpleNamespace

from sila2.framework import Feature
from sila2.framework.errors.defined_execution_error import DefinedExecutionError
from sila2.server import FeatureImplementationBase

from . import __version__
from .plugins.base import (
    AdapterError, AdapterIndeterminate, AdapterUnreachable, CommandRequest, CommandResult,
)
from .plugins.jobs import TERMINAL
from .site import DeviceEntry, SiteError
from .values import from_any, to_any, value_type
from .writes import WriteJournal

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts" / "sila2"
DEVICE_INFO = Feature((CONTRACTS / "DeviceInfo.sila.xml").read_text(encoding="utf-8"))
POINT_ACCESS = Feature((CONTRACTS / "PointAccess.sila.xml").read_text(encoding="utf-8"))
TASK_EXECUTION = Feature((CONTRACTS / "TaskExecution.sila.xml").read_text(encoding="utf-8"))
TASK_CODES = {"InvalidParameters", "Interlocked", "DeviceBusy", "NotSupported", "DeviceUnreachable"}
WRITE_CODES = {"UnknownPoint", "NotWritable", "ControlPoint", "OutOfRange", "InvalidValue", "DeviceBusy",
               "WriteRejected", "DeviceUnreachable"}
DEVICE_STATES = {"idle", "running", "held", "done", "failed"}
JSON_TYPES = {"Real": "number", "Integer": "integer", "Boolean": "boolean", "String": "string"}


def _defined(feature: Feature, code: str, message: str) -> DefinedExecutionError:
    return DefinedExecutionError(feature.defined_execution_errors[code], message)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class DeviceRuntime:
    """一台设备：插件实例、点位写入台账，以及「在途作业时不许换配置」的启动检查。"""

    def __init__(self, entry: DeviceEntry, plugin_class, state_dir: Path):
        self.entry = entry
        record = SimpleNamespace(
            station_id=entry.key, config=entry.config, credential_ref=entry.credential_ref, protocol="", version="",
            note=entry.note, supports_hold=entry.supports["hold"], supports_abort=entry.supports["abort"],
            supports_query=entry.supports["query"], supports_dedup=entry.supports["dedup"],
        )
        try:
            self.adapter = plugin_class(record, entry.key)
        except AdapterError as exc:
            raise SiteError(f"设备 {entry.key} 的配置不对：{exc}") from exc
        self._guard_config_change(state_dir)
        self.writes = WriteJournal(state_dir / f"{entry.key}.writes.json")
        self.tasks = bool(self.adapter.tasks)
        self.points = bool(self.adapter.point_specs())
        self.lock = threading.RLock()

    def _guard_config_change(self, state_dir: Path) -> None:
        """设备上还有没结束的作业时，配置摘要不许变：新配置可能连的是另一个地址、按另一套状态码判结论。

        这里只检查。摘要等设备服务真正按这份配置起来了才记（`record_digest`）：`--check` 不能改状态，否则检查过新配置、
        旧进程又开了作业，换配置重启时就核对不出来了。"""
        self.marker = state_dir / f"{self.entry.key}.digest"
        previous = self.marker.read_text(encoding="utf-8").strip() if self.marker.exists() else ""
        current = self.adapter.journal.current()
        if previous and previous != self.entry.digest and current is not None and current["state"] not in TERMINAL:
            raise SiteError(
                f"设备 {self.entry.key} 还有没结束的作业 {current['id']}，配置摘要却变了（{previous} → {self.entry.digest}）："
                "等作业结束，或现场核对后把台账里的这条作业处理掉，再换配置"
            )

    def record_digest(self) -> None:
        """设备服务按这份配置上线了：记下摘要，下次启动据此判断配置变没变。"""
        self.marker.write_text(self.entry.digest + "\n", encoding="utf-8")

    # ---------- 身份与状态 ----------

    def maps_identity(self) -> bool:
        """插件配置里映射了设备编号（点表的 identity，或 REST 身份请求的 fields）：设备能报身份。"""
        spec = self.entry.config.get("identity") or {}
        fields = spec.get("fields") if isinstance(spec.get("fields"), dict) else spec
        return any(fields.get(key) for key in ("device_id", "serial"))

    def identity(self) -> dict:
        """设备能报身份就以设备为准，报空就是缺失（接错了设备、PLC 没配编号），不拿配置顶替——顶替了就核对不出接错的设备。
        映射里没有编号的设备才按配置登记的编号（IdentitySource = config）。"""
        try:
            raw = self.adapter.identity()
        except (AdapterError, AdapterUnreachable) as exc:
            raise _defined(DEVICE_INFO, "DeviceUnreachable", str(exc)) from exc
        device_id = str(raw.get("device_id") or raw.get("serial") or "")
        reported = bool(device_id) or self.maps_identity()
        return {
            **raw, "device_id": device_id if reported else self.entry.device_id,
            "source": "device" if reported else "config",
            "simulator": bool(raw.get("simulator")) or self.entry.simulator,
        }

    def device_state(self) -> str:
        if not self.tasks:
            return "idle"
        with self.adapter._lock:
            try:
                state = self.adapter.device_state()
            except AdapterIndeterminate:
                return "unknown"
            except (AdapterError, AdapterUnreachable) as exc:
                raise _defined(DEVICE_INFO, "DeviceUnreachable", str(exc)) from exc
        if state == "accepted":
            return "running"
        return state if state in DEVICE_STATES else "unknown"

    def active_command(self) -> str:
        current = self.adapter.journal.current()
        return current["id"] if current is not None and current["state"] not in TERMINAL else ""


class DeviceInfoImpl(FeatureImplementationBase):
    def __init__(self, parent_server, runtime: DeviceRuntime):
        super().__init__(parent_server)
        self.runtime = runtime

    def get_Identity(self, *, metadata):
        identity = self.runtime.identity()
        return {
            "DeviceId": identity["device_id"], "IdentitySource": identity["source"],
            "Vendor": str(identity.get("vendor") or ""), "Model": str(identity.get("model") or ""),
            "SerialNumber": str(identity.get("serial") or identity["device_id"]),
            "Firmware": str(identity.get("firmware") or ""), "Simulator": identity["simulator"],
        }

    def get_Status(self, *, metadata):
        identity = self.runtime.identity()
        return {
            "State": self.runtime.device_state(), "ActiveCommandId": self.runtime.active_command(),
            "Interlock": bool(identity.get("interlock")), "AcceptsCommands": bool(identity.get("accepts_commands", True)),
            "ObservedAt": _now(),
        }

    def get_Driver(self, *, metadata):
        entry = self.runtime.entry
        return {
            "Plugin": entry.plugin, "PluginVersion": __version__, "HostVersion": __version__,
            "ConfigVersion": entry.config_version, "ConfigDigest": entry.digest,
            "OfflineAfterSeconds": float(getattr(self.runtime.adapter, "heartbeat_stale", 0) or 0),
        }


class PointAccessImpl(FeatureImplementationBase):
    def __init__(self, parent_server, runtime: DeviceRuntime):
        super().__init__(parent_server)
        self.runtime = runtime

    def _catalog(self) -> list[dict]:
        specs = self.runtime.adapter.point_specs()
        return [{**row, "value_type": value_type(specs[row["name"]])} for row in self.runtime.adapter.point_catalog()]

    def get_Points(self, *, metadata):
        return [{
            "Name": row["name"], "Label": row["label"], "Unit": row["unit"], "ValueType": row["value_type"],
            "Writable": row["writable"] and not row["control"],
            "Minimum": [] if row["min"] is None else [float(row["min"])],
            "Maximum": [] if row["max"] is None else [float(row["max"])], "Control": row["control"],
        } for row in self._catalog()]

    def ReadPoints(self, Names, *, metadata):
        names = list(Names)
        known = set(self.runtime.adapter.point_specs())
        unknown = [name for name in names if name not in known]
        if unknown:
            raise _defined(POINT_ACCESS, "UnknownPoint", f"点 {'、'.join(unknown)} 没有在点表里登记")
        return [{
            "Name": row["name"], "Value": to_any(row["value"]), "Quality": "bad" if row["error"] else "good",
            "ObservedAt": _now(), "Error": row["error"],
        } for row in self.runtime.adapter.read_points(names or None)]

    def WritePoint(self, RequestId, Name, Value, *, metadata):
        value = from_any(Value)
        writes = self.runtime.writes
        with writes.lock:
            previous = writes.find(RequestId)
            if previous is not None:
                if previous["name"] != Name or previous["value"] != value:
                    raise _defined(POINT_ACCESS, "RequestConflict",
                                   f"请求号 {RequestId} 写过 {previous['name']} = {previous['value']!r}，这次是 {Name} = {value!r}")
                return self._replay(previous)
            entry = {"name": Name, "value": value, **self._write(Name, value)}
            writes.record(RequestId, entry)
            return self._replay(entry)

    def _write(self, name: str, value) -> dict:
        """写一次，返回要记进台账的结论（写成了 / 定义错误码与原因）。"""
        catalog = {row["name"]: row for row in self._catalog()}
        row = catalog.get(name)
        if row is not None and not self._type_matches(row["value_type"], value):
            return {"code": "InvalidValue", "error": f"点 {name} 的值是 {row['value_type']}，收到 {value!r}"}
        runtime = self.runtime
        try:
            if runtime.active_command() or runtime.device_state() in {"running", "held"}:
                return {"code": "DeviceBusy", "error": "设备上有作业在运行或保持中，没有写入"}
        except DefinedExecutionError as exc:
            return {"code": "DeviceUnreachable", "error": f"读不到设备状态（{exc.message}），没有写入"}
        try:
            result = runtime.adapter.write_point_manually(name, value)
        except AdapterError as exc:
            return {"code": exc.code if exc.code in WRITE_CODES else "WriteRejected", "error": str(exc)}
        except Exception as exc:  # 写出去没拿到结论、回读失败、插件内部异常：都按设备上的值可能已经变了
            return {"code": "WriteUnconfirmed", "error": str(exc) or exc.__class__.__name__}
        return {"before": result["before"], "after": result["after"], "matches": bool(result["matches"]),
                "observed_at": _now().isoformat()}

    @staticmethod
    def _type_matches(kind: str, value) -> bool:
        if kind in {"Real", "Integer"}:
            return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        if kind == "Boolean":
            return isinstance(value, bool)
        return isinstance(value, str)

    @staticmethod
    def _replay(entry: dict):
        if entry.get("code"):
            raise _defined(POINT_ACCESS, entry["code"], entry.get("error") or entry["code"])
        return {
            "Before": to_any(entry["before"]), "After": to_any(entry["after"]), "Matches": entry["matches"],
            "ObservedAt": datetime.fromisoformat(entry["observed_at"]),
        }


class TaskExecutionImpl(FeatureImplementationBase):
    def __init__(self, parent_server, runtime: DeviceRuntime):
        super().__init__(parent_server)
        self.runtime = runtime

    @property
    def adapter(self):
        return self.runtime.adapter

    @staticmethod
    def _receipt(result: CommandResult) -> str:
        moment = result.device_ts.replace(tzinfo=timezone.utc) if result.device_ts else _now()
        return json.dumps({
            "command_id": result.command_id, "state": result.state, "device_ts": moment.isoformat(timespec="seconds"),
            "quality": result.quality, "delivered": result.delivered,
            "telemetry": [{"metric": metric, "value": value, "setpoint": setpoint}
                          for metric, value, setpoint in result.telemetry],
            "error": result.error,
        }, ensure_ascii=False)

    @staticmethod
    def _refusal(exc: AdapterError) -> DefinedExecutionError:
        return _defined(TASK_EXECUTION, exc.code if exc.code in TASK_CODES else "InvalidParameters", str(exc))

    def SubmitTask(self, CommandId, TaskType, Capability, ParametersJson, ContextJson, *, metadata):
        try:
            params = json.loads(ParametersJson or "{}")
            context = json.loads(ContextJson or "{}")
        except ValueError as exc:
            raise _defined(TASK_EXECUTION, "InvalidParameters", f"参数或上下文不是 JSON：{exc}") from exc
        if not isinstance(params, dict) or not isinstance(context, dict):
            raise _defined(TASK_EXECUTION, "InvalidParameters", "参数与上下文都必须是 JSON 对象")
        request = CommandRequest(
            command_id=CommandId, station_id=str(context.get("station_id") or self.runtime.entry.key),
            capability=Capability, params=params, type=TaskType or "dispatch",
            batch_id=str(context.get("batch_id") or ""), step_index=int(context.get("step_index") or 0),
            step_id=str(context.get("step_id") or ""), target_command_id=str(context.get("target_command_id") or ""),
            method=dict(context.get("method") or {}), material=dict(context.get("material") or {}),
        )
        try:
            return self._receipt(self.adapter.submit(request))
        except AdapterError as exc:
            raise self._refusal(exc) from exc
        except AdapterUnreachable as exc:  # 含 AdapterIndeterminate
            if self.adapter.journal.find(CommandId) is None:
                # 台账里还没有这条作业：启动前就失败了，设备没动
                raise _defined(TASK_EXECUTION, "DeviceUnreachable", f"没有下发：{exc}") from exc
            raise RuntimeError(f"结果未知：{exc}") from exc

    def QueryTask(self, CommandId, *, metadata):
        try:
            result = self.adapter.query(CommandId)
        except AdapterIndeterminate as exc:
            raise RuntimeError(f"设备有响应但无法确认：{exc}") from exc
        except AdapterUnreachable as exc:
            raise _defined(TASK_EXECUTION, "DeviceUnreachable", str(exc)) from exc
        if result is None:
            return json.dumps({"command_id": CommandId, "state": "not_found", "device_ts": _now().isoformat(timespec="seconds"),
                               "quality": "good", "delivered": {}, "telemetry": [], "error": ""})
        return self._receipt(result)

    def _control(self, kind: str, CommandId: str, TargetCommandId: str) -> str:
        request = CommandRequest(
            command_id=CommandId, station_id=self.runtime.entry.key, capability="", params={}, type=kind,
            batch_id="", step_index=0, target_command_id=TargetCommandId,
        )
        try:
            return self._receipt(getattr(self.adapter, kind)(request))
        except AdapterError as exc:
            raise self._refusal(exc) from exc
        except AdapterUnreachable as exc:  # 保持 / 终止信号可能已经发出
            raise RuntimeError(f"结果未知：{exc}") from exc

    def HoldTask(self, CommandId, TargetCommandId, *, metadata):
        return self._control("hold", CommandId, TargetCommandId)

    def AbortTask(self, CommandId, TargetCommandId, *, metadata):
        return self._control("abort", CommandId, TargetCommandId)

    def get_DeviceIdentity(self, *, metadata):
        identity = self.runtime.identity()
        identity.pop("source", None)
        return json.dumps({**identity, "device_ts": _now().isoformat(timespec="seconds")}, ensure_ascii=False, default=str)

    def get_TaskSupport(self, *, metadata):
        entry, adapter = self.runtime.entry, self.adapter
        return {
            "Capabilities": [
                {"Capability": capability, "ParametersSchema": json.dumps(self._schema(spec), ensure_ascii=False),
                 "Programs": self._programs(capability, spec)}
                for capability, spec in (entry.config.get("capabilities") or {}).items()
            ],
            "SupportsHold": entry.supports["hold"], "SupportsAbort": entry.supports["abort"],
            "SupportsQuery": entry.supports["query"], "SupportsDedup": entry.supports["dedup"],
            "Handoff": getattr(adapter, "handoff", "") or "sync",
        }

    def _schema(self, spec: dict) -> dict:
        """能力参数的 JSON Schema：写入点有单位、范围就带上，选项型参数列出可选值。"""
        accepted = self.adapter.accepted_params(spec) or set()
        writes = spec.get("write") or {}
        points = self.adapter.point_specs()
        properties = {}
        for name in sorted(accepted):
            item = writes.get(name)
            point = points.get(item if isinstance(item, str) else (item or {}).get("point") if isinstance(item, dict) else None)
            prop: dict = {}
            if isinstance(item, dict) and isinstance(item.get("map"), dict):
                prop["enum"] = list(item["map"])
            elif isinstance(point, dict):
                prop["type"] = JSON_TYPES[value_type(point)]
                for key, target in (("unit", "unit"), ("min", "minimum"), ("max", "maximum")):
                    if point.get(key) is not None:
                        prop[target] = point[key]
            properties[name] = prop
        defaults = spec.get("defaults") or {}
        required = sorted(name for name in writes if name not in defaults)
        return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}

    def _programs(self, capability: str, spec: dict) -> list[dict]:
        recipe = spec.get("recipe") or {}
        if isinstance(recipe.get("map"), dict):
            return [{"Program": str(program), "Name": str(program)} for program in recipe["map"]]
        rows = []
        for item in self.runtime.entry.config.get("methods") or []:
            row = {"program": item} if isinstance(item, str) else item if isinstance(item, dict) else {}
            if row.get("program") and row.get("capability", capability) == capability:
                rows.append({"Program": str(row["program"]), "Name": str(row.get("name") or row["program"])})
        return rows
