"""组合工位驱动（`composite_v1`）：一个工位由几台接口不同的仪器组成，按能力分派到各自的驱动。

例如「真空干燥与称重站」= 真空干燥箱（串口命令）+ 分析天平（MT-SICS）：

```json
{"routes": [
  {"name": "oven", "capabilities": ["cap.vacuum_dry"], "driver": "line_command_v1", "config": {…}},
  {"name": "balance", "capabilities": ["cap.weigh"], "driver": "mt_sics_v1", "config": {…}, "credential_ref": ""}
]}
```

- 每项能力只能落在一条路由上；路由里的驱动是任意已登记的真实驱动（不能再嵌套组合）。
- 子驱动各自校验自己的配置（主机白名单、证书、凭据位置……），各自一份作业台账（`<工位>#<路由名>`）。
- 健康检查要每台仪器都在线才算在线；任何一台联锁即联锁；任何一台自报模拟器即模拟器。
- 按指令号查询先看本进程记住的归属，再逐台问（执行器重启后靠子驱动自己的台账或设备侧查询回答）。
"""
from __future__ import annotations

from types import SimpleNamespace

from .base import AdapterContract, AdapterError, CommandRequest, CommandResult

DRIVER = "composite_v1"


class CompositeAdapter:
    def __init__(self, record):
        from .jobs import MappedJobAdapter
        from .registry import REAL_IMPLEMENTATIONS

        self.station_id = record.station_id
        self.config = dict(record.config or {})
        routes = self.config.get("routes")
        if not isinstance(routes, list) or not routes:
            raise AdapterError("composite_v1 必须配置 routes：[{name, capabilities, driver, config}]")
        self.routes: dict[str, object] = {}
        self.owners: dict[str, str] = {}  # 能力 → 路由
        self._commands: dict[str, str] = {}  # 指令号 → 路由（本进程记住的）
        for route in routes:
            if not isinstance(route, dict):
                raise AdapterError("routes 里每一项都必须是对象")
            name = str(route.get("name") or "")
            if not name or not name.replace("_", "").replace("-", "").isalnum() or name in self.routes:
                raise AdapterError(f"路由名 {name!r} 缺失、含非法字符或重复")
            driver = str(route.get("driver") or "")
            if driver == DRIVER:
                raise AdapterError("组合工位不能嵌套组合工位")
            implementation = REAL_IMPLEMENTATIONS.get(driver)
            if implementation is None:
                raise AdapterError(f"路由 {name} 的驱动 {driver or '（未填）'} 没有登记")
            capabilities = route.get("capabilities") or []
            if not isinstance(capabilities, list) or not capabilities:
                raise AdapterError(f"路由 {name} 至少要承接一项能力")
            for capability in capabilities:
                if capability in self.owners:
                    raise AdapterError(f"能力 {capability} 同时落在路由 {self.owners[capability]} 与 {name} 上")
                self.owners[capability] = name
            sub = SimpleNamespace(
                station_id=record.station_id, protocol=str(route.get("protocol") or driver),
                version=record.version, note=f"{record.station_id} 的 {name}", config=dict(route.get("config") or {}),
                credential_ref=str(route.get("credential_ref") or ""), supports_hold=record.supports_hold,
                supports_abort=record.supports_abort, supports_query=record.supports_query,
                supports_dedup=record.supports_dedup,
            )
            try:
                if issubclass(implementation, MappedJobAdapter):
                    self.routes[name] = implementation(sub, f"{record.station_id}#{name}")
                else:
                    self.routes[name] = implementation(sub)
            except AdapterError as exc:
                raise AdapterError(f"路由 {name}（{driver}）配置无效：{exc}") from exc
        self.contract = AdapterContract(
            kind="real", protocol=record.protocol or "组合工位", version=record.version or "1.0",
            capabilities=tuple(self.owners), supports_hold=bool(record.supports_hold),
            supports_abort=bool(record.supports_abort), supports_query=bool(record.supports_query),
            supports_dedup=bool(record.supports_dedup), note=record.note or "组合工位（按能力分派）",
        )

    def close(self) -> None:
        for adapter in self.routes.values():
            close = getattr(adapter, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:
                    pass

    # ---------- 身份与健康 ----------

    def identity(self) -> dict:
        members, methods, source = {}, [], "config"
        for name, adapter in self.routes.items():
            identity = getattr(adapter, "identity", None)
            raw = identity() if identity is not None else {}
            members[name] = {key: raw.get(key) for key in ("device_id", "model", "vendor", "firmware", "simulator")}
            reported = raw.get("methods")
            if isinstance(reported, list):
                methods.extend(reported)
                source = "device"
            else:
                methods.extend(adapter.config.get("methods") or [])
        return {
            "vendor": " / ".join(sorted({str(m.get("vendor") or "") for m in members.values()} - {""})),
            "model": " + ".join(str(m.get("model") or name) for name, m in members.items()),
            "firmware": "", "members": members, "methods": methods, "methods_source": source,
        }

    def healthcheck(self) -> dict:
        members = {name: adapter.healthcheck() for name, adapter in self.routes.items()}
        return {
            "reachable": True, "driver": DRIVER, "protocol": self.contract.protocol,
            "device_id": "; ".join(f"{name}={health.get('device_id', '')}" for name, health in members.items()),
            "model": " + ".join(str(health.get("model") or name) for name, health in members.items()),
            "simulator": any(health.get("simulator") for health in members.values()),
            "interlock": any(health.get("interlock") for health in members.values()),
            "accepts_commands": all(health.get("accepts_commands", True) for health in members.values()),
            "members": members,
        }

    # ---------- 分派 ----------

    def _route(self, capability: str):
        name = self.owners.get(capability)
        if name is None:
            raise AdapterError(f"能力 {capability} 没有落在组合工位的任何一条路由上，设备没有动作")
        return name, self.routes[name]

    def _owner_of(self, command_id: str):
        name = self._commands.get(command_id)
        if name is not None:
            return name, self.routes[name], None
        for name, adapter in self.routes.items():
            found = adapter.query(command_id)
            if found is not None:
                self._commands[command_id] = name
                return name, adapter, found
        return None, None, None

    def submit(self, request: CommandRequest) -> CommandResult:
        name, adapter = self._route(request.capability)
        self._commands[request.command_id] = name
        return adapter.submit(request)

    def query(self, command_id: str) -> CommandResult | None:
        if not self.contract.supports_query:
            return None
        name, adapter, found = self._owner_of(command_id)
        if adapter is None:
            return None
        return found if found is not None else adapter.query(command_id)

    def _control(self, request: CommandRequest, action: str) -> CommandResult:
        adapter = None
        if request.target_command_id:
            _, adapter, _ = self._owner_of(request.target_command_id)
        if adapter is None:
            _, adapter = self._route(request.capability)
        return getattr(adapter, action)(request)

    def hold(self, request: CommandRequest) -> CommandResult:
        if not self.contract.supports_hold:
            raise AdapterError("设备声明不支持保持")
        return self._control(request, "hold")

    def abort(self, request: CommandRequest) -> CommandResult:
        if not self.contract.supports_abort:
            raise AdapterError("设备声明不支持终止")
        return self._control(request, "abort")
