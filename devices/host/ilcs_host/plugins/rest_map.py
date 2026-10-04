"""通用 REST 接口映射驱动（`rest_map_v1`）。

面向「设备或调度系统有自己的 REST API，但不是 ILCS 网关契约」的场景：AGV 车队调度、视觉系统、带网页接口的
仪器。每项能力写成一个请求模板，驱动把设备返回的任务号（handle）记进作业台账，之后按它查状态、保持、终止：

```json
{
  "base_url": "https://fleet.lab.internal/api/v2.0.0",
  "headers": {"Accept-Language": "en_US"},
  "identity": {"method": "GET", "path": "/status",
               "fields": {"device_id": "robot_name", "model": "model", "firmware": "software_version"},
               "interlock": {"field": "state_text", "values": ["EmergencyStop", "Error"]}},
  "capabilities": {"cap.transfer": {
      "method": "POST", "path": "/mission_queue", "handle": "id",
      "body": {"mission_id": "{mission}", "message": "ILCS {command_id}",
               "parameters": [{"id": "From", "value": "{from_position}"}, {"id": "To", "value": "{to_position}"}]},
      "defaults": {"mission": "<任务模板 GUID>"}}},
  "positions": {"HOTEL-01/S01": "<站点 GUID>", "ST-05/N1": "<站点 GUID>"},
  "status": {"method": "GET", "path": "/mission_queue/{handle}", "field": "state",
             "states": {"Pending": "accepted", "Executing": "running", "Paused": "held", "Done": "done", "Aborted": "failed"}},
  "lookup": {"path": "/mission_queue", "detail_path": "/mission_queue/{id}", "id_field": "id",
             "match_field": "message", "match": "ILCS {command_id}", "recent": 20},
  "hold": {"method": "PUT", "path": "/status", "body": {"state_id": 4}},
  "resume": {"method": "PUT", "path": "/status", "body": {"state_id": 3}},
  "abort": {"method": "DELETE", "path": "/mission_queue/{handle}"}
}
```

- 模板里的 `{参数}` 取指令参数（转运指令的 `{from.location_id}`、`{to.location_id}` 也能用）；配置了 `positions` 时
  `{from_position}` / `{to_position}` 按 ILCS 位置编号查表，查不到就明确拒绝，不把 ILCS 编号猜成车队站点。
- 状态值 `accepted` 表示「排队中、还没开始」：排队期间不按启动超时判结果未知。
- 请求里带上 ILCS 指令号（如 `message`）并配置 `lookup` 后，启动请求没拿到应答的作业可以按指令号在设备侧找回。
- HTTP 分类与 `http_json_v1` 相同；凭据用 `credential_ref`（纯 token 当 Bearer，或 `{"headers": {...}}`）。

**只读写点位**：不参与自动流程的设备只配 `points`（可以不配 `capabilities` / `status`，没有 `identity` 时读第一个点
判断在线）。每个点一个读请求，`writable: true` 的点再配一个写请求，`{value}` 是要写的值（整串占位时保持数 / 布尔类型）：

```json
"points": {
  "temperature": {"path": "/temperature", "field": "value", "unit": "℃"},
  "setpoint": {"path": "/setpoint", "field": "value", "writable": true, "min": 0, "max": 80,
               "write": {"method": "PUT", "path": "/setpoint", "body": {"value": "{value}"}}}
}
```
"""
from __future__ import annotations

from urllib.parse import quote

from .base import AdapterError, AdapterIndeterminate
from .http_client import HttpTransport
from .jobs import BUILTINS, MappedJobAdapter, check_point_meta, render, render_value, template_fields

DRIVER = "rest_map_v1"
METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
STATE_NAMES = {"idle", "accepted", "running", "held", "done", "failed"}


def field(value, path: str):
    """按 `a.b.0.c` 取嵌套字段；取不到返回 None。"""
    current = value
    for part in str(path).split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return None
    return current


