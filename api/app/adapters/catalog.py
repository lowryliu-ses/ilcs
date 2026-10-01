"""驱动目录：每个内置驱动自己声明「配置长什么样」，界面按它出表单，保存时按它校验。

以前每个驱动的配置模板写死在前端，加一个驱动要改页面；现在驱动在这里登记：

- `fields`：顶层配置项（名称、类型、是否必填、说明），其中 `connection=True` 的是每台设备自己的连接参数
  （地址、端口、证书、设备编号）——设备模板里不写死它们，套用模板时由工位填；
- `template(limits)`：按工位能力极限生成一份起步配置（地址、点位、命令都是占位，要按设备手册改）；
- `supports`：这类驱动缺省声明支持的保持 / 终止 / 查询 / 去重。

字段可以往下描述嵌套结构（点表、能力映射、命令列表、状态映射）：`fields` 是固定的几个键，`entries` 是
「任意键 → 同一种值」的表，`items` 是列表的每一项；`ref` / `key_ref` 说明值或键是别处登记的名字（点名、能力、参数）。
界面按这些出表单（完整 JSON 仍可直接改），检查时按它提醒嵌套结构里拼错的键。

`validate_config` 在保存前把配置过一遍：先按字段说明查缺项与类型，再真的构造一次驱动实例（不连设备）——
驱动自己的校验（正则、点表、模板占位、白名单）就在构造时，配错了当场说清楚，不用等到「测试连接」或执行器
实例化才发现。未登记的键（顶层和嵌套结构里）只给提醒：驱动读的可选项很多，拼错的键不会报错、只会被忽略，人要看一眼。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

TYPES = {
    "string": (str,), "integer": (int,), "number": (int, float), "boolean": (bool,), "object": (dict,),
    "array": (list,), "scalar": (str, int, float, bool), "any": (object,),
}
TYPE_LABELS = {"string": "文本", "integer": "整数", "number": "数值", "boolean": "是 / 否", "object": "对象", "array": "列表",
               "scalar": "值", "any": "任意 JSON"}
STATES = ("idle", "running", "held", "done", "failed")


@dataclass(frozen=True)
class ConfigField:
    name: str
    label: str
    type: str
    required: bool = False
    connection: bool = False
    hint: str = ""
    # ---- 嵌套结构（界面按它出表单，检查按它提醒拼错的键）----
    fields: tuple[ConfigField, ...] = ()  # object：固定的几个键
    entries: ConfigField | None = None  # object：任意键 → 同一种值（点表、能力映射、状态映射）
    items: ConfigField | None = None  # array：每一项
    options: tuple[str, ...] = ()  # 只能取这几个值
    ref: str = ""  # 值是别处登记的名字：points（点名）/ capabilities（能力）
    key_ref: str = ""  # 键从哪来：points / capabilities / params（当前能力的参数）/ options（当前参数的选项）
    key_options: tuple[str, ...] = ()  # 键的常用写法（身份字段、状态码）
    key_label: str = ""
    scope: str = ""  # capability / param：往下走时记住键是哪项能力 / 哪个参数，给下层的 key_ref 用
    shorthand: str = ""  # 只填这一个键时可以简写成它的值（"sp_temp" 就是 {"point": "sp_temp"}）
    single: bool = False  # array：只有一项时也可以不写成列表

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name, "label": self.label, "type": self.type, "type_label": TYPE_LABELS[self.type],
            "required": self.required, "connection": self.connection, "hint": self.hint,
        }
        if self.fields:
            out["fields"] = [item.as_dict() for item in self.fields]
        if self.entries is not None:
            out["entries"] = self.entries.as_dict()
        if self.items is not None:
            out["items"] = self.items.as_dict()
        for key in ("options", "key_options"):
            if getattr(self, key):
                out[key] = list(getattr(self, key))
        for key in ("ref", "key_ref", "key_label", "scope", "shorthand", "single"):
            if getattr(self, key):
                out[key] = getattr(self, key)
        return out


def _value(label: str, type_: str = "scalar", **kwargs) -> ConfigField:
    """表里的值 / 列表的项：没有自己的键名。"""
    return ConfigField("", label, type_, **kwargs)


def _record(name: str, label: str, fields: tuple[ConfigField, ...], hint: str = "", **kwargs) -> ConfigField:
    return ConfigField(name, label, "object", hint=hint, fields=fields, **kwargs)


def _table(name: str, label: str, value: ConfigField, key_label: str, hint: str = "", **kwargs) -> ConfigField:
    return ConfigField(name, label, "object", hint=hint, entries=value, key_label=key_label, **kwargs)


def _list(name: str, label: str, item: ConfigField, hint: str = "", **kwargs) -> ConfigField:
    return ConfigField(name, label, "array", hint=hint, items=item, **kwargs)


def _point(name: str, label: str, hint: str = "", **kwargs) -> ConfigField:
    return ConfigField(name, label, "string", hint=hint, ref="points", **kwargs)


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


ACCEPTANCE_FIELDS = (
    ConfigField("capability", "能力", "string", ref="capabilities", hint="缺省工位第一项能力"),
    _table("params", "参数", _value("值", "any"), "参数", key_ref="params", hint="转运给起止位置 from / to"),
)
COMMON = (
    ConfigField("expected_device_id", "期望设备编号", "string", connection=True,
                hint="设备自报的编号必须与它一致，防止接错设备"),
    ConfigField("heartbeat_mode", "心跳方式", "string", hint="probe（执行器探测）或 push（设备推心跳）",
                options=("probe", "push")),
    _list("methods", "方法目录（按登记）", _record("", "程序", (
        ConfigField("program", "设备端程序", "string", required=True),
        ConfigField("name", "名称", "string"),
        ConfigField("capability", "对应能力", "string", ref="capabilities"),
    ), shorthand="program"), hint="协议带不了目录时登记：[\"VD-120\"] 或 [{program, name, capability}]"),
    _list("commands", "设备接受的指令类型", _value("指令类型", "string")),
    ConfigField("vendor", "厂商（按登记）", "string"),
    ConfigField("firmware", "固件（按登记）", "string"),
    _table("material_map", "实测值折算物料", _record("", "折算", (
        ConfigField("material", "物料", "string", required=True),
        ConfigField("unit", "单位", "string"),
        ConfigField("factor", "折算系数", "number", hint="实测值 × 系数 = 物料用量，缺省 1"),
    )), "实测参数", hint="{实测参数: {material, unit, factor}}", key_ref="params"),
    _record("simulator_control", "模拟设备控制口", (
        ConfigField("url", "控制口地址", "string", required=True),
        ConfigField("base_url", "控制口地址（同 url）", "string"),
        ConfigField("token_ref", "令牌引用", "string", hint="file://… / env://…，不写原文"),
        ConfigField("unit", "单元", "string", hint="一个进程模拟多台设备（车队）时填设备编号"),
        ConfigField("ca_file", "CA 证书", "string"),
        ConfigField("verify_tls", "校验证书", "boolean"),
        ConfigField("request_timeout_sec", "请求超时（秒）", "number"),
        ConfigField("connect_timeout_sec", "连接超时（秒）", "number"),
    ), connection=True, hint="只对自报为模拟器的设备生效：{url, token_ref, unit}；接真机时删掉"),
    _record("acceptance", "验收缺省", ACCEPTANCE_FIELDS,
            hint="{capability, params}：申请接入验收时缺省用的能力与参数（转运给起止位置 from / to）"),
)
IDLE_AFTER_START = ("done", "unknown")
JOBS = (
    ConfigField("start_timeout_sec", "启动确认超时（秒）", "number", hint="发了启动却一直没见到运行，超过它判结果未知"),
    ConfigField("idle_after_start", "启动后回到空闲的含义", "string", options=IDLE_AFTER_START,
                hint="done：设备没有「运行中」可查、回到空闲就是做完"),
)
# 驱动作业台账类驱动每项能力都能带的几项
CAPABILITY_EXTRAS = (
    _table("defaults", "参数缺省值", _value("值"), "参数", key_ref="params",
           hint="指令里没带时用它；也可以放模板占位用的常量（如 REST 的 {mission}）"),
    ConfigField("program", "缺省设备端程序", "string", hint="步骤没引用设备方法时用它"),
    _list("accept", "另外接受的参数", _value("参数", "string"), hint="映射里没用到、但允许指令带的参数"),
    ConfigField("idle_after_start", "启动后回到空闲的含义", "string", options=IDLE_AFTER_START, hint="只对这项能力，覆盖全局设置"),
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
    ConfigField("security_policy", "安全策略", "string", hint="Basic256Sha256；None 只限非正式环境",
                options=("Basic256Sha256", "None")),
    ConfigField("security_mode", "安全模式", "string", hint="SignAndEncrypt / Sign", options=("SignAndEncrypt", "Sign")),
    ConfigField("server_certificate", "服务器证书", "string", connection=True, hint="位于凭据目录内"),
    ConfigField("application_uri", "客户端应用 URI", "string"),
)
TLS = (
    ConfigField("ca_file", "CA 证书", "string", connection=True, hint="私有 CA，必须位于凭据目录内；不填用系统信任链"),
    ConfigField("verify_tls", "校验证书", "boolean", hint="正式环境不允许关闭"),
    ConfigField("allow_insecure_http", "允许不加密 HTTP", "boolean", hint="只限本地联调，正式环境拒绝"),
    _table("headers", "固定请求头", _value("值", "string"), "请求头", hint="凭据不写这里，放 credential_ref"),
)


# ---------- PLC 点表（opcua_map_v1 / modbus_map_v1）----------

def _signal(name: str, label: str, *extra: ConfigField) -> ConfigField:
    return _record(name, label, (
        _point("point", "信号点", required=True),
        ConfigField("value", "写入值", "scalar", hint="缺省 true"),
        ConfigField("pulse_ms", "脉冲宽度（毫秒）", "number", hint="写入后隔多久写回复位值；不填就不复位"),
        ConfigField("reset", "复位值", "scalar", hint="缺省 false（数值点为 0）"),
        *extra,
    ))


def _gate(name: str, label: str) -> ConfigField:
    return _record(name, label, (
        _point("point", "判断点", required=True),
        _list("ok", "放行的值", _value("值"), hint="读到其中之一才算满足，缺省 [true]"),
    ))


OPCUA_POINT = _record("", "节点", (
    ConfigField("node", "节点 ID", "string", required=True,
                hint='ns=3;s="DB_ILCS"."State"，或带命名空间 URI 的 nsu=urn:…;s=…（服务器重启后序号会变，URI 不变）'),
    ConfigField("scale", "比例", "number", hint="读数 × 比例 = 工程值，写入时反算"),
), shorthand="node")
MODBUS_POINT = _record("", "寄存器", (
    ConfigField("table", "表", "string", options=("holding", "input", "coil", "discrete"),
                hint="只有 holding 与 coil 可写；缺省 holding"),
    ConfigField("address", "地址", "integer", required=True, hint="协议里的 0 基地址（手册上写 40001 的填 0）"),
    ConfigField("type", "类型", "string", options=("uint16", "int16", "uint32", "int32", "float32", "bool", "ascii"),
                hint="缺省 uint16"),
    ConfigField("scale", "比例", "number", hint="读数 × 比例 = 工程值，写入时反算"),
    ConfigField("word_order", "字序", "string", options=("big", "little"), hint="32 位数值缺省高字在前"),
    ConfigField("bit", "位", "integer", hint="从保持寄存器取某一位（只读）"),
    ConfigField("length", "字符数", "integer", hint="ascii 必填，≤240"),
))


def _point_map(kind: str) -> tuple[ConfigField, ...]:
    start: list[ConfigField] = [
        _point("point", "启动点", hint="写下启动信号的点" + ("；也可以改用启动方法" if kind == "opcua" else "")),
        ConfigField("value", "写入值", "scalar", hint="缺省 true"),
        ConfigField("pulse_ms", "脉冲宽度（毫秒）", "number", hint="写入后隔多久写回复位值"),
        ConfigField("reset", "复位值", "scalar"),
    ]
    if kind == "opcua":
        start.append(_record("method", "启动方法", (
            ConfigField("object", "对象节点", "string", required=True),
            ConfigField("method", "方法节点", "string", required=True),
            _list("args", "方法参数", _value("参数"), hint="可用 {program}、{参数} 占位"),
        ), hint="设备用方法调用启动时填，与启动点二选一"))
    capability = _record("", "能力", (
        _table("constants", "常量", _value("值"), "点名", key_ref="points", hint="每次启动前先写的固定值"),
        _record("recipe", "程序号", (
            _point("point", "程序号点", required=True),
            _table("map", "程序 → 程序号", _value("程序号"), "设备端程序", hint="不在表里的程序直接拒绝"),
            ConfigField("default", "缺省程序", "string", hint="步骤没引用设备方法时用它"),
        ), hint="把设备方法的设备端程序换成 PLC 的程序号"),
        _table("write", "设定值", _record("", "写入", (
            _point("point", "设定值点", required=True),
            _table("map", "选项 → 设备代码", _value("代码"), "选项", key_ref="options",
                   hint="选项型参数（溶剂、模式）下发时换成设备代码，没登记的选项拒绝"),
        ), shorthand="point"), "参数", key_ref="params", scope="param", hint="参数 → 设定值点"),
        _record("start", "启动", tuple(start), required=True),
        _table("actuals", "实测点", _point("", "实测点"), "参数", key_ref="params", hint="做完读回的实测值"),
        *CAPABILITY_EXTRAS,
    ))
    return (
        _table("points", "点表", OPCUA_POINT if kind == "opcua" else MODBUS_POINT, "点名", required=True,
               hint="点名 → 节点 ID" if kind == "opcua" else "点名 → 寄存器定义"),
        _table("identity", "身份点", _point("", "点"), "身份字段", hint="{device_id, model, vendor, firmware} → 点名",
               key_options=("device_id", "serial", "model", "vendor", "firmware")),
        _gate("ready", "就绪条件"), _gate("interlock", "联锁条件"),
        _record("heartbeat", "心跳点", (
            _point("point", "心跳计数点", required=True),
            ConfigField("stale_sec", "多久不变判失联（秒）", "number", hint="缺省 30"),
        ), shorthand="point"),
        _record("job_id", "指令号写入 / 回显", (
            _point("write", "写指令号的点"),
            _point("echo", "回显指令号的点", hint="PLC 回显时，启动未确认的作业可以按它找回"),
        )),
        _table("capabilities", "能力映射", capability, "能力", key_ref="capabilities", scope="capability", required=True,
               hint="每项能力的常量、程序号、设定值、启动、实测"),
        _record("status", "状态点与状态映射", (
            _point("point", "状态点", required=True),
            _table("states", "状态映射", _value("状态", "string", options=STATES), "状态值", required=True),
        ), required=True),
        _record("error", "故障点与故障码", (
            _point("point", "故障点", required=True),
            _table("codes", "故障码说明", _value("说明", "string"), "故障码"),
        )),
        _record("start_refused", "拒绝启动的故障码", (
            _list("codes", "故障码", _value("故障码")),
            ConfigField("after_sec", "启动后多久才认（秒）", "number", hint="缺省 1：给 PLC 一个扫描周期"),
        ), hint='{"codes": ["90", "91"], "after_sec": 1}：写下启动沿后 PLC 停在空闲并报这些码 = 明确拒绝、没有动作'),
        _signal("hold", "保持"), _signal("resume", "恢复"), _signal("abort", "终止"),
        _signal("acknowledge", "复位", ConfigField("settle_ms", "复位后等（毫秒）", "number", hint="缺省 200")),
    )


# ---------- 串口 / TCP 文本命令（line_command_v1）----------

def _query(name: str, label: str, group: str = "", *extra: ConfigField, hint: str = "", **kwargs) -> ConfigField:
    return _record(name, label, (
        ConfigField("send", "查询命令", "string", required=True),
        ConfigField("pattern", "回复格式（正则）", "string", required=True,
                    hint=f"要带命名组 (?P<{group}>…)" if group else "每个命名组 (?P<名>…) 就是一个字段"),
        *extra,
    ), hint=hint, **kwargs)


LINE_STEP = _record("", "命令", (
    ConfigField("send", "发送", "string", required=True, hint="可用 {参数}、{参数:.1f}、{command_id}、{program} 占位"),
    ConfigField("expect", "期望回复（正则）", "string"),
    ConfigField("reject", "拒绝回复（正则）", "string"),
    ConfigField("reply", "等回复", "boolean", hint="缺省等；设备不回复的命令关掉"),
    ConfigField("wait_ms", "发完等（毫秒）", "number"),
    ConfigField("motion", "动作命令", "boolean", hint="这一条才让设备动作；缺省是最后一条"),
))
LINE_OK = _list("ok", "放行的值", _value("值", "string"), hint="命名组 value 读到其中之一才算满足")
LINE = (
    _record("transport", "通道", (
        ConfigField("kind", "类型", "string", required=True, options=("tcp", "serial")),
        ConfigField("host", "主机", "string", hint="TCP 通道填"),
        ConfigField("port", "端口", "scalar", required=True,
                    hint="TCP 填端口号；串口填 /dev/ttyUSB0、COM3、rfc2217://主机:端口 或 socket://主机:端口"),
        ConfigField("baudrate", "波特率", "integer", hint="串口，缺省 9600"),
        ConfigField("bytesize", "数据位", "integer", hint="缺省 8"),
        ConfigField("parity", "校验", "string", options=("N", "E", "O", "M", "S"), hint="缺省 N"),
        ConfigField("stopbits", "停止位", "number", hint="缺省 1"),
        ConfigField("xonxoff", "软件流控", "boolean"),
        ConfigField("rtscts", "硬件流控", "boolean"),
    ), required=True, connection=True, hint='{"kind": "tcp", "host", "port"} 或 {"kind": "serial", "port": "rfc2217://…"}'),
    ConfigField("encoding", "编码", "string", hint="缺省 ascii"), ConfigField("write_terminator", "发送结束符", "string"),
    ConfigField("read_terminator", "接收结束符", "string"), ConfigField("greeting", "欢迎语（正则）", "string"),
    ConfigField("keep_open", "保持连接", "boolean"), ConfigField("inter_command_delay_ms", "命令间隔（毫秒）", "number"),
    _list("identity", "身份命令", _query("", "身份查询", hint="命名组 device_id / serial / model / vendor / firmware"),
          single=True, hint="一条或几条查询，各自的命名组合在一起就是设备身份"),
    _query("ready", "就绪命令", "value", LINE_OK),
    _query("interlock", "联锁命令", "value", LINE_OK),
    ConfigField("error_pattern", "错误回复（正则）", "string"),
    _table("error_codes", "故障码说明", _value("说明", "string"), "故障码"),
    _table("capabilities", "能力命令", _record("", "能力", (
        _list("start", "启动命令", LINE_STEP, required=True, hint="依次发送；动作命令之前的命令被拒，设备没有动作"),
        _record("result", "即时结果", (
            ConfigField("pattern", "结果格式（正则）", "string", required=True, hint="命名组就是结果字段"),
        ), hint="读码器这类即时动作：动作命令的回复就是结果，作业当场完成"),
        *CAPABILITY_EXTRAS,
    )), "能力", key_ref="capabilities", scope="capability", required=True, hint="每项能力的启动命令列表"),
    _query("status", "状态命令与映射", "state",
           _table("states", "状态映射", _value("状态", "string", options=STATES), "设备状态值", required=True),
           hint="可选命名组 detail：故障时按故障码说明翻译", required=True),
    _list("actuals", "实测值命令", _query("", "实测查询"), single=True, hint="做完读回实测值：每个命名组是一个实测参数"),
    _list("hold", "保持命令", LINE_STEP), _list("resume", "恢复命令", LINE_STEP),
    _list("abort", "终止命令", LINE_STEP), _list("acknowledge", "复位命令", LINE_STEP),
)


# ---------- 设备自有 REST 接口（rest_map_v1）----------

def _request(name: str, label: str, *extra: ConfigField, path_hint: str = "", hint: str = "", **kwargs) -> ConfigField:
    return _record(name, label, (
        ConfigField("method", "方法", "string", options=("GET", "POST", "PUT", "PATCH", "DELETE"), hint="缺省 GET"),
        ConfigField("path", "路径", "string", required=True, hint=path_hint or "base_url 下以 / 开头的相对路径，可用 {占位}"),
        ConfigField("body", "请求体", "any", hint="JSON；字符串里可用 {参数}、{command_id} 占位"),
        *extra,
    ), hint=hint, **kwargs)


def _match(name: str, label: str, values_label: str) -> ConfigField:
    return _record(name, label, (
        ConfigField("field", "响应字段", "string", required=True, hint="字段路径，如 state_text 或 a.b.0.c"),
        _list("values", values_label, _value("值")),
    ))


REST_STATES = ("idle", "accepted", "running", "held", "done", "failed")
REST = (
    _request("identity", "身份请求",
             _table("fields", "身份字段", _value("响应字段", "string"), "身份字段",
                    key_options=("device_id", "serial", "model", "vendor", "firmware")),
             _match("interlock", "急停 / 故障判断", "取这些值时算联锁"), _match("ready", "就绪判断", "取这些值时算就绪"),
             required=True, hint="没有身份请求就无法判断在线"),
    _request("busy", "忙判断", ConfigField("field", "响应字段", "string", required=True),
             _list("values", "取这些值时算忙", _value("值")), hint="不配就不判忙（调度系统自己排队）"),
    _table("capabilities", "能力请求模板", _request("", "能力",
           ConfigField("handle", "任务号字段", "string", hint="响应里设备任务号在哪个字段；不填用指令号"),
           _table("actuals", "实测字段", _value("响应字段", "string"), "参数", key_ref="params", hint="做完按状态请求的响应读"),
           *CAPABILITY_EXTRAS), "能力", key_ref="capabilities", scope="capability", required=True),
    _table("positions", "位置映射", _value("设备站点编号", "string"), "ILCS 位置编号",
           hint="配置后 {from_position} / {to_position} 按它查表，查不到明确拒绝"),
    _request("status", "状态请求", ConfigField("field", "状态字段", "string", required=True),
             _table("states", "状态映射", _value("状态", "string", options=REST_STATES), "状态值", required=True),
             ConfigField("error_field", "故障说明字段", "string"),
             path_hint="可用 {handle}（设备任务号）、{command_id}", required=True),
    _request("lookup", "按指令号找回",
             ConfigField("detail_path", "详情路径", "string", hint="列表项里没有匹配字段时逐个取详情，可用 {id}"),
             ConfigField("id_field", "任务号字段", "string", hint="缺省 id"),
             ConfigField("match_field", "匹配字段", "string", hint="缺省 message"),
             ConfigField("match", "匹配内容", "string", hint="缺省 {command_id}；要与请求里带的指令号写法一致"),
             ConfigField("recent", "只看最近几条", "integer", hint="缺省 20"),
             hint="启动请求没拿到应答时，按请求里带的指令号在设备侧找回"),
    _request("hold", "保持", path_hint="可用 {handle}、{command_id}"),
    _request("resume", "恢复", path_hint="可用 {handle}、{command_id}"),
    _request("abort", "终止", path_hint="可用 {handle}、{command_id}"),
)

DRIVERS: dict[str, DriverInfo] = {item.key: item for item in (
    DriverInfo(
        "http_json_v1", "HTTPS JSON 网关", "HTTPS JSON",
        "设备或厂家 SDK 接口服务实现 ILCS 网关契约（按指令号去重、查询）", "设备侧",
        (ConfigField("base_url", "网关地址", "string", required=True, connection=True, hint="https://…/api/v1，主机须在白名单"),
         *TLS, _record("paths", "接口路径", tuple(ConfigField(key, label, "string") for key, label in (
             ("health", "健康检查"), ("submit", "下发"), ("query", "按指令号查询"), ("hold", "保持"), ("abort", "终止"),
         )), hint="网关下以 / 开头的相对路径，可用 {command_id}"),
         ConfigField("idempotency_header", "幂等请求头", "string"),
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
         _table("capabilities", "能力码", _value("能力码", "integer"), "能力", key_ref="capabilities", required=True,
                hint="{能力: 能力码}，与 PLC 程序核对"),
         _table("params", "参数槽位", _value("槽位", "integer"), "参数", key_ref="params", required=True,
                hint="{参数: 槽位 1–16}"),
         ConfigField("poll_interval_ms", "握手轮询（毫秒）", "number"), ConfigField("heartbeat_stale_sec", "心跳超时（秒）", "number"),
         *_timeouts(), *COMMON),
        _modbus_tcp,
    ),
    DriverInfo(
        "opcua_map_v1", "OPC UA 节点映射", "OPC UA 节点映射", "设备自有 OPC UA 节点（PLC、视觉系统）", "驱动作业台账",
        (ConfigField("endpoint", "端点", "string", required=True, connection=True), *OPCUA_SECURITY, *_point_map("opcua"),
         _table("rejections", "明确拒绝的状态码", _value("含义", "string"), "OPC UA 状态码",
                key_options=("BadInvalidState", "BadInvalidArgument", "BadOutOfRange", "BadResourceUnavailable",
                             "BadNotSupported", "BadUserAccessDenied"),
                hint="启动方法返回这些状态码 = 设备明确没执行；在内置的几项之外补充"),
         *JOBS, *_timeouts(), *COMMON),
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
         *_point_map("modbus"), *JOBS, *_timeouts(3), *COMMON),
        lambda limits: {"host": "plc.lab.internal", "port": 502, "unit_id": 1, "request_timeout_sec": 3,
                        "probe_interval_sec": 10, **_plc("modbus", limits)},
    ),
    DriverInfo(
        "line_command_v1", "串口 / TCP 命令", "串口 / TCP 命令", "一问一答的文本命令（RS232 / RS485 / TCP、机械臂仪表盘服务）",
        "驱动作业台账", (*LINE, *JOBS, *_timeouts(5), *COMMON), _line,
    ),
    DriverInfo(
        "rest_map_v1", "REST 接口映射", "REST 接口映射", "设备或调度系统自有 REST 接口（AGV 车队等）", "驱动作业台账 + 设备任务号",
        (ConfigField("base_url", "接口地址", "string", required=True, connection=True), *TLS, *REST,
         *JOBS, *_timeouts(), *COMMON),
        _rest,
        credential="file:///run/secrets/ilcs/<工位>.json",
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
        if _short(item, value):
            check.warnings.extend(nested_warnings(item, value, name))
            continue
        expected = TYPES[item.type]
        if isinstance(value, bool) and item.type in {"integer", "number"} or not isinstance(value, expected):
            check.problems.append(f"「{item.label}」（{name}）应是{TYPE_LABELS[item.type]}")
            continue
        check.warnings.extend(nested_warnings(item, value, name))
    return check


def _short(spec: ConfigField, value) -> bool:
    """简写：`"heartbeat": "hb"` 就是 `{"point": "hb"}`；只有一项的列表可以直接写那一项。"""
    if spec.single and spec.items is not None and isinstance(value, dict):
        return True
    return bool(spec.shorthand) and isinstance(value, (str, int, float)) and not isinstance(value, bool)


def nested_warnings(spec: ConfigField, value, path: str) -> list[str]:
    """嵌套结构里没登记的键：驱动只读它认识的键，拼错的（`pulse-ms`、`state`）不报错、只会被忽略——
    脉冲不复位、状态没人读。只提醒不拒绝；类型对不上的交给驱动构造时的校验。"""
    if spec.single and spec.items is not None and isinstance(value, dict):
        return nested_warnings(spec.items, value, path)
    if _short(spec, value):
        return []
    warnings: list[str] = []
    if spec.fields and isinstance(value, dict):
        known = {item.name: item for item in spec.fields}
        for key, child in value.items():
            item = known.get(key)
            if item is None:
                warnings.append(f"{path}.{key} 不是登记的配置项：拼错的键会被驱动忽略，请核对")
            else:
                warnings.extend(nested_warnings(item, child, f"{path}.{key}"))
    elif spec.entries is not None and isinstance(value, dict):
        for key, child in value.items():
            warnings.extend(nested_warnings(spec.entries, child, f"{path}.{key}"))
    elif spec.items is not None and isinstance(value, list):
        for index, child in enumerate(value):
            warnings.extend(nested_warnings(spec.items, child, f"{path}[{index}]"))
    return warnings


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
    """套用模板：模板里的映射 + 工位自己的连接参数。对象逐层合并，其余覆盖。"""
    import copy

    merged = copy.deepcopy(base or {})
    for key, value in (overlay or {}).items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = merge_config(current, value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged
