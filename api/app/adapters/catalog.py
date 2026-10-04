"""驱动目录：每个内置驱动自己声明「配置长什么样」，界面按它出表单，保存时按它校验。

ILCS 只登记两个契约驱动（`sila2_v1`、`http_json_v1`）：协议驱动（PLC 点表、Modbus / OPC UA 任务契约、REST、串口命令）
都在驱动宿主（ilcs-devices/host）里，它们的映射写在驱动宿主的设备文件里，不在这里。每个驱动在这里登记：

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
    minimum: float | None = None  # integer / number：最小值

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name, "label": self.label, "type": self.type, "type_label": TYPE_LABELS[self.type],
            "required": self.required, "connection": self.connection, "hint": self.hint,
        }
        if self.minimum is not None:
            out["minimum"] = self.minimum
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

def _timeouts(request: float = 10, connect: str = "只约束建立连接（含 TLS 握手）") -> tuple[ConfigField, ...]:
    return (
        ConfigField("connect_timeout_sec", "连接超时（秒）", "number", hint=connect),
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
    ConfigField("wells_per_command", "一条指令最多几个样本", "integer", minimum=1,
                hint="设备一次只能处理一个样本（秤上一个位置、单测量位）时填 1：一步要做的样本（孔位）超过它，ILCS 逐样本拆开、"
                     "依次下发（设备指令号 <指令号>/<序号>，每条只带这几个样本的孔位与参数），每个样本做完就按实际量入账；"
                     "不填就一条指令带全部样本"),
    ConfigField("vendor", "厂商（按登记）", "string"),
    ConfigField("firmware", "固件（按登记）", "string"),
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
    _record("environment", "环境采集", (
        ConfigField("zone", "区域", "string", required=True,
                    hint="工位编号或房间 / 手套箱名：步骤的环境要求按区域取读数"),
        _table("points", "指标 ← 点", _point("", "点"), "指标", required=True,
               key_options=("temperature", "humidity", "dew_point", "h2o_ppm", "o2_ppm", "pressure", "pressure_diff",
                            "particles"),
               hint="{temperature: \"temperature\", humidity: \"humidity\"}：每个环境指标读哪个点"),
        ConfigField("interval_sec", "采集周期（秒）", "number", hint="缺省 60 秒；执行器探测在线时顺带读"),
    ), hint="把点位读数记成环境读数，开跑检查与下发前按它核对步骤的环境要求：{zone, points: {指标: 点名}, interval_sec}。"
            "只对执行器探测的设备生效"),
)
TLS = (
    ConfigField("ca_file", "CA 证书", "string", connection=True, hint="私有 CA，必须位于凭据目录内；不填用系统信任链"),
    ConfigField("verify_tls", "校验证书", "boolean", hint="正式环境不允许关闭"),
    ConfigField("allow_insecure_http", "允许不加密 HTTP", "boolean", hint="只限本地联调，正式环境拒绝"),
    _table("headers", "固定请求头", _value("值", "string"), "请求头", hint="凭据不写这里，放 credential_ref"),
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
        "sila2_v1", "SiLA 2", "SiLA 2", "SiLA 2 设备服务（驱动宿主、厂商网关）实现 ILCS 的 DeviceInfo / PointAccess / TaskExecution", "设备侧",
        (ConfigField("host", "主机", "string", required=True, connection=True),
         ConfigField("port", "端口", "integer", required=True, connection=True),
         ConfigField("ca_file", "服务器证书 / CA", "string", connection=True, hint="位于凭据目录内"),
         ConfigField("insecure", "不加密", "boolean", hint="只限非正式环境"),
         ConfigField("tasks", "参与自动流程", "boolean",
                     hint="缺省是；只读写点位的设备（设备服务没有实现 TaskExecution）填 false"),
         ConfigField("device_timezone", "设备时区", "string"),
         *_timeouts(connect="探测端口；探通后建客户端（TLS、读特性清单）最多再等连接超时 + 请求超时"), *COMMON),
        lambda limits: {"host": "sila-device.lab.internal", "port": 50052, "ca_file": "/run/secrets/ilcs/sila/<设备>.crt",
                        "request_timeout_sec": 10, "probe_interval_sec": 10},
        credential="file:///run/secrets/ilcs/sila/<驱动宿主>.token",
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
        if item.minimum is not None and value < item.minimum:
            check.problems.append(f"「{item.label}」（{name}）不能小于 {item.minimum:g}")
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


def has_tasks(driver: str, config: dict | None) -> bool:
    """这份配置让设备参与自动流程（接指令）吗？SiLA 设备服务看 tasks（缺省参与；只读写点位的设备填 false）；
    HTTPS 网关一律参与。"""
    if driver == "sila2_v1":
        return (config or {}).get("tasks", True) is not False
    return True


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
    if not found and not has_tasks(driver, config):
        check.warnings.append("tasks: false（只读写点位）：可以读值、手动写标了可写的点，但不参与自动流程"
                              "（排到这台设备的指令会被拒绝；接入验收只要求只读级）")
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