class RestMapAdapter(MappedJobAdapter):
    DRIVER = DRIVER
    PROTOCOL = "REST 接口映射"
    NOTE = "设备自有 REST 接口映射"

    def __init__(self, record, journal_key: str = ""):
        super().__init__(record, journal_key)
        self.transport = HttpTransport(self.config, self.credential_ref, driver=DRIVER, label="设备接口")
        self.points = self.config.get("points") or {}
        if not isinstance(self.points, dict):
            raise AdapterError("points 必须是 {点名: {path, field, …}}")
        for name, spec in self.points.items():
            self._check_point(name, spec)
        if not self.tasks and not self.points:
            raise AdapterError("rest_map_v1 至少要配点表（points，读写点位）或能力映射（capabilities，参与自动流程）")
        # 只读写点位的设备可以不配状态请求；配了能力映射（要参与自动流程）就必须能查到作业状态
        self.status = self._request("status", required=self.tasks)
        if self.status:
            states = self.status.get("states") or {}
            if not isinstance(states, dict) or not states or not set(states.values()) <= STATE_NAMES:
                raise AdapterError("status.states 必须把状态值映射到 idle / accepted / running / held / done / failed")
            if not self.status.get("field"):
                raise AdapterError("status.field 必填：状态在响应的哪个字段")
        self.positions = self.config.get("positions") or {}
        if not isinstance(self.positions, dict):
            raise AdapterError("positions 必须是 {ILCS 位置编号: 设备站点编号}")
        for capability, spec in (self.config.get("capabilities") or {}).items():
            self._check_request(spec, f"capabilities.{capability}")
            for name, source in (spec.get("actuals") or {}).items():
                # 实测值：字符串是状态响应里的字段路径；{"point": 点名} 按点表读这个点（状态响应里没有实测值的设备）
                if isinstance(source, dict) and source.get("point") not in self.points:
                    raise AdapterError(f"capabilities.{capability}.actuals.{name} 引用的点 {source.get('point')} 不在点表里")
        # 没有身份请求就没法判断在线：健康检查不能返回假在线（只读写点位的设备读第一个点代替）
        self._request("identity", required=not self.points)
        for name in ("busy", "hold", "resume", "abort", "lookup"):
            self._request(name)

    def _check_request(self, spec, label: str) -> dict:
        if not isinstance(spec, dict) or not str(spec.get("path") or "").startswith("/") or "://" in str(spec.get("path")):
            raise AdapterError(f"{label}.path 必须是 base_url 下以 / 开头的相对路径")
        if str(spec.get("method") or "GET").upper() not in METHODS:
            raise AdapterError(f"{label}.method 只能是 {' / '.join(sorted(METHODS))}")
        return spec

    def _request(self, name: str, *, required: bool = False) -> dict:
        spec = self.config.get(name)
        if spec in (None, {}):
            if required:
                raise AdapterError(f"rest_map_v1 必须配置 {name}")
            return {}
        return self._check_request(spec, name)

    def _check_point(self, name: str, spec) -> None:
        if not isinstance(spec, dict):
            raise AdapterError(f"points.{name} 必须是 {{path, field, …}}")
        self._check_request({"method": "GET", **spec}, f"points.{name}")
        check_point_meta(name, spec, set())
        write = spec.get("write")
        if spec.get("writable") and not isinstance(write, dict):
            raise AdapterError(f"points.{name} 声明了可写，要配写请求 write：{{method, path, body}}，body 里用 {{value}}")
        if write is not None:
            self._check_request(write, f"points.{name}.write")
            if str(write.get("method") or "PUT").upper() == "GET":
                raise AdapterError(f"points.{name}.write 不能是 GET")

    def close(self) -> None:
        pass

    # ---------- 点位 ----------

    def point_specs(self) -> dict:
        return dict(self.points)

    def read_point_value(self, name: str):
        spec = self.points[name]
        response = self._send({"method": spec.get("method") or "GET", "path": spec["path"]}, {})
        if not spec.get("field"):
            return response
        value = field(response, spec["field"])
        if value is None:
            raise AdapterIndeterminate(f"点 {name} 的响应里没有字段 {spec['field']}")
        return value

    def write_point_value(self, name: str, value) -> None:
        write = dict(self.points[name]["write"])
        write.setdefault("method", "PUT")
        self._send(write, {"value": value})

    # ---------- 请求 ----------

    def _send(self, spec: dict, values: dict, *, allow_not_found: bool = False):
        method, path, body = self._compose(spec, values)
        return self.transport.request(method, path, body, allow_not_found=allow_not_found, allow_empty=True, detail=True)

    @staticmethod
    def _compose(spec: dict, values: dict) -> tuple[str, str, object]:
        method = str(spec.get("method") or "GET").upper()
        # 路径里的值（设备返回的任务号、指令号）按路径段转义：一个带 / 或 .. 的任务号不能把请求改到别的接口上
        escaped = {key: quote(value, safe="") if isinstance(value, str) else value for key, value in values.items()}
        path = render(str(spec["path"]), escaped)
        body = render_value(spec["body"], values) if "body" in spec else None
        return method, path, body

    def values_for(self, request, spec: dict, params: dict | None = None) -> dict:
        values = super().values_for(request, spec, params)
        for end in ("from", "to"):
            location = values.get(f"{end}.location_id")
            if location is None:
                continue
            if self.positions:
                if location not in self.positions:
                    raise AdapterError(f"位置 {location} 没有在 positions 里登记设备站点，转运没有下发")
                values[f"{end}_position"] = self.positions[location]
            else:
                values[f"{end}_position"] = location
        return values

    # ---------- 钩子 ----------

    def accepted_params(self, spec: dict) -> set[str]:
        used = template_fields(spec.get("body")) | template_fields(str(spec.get("path") or ""))
        return (used - BUILTINS) | set(spec.get("accept") or [])

    def read_identity(self) -> dict:
        spec = self.config.get("identity") or {}
        identity: dict = {"vendor": self.config.get("vendor", ""), "model": self.config.get("model", "")}
        if not spec:
            # 没有身份请求：读第一个点证明接口在回话（构造时已保证有点表）
            self.read_point_value(next(iter(self.points)))
            return identity
        response = self._send(spec, {})
        for key, path in (spec.get("fields") or {}).items():
            value = field(response, path)
            if value is not None:
                identity[key] = str(value)
        for key, flag in (("interlock", "interlock"), ("ready", "accepts_commands")):
            check = spec.get(key) or {}
            if check.get("field"):
                identity[flag] = field(response, check["field"]) in (check.get("values") or [])
        return identity

    def precheck(self, spec: dict) -> None:
        if not (self.config.get("identity") or {}):
            return
        identity = self.read_identity()
        if identity.get("interlock"):
            raise AdapterError("设备处于急停 / 故障（Interlocked），没有下发", code="Interlocked")
        if identity.get("accepts_commands") is False:
            raise AdapterError("设备未就绪，没有下发", code="DeviceBusy")

    def device_state(self) -> str:
        spec = self.config.get("busy") or {}
        if not spec:
            return "idle"  # 调度系统自己排队：设备级不判忙
        value = field(self._send(spec, {}), spec.get("field", ""))
        return "running" if value in (spec.get("values") or []) else "idle"

    def check_start(self, spec: dict, values: dict) -> None:
        self._compose(spec, values)  # 路径与请求体先拼一遍：缺参数在发请求之前就拒绝

    def start_job(self, job: dict, spec: dict, values: dict) -> None:
        response = self._send(spec, values)
        handle = field(response, spec.get("handle", "")) if spec.get("handle") else None
        if handle in (None, ""):
            if spec.get("handle"):
                raise AdapterIndeterminate(f"设备接受了请求，但响应里没有任务号 {spec['handle']}，无法跟踪")
            handle = job.get("run_id") or job["id"]  # 逐孔时每孔一个运行号
        job["handle"] = str(handle)
        job["delivered"] = {"remote_id": str(handle)}

    def read_status(self, job: dict) -> tuple[str, str]:
        if not job.get("handle"):
            return self.device_state(), ""
        response = self._send(self.status, {"handle": job["handle"], "command_id": job.get("run_id") or job["id"]},
                              allow_not_found=True)
        if response is None:
            raise AdapterIndeterminate(f"设备侧查不到任务 {job['handle']}")
        value = field(response, self.status["field"])
        state = (self.status.get("states") or {}).get(str(value))
        if state is None:
            raise AdapterIndeterminate(f"任务状态 {value!r} 没有在 status.states 里映射")
        detail = str(field(response, self.status.get("error_field", "")) or "") if state == "failed" else ""
        return state, detail

    def read_actuals(self, job: dict, spec: dict) -> dict:
        mapping = spec.get("actuals") or {}
        if not mapping:
            return {}
        response = None
        actuals = {}
        for name, path in mapping.items():
            if isinstance(path, dict):  # {"point": 点名}：按点表读
                value = self.read_point_value(path["point"])
            else:
                if not job.get("handle"):
                    continue
                if response is None:
                    response = self._send(self.status, {"handle": job["handle"], "command_id": job.get("run_id") or job["id"]})
                value = field(response, path)
            try:
                actuals[name] = float(value)
            except (TypeError, ValueError):
                continue
        return actuals

    def lookup(self, job: dict) -> bool:
        spec = self.config.get("lookup") or {}
        if not spec:
            return False
        values = {"command_id": job.get("run_id") or job["id"]}
        wanted = render(str(spec.get("match") or "{command_id}"), values)
        items = self._send(spec, values)
        if not isinstance(items, list):
            raise AdapterIndeterminate("lookup 接口必须返回数组")
        id_field = spec.get("id_field", "id")
        for item in list(reversed(items))[: int(spec.get("recent") or 20)]:
            identifier = field(item, id_field)
            if identifier is None:
                continue
            detail = item
            if spec.get("detail_path"):
                detail = self._send({"method": "GET", "path": spec["detail_path"]}, {"id": identifier},
                                    allow_not_found=True) or {}
            if str(field(detail, spec.get("match_field", "message")) or "") == wanted:
                job["handle"] = str(identifier)
                job["unconfirmed"] = False
                job["state"] = "accepted"
                job["delivered"] = {"remote_id": str(identifier)}
                return True
        return False

    def _send_control(self, name: str, job: dict | None) -> None:
        spec = self.config.get(name) or {}
        if not spec:
            raise AdapterError(f"映射里没有配置 {name} 请求", code="NotSupported")
        self._send(spec, {"handle": (job or {}).get("handle", ""), "command_id": (job or {}).get("id", "")})

    def hold_job(self, job: dict) -> None:
        self._send_control("hold", job)

    def resume_job(self, job: dict) -> None:
        self._send_control("resume", job)

    def abort_job(self, job: dict | None) -> None:
        self._send_control("abort", job)
