"""驱动目录：每个内置驱动自己声明「配置长什么样」，界面按它出表单，保存时按它校验。

以前每个驱动的配置模板写死在前端，加一个驱动要改页面；现在驱动在这里登记：

- `fields`：顶层配置项（名称、类型、是否必填、说明），其中 `connection=True` 的是每台设备自己的连接参数
  （地址、端口、证书、设备编号）——设备模板里不写死它们，套用模板时由工位填；
- `template(limits)`：按工位能力极限生成一份起步配置（地址、点位、命令都是占位，要按设备手册改）；
- `supports`：这类驱动缺省声明支持的保持 / 终止 / 查询 / 去重。

`validate_config` 在保存前把配置过一遍：先按字段说明查缺项与类型，再真的构造一次驱动实例（不连设备）——
驱动自己的校验（正则、点表、模板占位、白名单）就在构造时，配错了当场说清楚，不用等到「测试连接」或执行器
实例化才发现。未登记的顶层键只给提醒：驱动读的可选项很多，拼错的键不会报错、只会被忽略，人要看一眼。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

TYPES = {
    "string": (str,), "integer": (int,), "number": (int, float), "boolean": (bool,), "object": (dict,),
    "array": (list,),
}
TYPE_LABELS = {"string": "文本", "integer": "整数", "number": "数值", "boolean": "是 / 否", "object": "对象", "array": "列表"}


@dataclass(frozen=True)
class ConfigField:
    name: str
    label: str
    type: str
    required: bool = False
    connection: bool = False
    hint: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "label": self.label, "type": self.type, "type_label": TYPE_LABELS[self.type],
                "required": self.required, "connection": self.connection, "hint": self.hint}


@dataclass(frozen=True)
class DriverInfo:
    key: str
    label: str
    protocol: str
    summary: str
    # 指令号、去重、查询由谁负责
    ledger: str
    fields: tuple[ConfigField, ...]
    template: Callable[[dict], dict]
    credential: str = ""
    supports: dict[str, bool] = field(default_factory=lambda: {"hold": True, "abort": True, "query": True, "dedup": True})

    @property
    def connection_keys(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.fields if item.connection)

    def as_dict(self, limits: dict | None = None) -> dict[str, Any]:
        return {
            "key": self.key, "label": self.label, "protocol": self.protocol, "summary": self.summary,
            "ledger": self.ledger, "fields": [item.as_dict() for item in self.fields],
            "connection_keys": list(self.connection_keys), "credential": self.credential,
            "supports": dict(self.supports), "template": self.template(limits or {}),
        }


# ---------- 公共字段 ----------

def _timeouts(request: float = 10) -> tuple[ConfigField, ...]:
    return (
        ConfigField("connect_timeout_sec", "连接超时（秒）", "number", hint="只约束建立连接（含 TLS 握手）"),
        ConfigField("request_timeout_sec", "请求超时（秒）", "number", hint=f"连接建立后的读写，缺省 {request:g} 秒"),
        ConfigField("probe_interval_sec", "探测周期（秒）", "number", hint="执行器多久读一次设备身份判在线"),
    )


COMMON = (
    ConfigField("expected_device_id", "期望设备编号", "string", connection=True,
                hint="设备自报的编号必须与它一致，防止接错设备"),
    ConfigField("heartbeat_mode", "心跳方式", "string", hint="probe（执行器探测）或 push（设备推心跳）"),
    ConfigField("methods", "方法目录（按登记）", "array", hint="协议带不了目录时登记：[\"VD-120\"] 或 [{program, name, capability}]"),
    ConfigField("commands", "设备接受的指令类型", "array"),
    ConfigField("vendor", "厂商（按登记）", "string"),
    ConfigField("firmware", "固件（按登记）", "string"),
    ConfigField("material_map", "实测值折算物料", "object", hint="{实测参数: {material, unit, factor}}"),
    ConfigField("simulator_control", "模拟设备控制口", "object", connection=True,
                hint="只对自报为模拟器的设备生效：{url, token_ref, unit}；接真机时删掉"),
    ConfigField("acceptance", "验收缺省", "object",
                hint="{capability, params}：申请接入验收时缺省用的能力与参数（转运给起止位置 from / to）"),
)
JOBS = (
    ConfigField("start_timeout_sec", "启动确认超时（秒）", "number", hint="发了启动却一直没见到运行，超过它判结果未知"),
    ConfigField("idle_after_start", "启动后回到空闲的含义", "string", hint="done：设备没有「运行中」可查、回到空闲就是做完"),
)


def _params(limits: dict) -> tuple[list[str], Callable[[str], list[str]]]:
    capabilities = sorted(limits or {})

    def params_of(capability: str) -> list[str]:
        return sorted((limits or {}).get(capability) or {})

    return capabilities, params_of


PLC_STATES = {"0": "idle", "1": "running", "2": "held", "3": "done", "4": "failed"}
PULSE = {"value": True, "pulse_ms": 300}


def _plc(kind: str, limits: dict) -> dict:
    """PLC 点表模板：每个参数一个设定值点、一个实测点，外加状态 / 故障 / 启停 / 就绪 / 联锁 / 心跳；地址是占位。"""
    capabilities, params_of = _params(limits)
    params = sorted({name for capability in capabilities for name in params_of(capability)})
    register = [10]
    coils = ["CmdStart", "CmdHold", "CmdResume", "CmdAbort", "CmdAck"]

    def point(name: str, kind_: str, table: str = "holding"):
        if kind == "opcua":
            return f'ns=3;s="DB_ILCS"."{name}"'
        if table == "coil":
            return {"table": "coil", "address": coils.index(name), "type": "bool"}
        address = register[0]
        register[0] += 2 if kind_ == "float32" else 1
        return {"table": table, "address": address, "type": kind_}

    points: dict[str, Any] = {
        "state": point("State", "uint16"), "error": point("ErrorCode", "uint16"), "heartbeat": point("Heartbeat", "uint16"),
        "remote": point("RemoteMode", "uint16"), "safety": point("SafetyOk", "uint16"),
        **{f"cmd_{name[3:].lower()}": point(name, "bool", "coil") for name in coils},
    }
    for name in params:
        points[f"sp_{name}"] = point(f"SP_{name}", "float32")
        points[f"pv_{name}"] = point(f"PV_{name}", "float32")
    return {
        "points": points,
        "ready": {"point": "remote", "ok": [True, 1]}, "interlock": {"point": "safety", "ok": [True, 1]},
        "heartbeat": {"point": "heartbeat", "stale_sec": 30},
        "capabilities": {capability: {
            "write": {name: f"sp_{name}" for name in params_of(capability)},
            "actuals": {name: f"pv_{name}" for name in params_of(capability)},
            "start": {"point": "cmd_start", **PULSE},
        } for capability in capabilities},
        "status": {"point": "state", "states": PLC_STATES}, "error": {"point": "error", "codes": {}},
        "hold": {"point": "cmd_hold", **PULSE}, "resume": {"point": "cmd_resume", **PULSE},
        "abort": {"point": "cmd_abort", **PULSE}, "acknowledge": {"point": "cmd_ack", **PULSE},
    }


def _line(limits: dict) -> dict:
    """串口 / TCP 命令模板：命令与回复格式是占位，必须按设备的命令手册改。"""
    capabilities, params_of = _params(limits)
    return {
        "transport": {"kind": "serial", "port": "rfc2217://serial-server.lab.internal:4001", "baudrate": 9600, "parity": "N"},
        "write_terminator": "\r\n", "read_terminator": "\r\n", "request_timeout_sec": 3, "probe_interval_sec": 10,
        "identity": {"send": "*IDN?", "pattern": "^(?P<vendor>[^,]*),(?P<model>[^,]*),(?P<device_id>[^,]*),(?P<firmware>.*)$"},
        "error_pattern": "^ERR",
        "capabilities": {capability: {"start": [
            *({"send": f"SET {name.upper()} {{{name}}}", "expect": "^OK$"} for name in params_of(capability)),
            {"send": "RUN", "expect": "^OK$"},
        ]} for capability in capabilities},
        "status": {"send": "STAT?", "pattern": "^(?P<state>[A-Z]+)(,(?P<detail>.*))?$",
                   "states": {"IDLE": "idle", "RUN": "running", "HOLD": "held", "DONE": "done", "ALARM": "failed"}},
        "actuals": [],
        "hold": [{"send": "HOLD", "expect": "^OK$"}], "resume": [{"send": "CONT", "expect": "^OK$"}],
        "abort": [{"send": "STOP", "expect": "^OK$"}], "acknowledge": [{"send": "ACK", "expect": "^OK$"}],
    }


def _modbus_tcp(limits: dict) -> dict:
    capabilities, params_of = _params(limits)
    params = sorted({name for capability in capabilities for name in params_of(capability)})
    return {
        "host": "plc.lab.internal", "port": 502, "unit_id": 1, "base_address": 0, "request_timeout_sec": 10,
        "probe_interval_sec": 10,
        "capabilities": {capability: index for index, capability in enumerate(capabilities, start=1)},
        "params": {name: index for index, name in enumerate(params[:16], start=1)},
    }


def _rest(limits: dict) -> dict:
    capabilities, params_of = _params(limits)
    return {
        "base_url": "https://fleet.lab.internal/api/v2.0.0", "verify_tls": True, "request_timeout_sec": 10,
        "probe_interval_sec": 10,
        "identity": {"method": "GET", "path": "/status",
                     "fields": {"device_id": "robot_name", "model": "model", "firmware": "software_version"},
                     "interlock": {"field": "state_text", "values": ["EmergencyStop", "Error"]}},
        "capabilities": {capability: {
            "method": "POST", "path": "/mission_queue", "handle": "id",
            "body": {"mission_id": "{mission}", "message": "ILCS {command_id}",
                     "parameters": [{"id": "From", "value": "{from_position}"}, {"id": "To", "value": "{to_position}"}]}
            if capability == "cap.transfer" else
            {"mission_id": "{mission}", "message": "ILCS {command_id}", **{name: f"{{{name}}}" for name in params_of(capability)}},
            "defaults": {"mission": "<任务模板编号>"},
        } for capability in capabilities},
        "positions": {},
        "status": {"method": "GET", "path": "/mission_queue/{handle}", "field": "state",
                   "states": {"Pending": "accepted", "Executing": "running", "Paused": "held", "Done": "done",
                              "Aborted": "failed"}},
        "lookup": {"method": "GET", "path": "/mission_queue", "detail_path": "/mission_queue/{id}", "id_field": "id",
                   "match_field": "message", "match": "ILCS {command_id}"},
        "abort": {"method": "DELETE", "path": "/mission_queue/{handle}"},
    }


OPCUA_SECURITY = (
    ConfigField("security_policy", "安全策略", "string", hint="Basic256Sha256；None 只限非正式环境"),
    ConfigField("security_mode", "安全模式", "string", hint="SignAndEncrypt / Sign"),
    ConfigField("server_certificate", "服务器证书", "string", connection=True, hint="位于凭据目录内"),
    ConfigField("application_uri", "客户端应用 URI", "string"),
)
TLS = (
    ConfigField("ca_file", "CA 证书", "string", connection=True, hint="私有 CA，必须位于凭据目录内；不填用系统信任链"),
    ConfigField("verify_tls", "校验证书", "boolean", hint="正式环境不允许关闭"),
    ConfigField("allow_insecure_http", "允许不加密 HTTP", "boolean", hint="只限本地联调，正式环境拒绝"),
    ConfigField("headers", "固定请求头", "object", hint="凭据不写这里，放 credential_ref"),
)
POINT_MAP = (
    ConfigField("points", "点表", "object", required=True, hint="点名 → 节点 ID / 寄存器定义"),
    ConfigField("identity", "身份点", "object", hint="{device_id, model, vendor, firmware} → 点名"),
    ConfigField("ready", "就绪条件", "object"), ConfigField("interlock", "联锁条件", "object"),
    ConfigField("heartbeat", "心跳点", "object"), ConfigField("job_id", "指令号写入 / 回显", "object"),
    ConfigField("capabilities", "能力映射", "object", required=True, hint="每项能力的常量、程序号、设定值、启动、实测"),
    ConfigField("status", "状态点与状态映射", "object", required=True),
    ConfigField("error", "故障点与故障码", "object"),
    ConfigField("start_refused", "拒绝启动的故障码", "object",
                hint='{"codes": ["90", "91"], "after_sec": 1}：写下启动沿后 PLC 停在空闲并报这些码 = 明确拒绝、没有动作'),
    ConfigField("hold", "保持", "object"), ConfigField("resume", "恢复", "object"),
    ConfigField("abort", "终止", "object"), ConfigField("acknowledge", "复位", "object"),
)
LINE = (
    ConfigField("transport", "通道", "object", required=True, connection=True,
                hint='{"kind": "tcp", "host", "port"} 或 {"kind": "serial", "port": "rfc2217://…"}'),
    ConfigField("encoding", "编码", "string"), ConfigField("write_terminator", "发送结束符", "string"),
    ConfigField("read_terminator", "接收结束符", "string"), ConfigField("greeting", "欢迎语（正则）", "string"),
    ConfigField("keep_open", "保持连接", "boolean"), ConfigField("inter_command_delay_ms", "命令间隔（毫秒）", "number"),
    ConfigField("identity", "身份命令", "object"), ConfigField("ready", "就绪命令", "object"),
    ConfigField("interlock", "联锁命令", "object"), ConfigField("error_pattern", "错误回复（正则）", "string"),
    ConfigField("error_codes", "故障码说明", "object"),
    ConfigField("capabilities", "能力命令", "object", required=True, hint="每项能力的启动命令列表"),
    ConfigField("status", "状态命令与映射", "object", required=True), ConfigField("actuals", "实测值命令", "array"),
    ConfigField("hold", "保持命令", "array"), ConfigField("resume", "恢复命令", "array"),
    ConfigField("abort", "终止命令", "array"), ConfigField("acknowledge", "复位命令", "array"),
)

DRIVERS: dict[str, DriverInfo] = {item.key: item for item in (
    DriverInfo(
        "http_json_v1", "HTTPS JSON 网关", "HTTPS JSON",
        "设备或厂家 SDK 接口服务实现 ILCS 网关契约（按指令号去重、查询）", "设备侧",
        (ConfigField("base_url", "网关地址", "string", required=True, connection=True, hint="https://…/api/v1，主机须在白名单"),
         *TLS, ConfigField("paths", "接口路径", "object"), ConfigField("idempotency_header", "幂等请求头", "string"),
         ConfigField("device_timezone", "设备时区", "string"), ConfigField("device_id_field", "设备编号字段", "string"),
         *_timeouts(), *COMMON),
        lambda limits: {
            "base_url": "https://instrument-gateway.lab.internal/api/v1", "verify_tls": True, "connect_timeout_sec": 3,
            "request_timeout_sec": 10,
            "paths": {"health": "/health", "submit": "/commands", "query": "/commands/{command_id}",
                      "hold": "/commands/{command_id}/hold", "abort": "/commands/{command_id}/abort"},
            "idempotency_header": "Idempotency-Key",
        },
        credential="file:///run/secrets/ilcs/<工位>.token",
    ),
    DriverInfo(
        "sila2_v1", "SiLA 2", "SiLA 2", "SiLA 2 服务器实现 ILCS TaskExecution 特性", "设备侧",
        (ConfigField("host", "主机", "string", required=True, connection=True),
         ConfigField("port", "端口", "integer", required=True, connection=True),
         ConfigField("ca_file", "服务器证书 / CA", "string", connection=True, hint="位于凭据目录内"),
         ConfigField("insecure", "不加密", "boolean", hint="只限非正式环境"),
         ConfigField("device_timezone", "设备时区", "string"), *_timeouts(), *COMMON),
        lambda limits: {"host": "sila-device.lab.internal", "port": 50052, "ca_file": "/run/secrets/ilcs/sila/<设备>.crt",
                        "request_timeout_sec": 10, "probe_interval_sec": 10},
    ),
    DriverInfo(
        "opcua_v1", "OPC UA TaskExecution", "OPC UA", "OPC UA 服务器实现 ILCS TaskExecution 节点与方法", "设备侧",
        (ConfigField("endpoint", "端点", "string", required=True, connection=True, hint="opc.tcp://主机:端口/…"),
         *OPCUA_SECURITY, ConfigField("device_timezone", "设备时区", "string"), *_timeouts(), *COMMON),
        lambda limits: {"endpoint": "opc.tcp://opcua-device.lab.internal:4840/ilcs/", "security_policy": "Basic256Sha256",
                        "security_mode": "SignAndEncrypt", "server_certificate": "/run/secrets/ilcs/opcua/<设备>.crt",
                        "application_uri": "urn:ilcs:client", "request_timeout_sec": 10, "probe_interval_sec": 10},
        credential="file:///run/secrets/ilcs/opcua/ilcs-client.json",
    ),
    DriverInfo(
        "modbus_tcp_v1", "Modbus 任务寄存器", "Modbus TCP", "PLC 按 ILCS 任务寄存器表编程", "设备侧",
        (ConfigField("host", "主机", "string", required=True, connection=True),
         ConfigField("port", "端口", "integer", connection=True), ConfigField("unit_id", "从站号", "integer", connection=True),
         ConfigField("base_address", "寄存器基地址", "integer"),
         ConfigField("capabilities", "能力码", "object", required=True, hint="{能力: 能力码}，与 PLC 程序核对"),
         ConfigField("params", "参数槽位", "object", required=True, hint="{参数: 槽位 1–16}"),
         ConfigField("poll_interval_ms", "握手轮询（毫秒）", "number"), ConfigField("heartbeat_stale_sec", "心跳超时（秒）", "number"),
         *_timeouts(), *COMMON),
        _modbus_tcp,
    ),
    DriverInfo(
        "opcua_map_v1", "OPC UA 节点映射", "OPC UA 节点映射", "设备自有 OPC UA 节点（PLC、视觉系统）", "驱动作业台账",
        (ConfigField("endpoint", "端点", "string", required=True, connection=True), *OPCUA_SECURITY, *POINT_MAP,
         ConfigField("rejections", "明确拒绝的状态码", "array"), *JOBS, *_timeouts(), *COMMON),
        lambda limits: {"endpoint": "opc.tcp://plc.lab.internal:4840/", "security_policy": "Basic256Sha256",
                        "security_mode": "SignAndEncrypt", "server_certificate": "/run/secrets/ilcs/opcua/<设备>.crt",
                        "application_uri": "urn:ilcs:client", "request_timeout_sec": 10, "probe_interval_sec": 10,
                        **_plc("opcua", limits)},
        credential="file:///run/secrets/ilcs/opcua/ilcs-client.json",
    ),
    DriverInfo(
        "modbus_map_v1", "Modbus 点表", "Modbus TCP 点表", "设备自有 Modbus 寄存器表（PLC、温控仪表）", "驱动作业台账",
        (ConfigField("host", "主机", "string", required=True, connection=True),
         ConfigField("port", "端口", "integer", connection=True), ConfigField("unit_id", "从站号", "integer", connection=True),
         *POINT_MAP, *JOBS, *_timeouts(3), *COMMON),
        lambda limits: {"host": "plc.lab.internal", "port": 502, "unit_id": 1, "request_timeout_sec": 3,
                        "probe_interval_sec": 10, **_plc("modbus", limits)},
    ),
    DriverInfo(
        "line_command_v1", "串口 / TCP 命令", "串口 / TCP 命令", "一问一答的文本命令（RS232 / RS485 / TCP、机械臂仪表盘服务）",
        "驱动作业台账", (*LINE, *JOBS, *_timeouts(5), *COMMON), _line,
    ),
    DriverInfo(
        "mt_sics_v1", "MT-SICS 天平", "MT-SICS", "梅特勒天平（串口或以太网）", "驱动作业台账",
        (ConfigField("transport", "通道", "object", required=True, connection=True),
         ConfigField("device_id_source", "设备编号取自", "string", hint="serial（I4）或 balance_id（I10）"),
         ConfigField("capabilities", "能力", "object", required=True, hint="{能力: {action, result, unit, stable_timeout_sec}}"),
         *JOBS, *_timeouts(3), *COMMON),
        lambda limits: {
            "transport": {"kind": "tcp", "host": "balance.lab.internal", "port": 4305}, "request_timeout_sec": 3,
            "probe_interval_sec": 10, "device_id_source": "serial",
            "capabilities": {capability: {"action": "weigh", "result": (sorted((limits or {}).get(capability) or {}) or ["mass"])[0],
                                          "unit": "g", "stable_timeout_sec": 15}
                             for capability in (sorted(limits or {}) or ["cap.weigh"])},
        },
        supports={"hold": False, "abort": False, "query": True, "dedup": True},
    ),
    DriverInfo(
        "rest_map_v1", "REST 接口映射", "REST 接口映射", "设备或调度系统自有 REST 接口（AGV 车队等）", "驱动作业台账 + 设备任务号",
        (ConfigField("base_url", "接口地址", "string", required=True, connection=True), *TLS,
         ConfigField("identity", "身份请求", "object", required=True), ConfigField("busy", "忙判断", "object"),
         ConfigField("capabilities", "能力请求模板", "object", required=True), ConfigField("positions", "位置映射", "object"),
         ConfigField("status", "状态请求", "object", required=True), ConfigField("lookup", "按指令号找回", "object"),
         ConfigField("hold", "保持", "object"), ConfigField("resume", "恢复", "object"), ConfigField("abort", "终止", "object"),
         *JOBS, *_timeouts(), *COMMON),
        _rest,
        credential="file:///run/secrets/ilcs/<工位>.json",
    ),
    DriverInfo(
        "sql_table_v1", "数据库中间表", "数据库中间表", "厂家调度软件 / MES 经 ILCS 中间库契约的作业表对接", "中间库（作业表主键是指令号）",
        (ConfigField("url", "中间库连接串", "string", required=True, connection=True, hint="口令不写进来，放 credential_ref"),
         ConfigField("jobs_table", "作业表", "string"), ConfigField("device_table", "设备表", "string"),
         ConfigField("device_id", "设备编号", "string", required=True, connection=True),
         ConfigField("heartbeat_stale_sec", "心跳超时（秒）", "number"), *_timeouts(), *COMMON),
        lambda limits: {"url": "postgresql+psycopg2://ilcs_exchange@exchange-db.lab.internal:5432/exchange",
                        "jobs_table": "ilcs_jobs", "device_table": "ilcs_device", "device_id": "<设备编号>",
                        "heartbeat_stale_sec": 30, "request_timeout_sec": 10, "probe_interval_sec": 10},
        credential="file:///run/secrets/ilcs/<工位>.dbpass",
    ),
    DriverInfo(
        "composite_v1", "组合工位", "组合工位", "一个工位由几台接口不同的仪器组成，按能力分派到子驱动", "各子驱动",
        (ConfigField("routes", "路由", "array", required=True,
                     hint="[{name, capabilities, driver, config, credential_ref}]；每项能力只落在一条路由上；"
                          "套用模板时各路由的连接参数按路由名合并"),
         ConfigField("probe_interval_sec", "探测周期（秒）", "number"), *COMMON),
        lambda limits: {"probe_interval_sec": 10, "routes": [
            {"name": f"route{index}", "capabilities": [capability], "driver": "line_command_v1",
             "config": _line({capability: (limits or {}).get(capability) or {}})}
            for index, capability in enumerate(sorted(limits or {}) or ["cap.example"], start=1)
        ]},
    ),
)}


@dataclass
class ConfigCheck:
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "problems": self.problems, "warnings": self.warnings}


def check_fields(driver: str, config: dict, *, template: bool = False) -> ConfigCheck:
    """按驱动登记的字段查缺项与类型；`template=True` 时连接参数可以不填（套用模板时由工位填）。"""
    check = ConfigCheck()
    info = DRIVERS.get(driver)
    if info is None:
        check.problems.append(f"驱动 {driver or '（未填）'} 没有登记；已登记：{', '.join(sorted(DRIVERS))}")
        return check
    if not isinstance(config, dict):
        check.problems.append("配置必须是 JSON 对象")
        return check
    known = {item.name: item for item in info.fields}
    for item in info.fields:
        if item.required and item.name not in config and not (template and item.connection):
            check.problems.append(f"缺少「{item.label}」（{item.name}）")
    for name, value in config.items():
        item = known.get(name)
        if item is None:
            check.warnings.append(f"{name} 不是 {info.label} 登记的配置项：拼错的键会被驱动忽略，请核对")
            continue
        expected = TYPES[item.type]
        if isinstance(value, bool) and item.type in {"integer", "number"} or not isinstance(value, expected):
            check.problems.append(f"「{item.label}」（{name}）应是{TYPE_LABELS[item.type]}")
    return check


def validate_config(driver: str, config: dict, credential_ref: str = "", *, protocol: str = "",
                    template: bool = False) -> ConfigCheck:
    """保存前的配置检查：字段 + 真的构造一次驱动实例（不连设备，不碰任何真实工位的作业台账）。

    `template=True`（设备模板）：模板只有映射，连接参数是示例值、证书之类的文件在别的部署里——
    缺项与类型照样算问题；构造驱动时发现的只算提醒（套用到工位时按真实连接参数再核一次），也不查本部署的主机白名单：
    模板要能跨部署导入导出。
    """
    from ..core.hosts import host_check_skipped
    from .acceptance import AcceptanceRecord
    from .base import AdapterError
    from .registry import REAL_IMPLEMENTATIONS

    check = check_fields(driver, config, template=template)
    implementation = REAL_IMPLEMENTATIONS.get(driver)
    if not check.ok or implementation is None:
        return check
    info = DRIVERS[driver]
    required_connection = [item.name for item in info.fields if item.required and item.connection]
    if template and any(name not in config for name in required_connection):
        return check  # 模板没带连接示例：构造不了驱动，只查字段
    record = AcceptanceRecord(station_id="__validate__", kind="real", driver=driver, protocol=protocol or info.protocol,
                              config=config, credential_ref=credential_ref)
    found = ""
    try:
        if template:
            with host_check_skipped():
                implementation(record)
        else:
            implementation(record)
    except AdapterError as exc:
        found = str(exc)
    except Exception as exc:  # noqa: BLE001  驱动构造时的其他错误同样说明配置用不了
        found = f"配置无法解析：{exc.__class__.__name__}：{exc}"
    if found and template:
        check.warnings.append(f"按连接示例构造驱动时发现：{found}（套用到工位时按真实连接参数再核一次）")
    elif found:
        check.problems.append(found)
    return check


def merge_config(base: dict, overlay: dict) -> dict:
    """套用模板：模板里的映射 + 工位自己的连接参数。对象逐层合并；组合工位的 routes 按路由名合并；其余覆盖。"""
    import copy

    merged = copy.deepcopy(base or {})
    for key, value in (overlay or {}).items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = merge_config(current, value)
        elif key == "routes" and isinstance(current, list) and isinstance(value, list):
            named = {str(item.get("name")): item for item in value if isinstance(item, dict)}
            merged[key] = [
                merge_config(item, named[str(item.get("name"))]) if isinstance(item, dict) and str(item.get("name")) in named
                else item for item in current
            ] + [item for name, item in named.items() if name not in {str(row.get("name")) for row in current
                                                                     if isinstance(row, dict)}]
        else:
            merged[key] = copy.deepcopy(value)
    return merged
