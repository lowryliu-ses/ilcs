"""MethodSCRIPT 编解码（纯函数，只用标准库）：数据包、SI 前缀、脚本里的数值写法、错误行、固件版本、各型号的极限。

依据 PalmSens 公开文档（见 README「参考」）：MethodSCRIPT 手册 v1.8（2025-10-15）第 4–6、11 章与附录 A–C，
EmStat4 通讯协议 v1.3、EmStat Pico 通讯协议 v1.6。代码是照文档自己写的，没有拷 PalmSens 的示例代码
（那份许可证限定只能和 PalmSens 的设备一起用，假仪器不是）。

数据包（仪器 → 主机）：

    Pda7F0BDF9u;ba7678CD7p,10,20F,40

`P` 开头，变量之间用 `;` 分开。每个变量 = 2 个字母的变量类型（`da` 设定电位、`ba` 电流……）+ 7 位十六进制 + 1 个
SI 前缀（没有前缀是空格，整数是 `i`），值 = (十六进制 − 0x8000000) × 前缀的倍数；`     nan`（5 个空格 + nan）是无效值。
后面可以跟 `,` 分隔的元数据：`1X` 状态位（1 时序不满足、2 过载、4 欠载、8 过载预警），`2XX` 量程号，`4X` 噪声等级。

脚本里的数（主机 → 仪器）写成「整数 + SI 前缀」（`100m` = 0.1，`200k`），整数后缀 `i`（`51i`）；不能写小数点。
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import re
from typing import Any

# SI 前缀 → 倍数（手册表 1）；空格 = 没有前缀
SI_PREFIXES = {
    "a": 1e-18, "f": 1e-15, "p": 1e-12, "n": 1e-9, "u": 1e-6, "m": 1e-3, " ": 1.0,
    "k": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15, "E": 1e18,
}
OFFSET = 1 << 27  # 数据包里的值 = 28 位无符号十六进制 − 0x8000000
NAN_FIELD = "     nan"
# 脚本里的整数部分不超过这个（int32）；数据包里能写的是 ±0x7FFFFFF
LITERAL_LIMIT = 2**31 - 1
PACKAGE_LIMIT = OFFSET - 1

# 用到的变量类型（手册附录 C）：类型 → (含义, 单位)
VAR_TYPES = {
    "aa": ("未初始化", ""), "ab": ("WE 对 RE 实测电位", "V"), "ac": ("CE 对地电位", "V"), "ae": ("RE 对地电位", "V"),
    "ag": ("WE 对 CE 电位", "V"), "ba": ("WE 电流", "A"), "bb": ("WE2 电流", "A"), "ca": ("相位", "°"),
    "cb": ("阻抗模", "Ω"), "cc": ("阻抗实部", "Ω"), "cd": ("阻抗虚部", "Ω"), "ch": ("交流电位", "Vrms"),
    "ci": ("直流电位", "V"), "cj": ("交流电流", "Arms"), "ck": ("直流电流", "A"), "da": ("设定电位", "V"),
    "db": ("设定电流", "A"), "dc": ("设定频率", "Hz"), "dd": ("设定交流振幅", "Vrms"), "ea": ("通道", ""),
    "eb": ("时间", "s"), "ed": ("CPU 温度", "℃"), "ee": ("计数", ""), "ef": ("板温", "℃"),
    "ja": ("通用 1", ""), "jb": ("通用 2", ""), "jc": ("通用 3", ""), "jd": ("通用 4", ""),
}
# 测量循环开始的那一行 MXXXX 里的技术号（手册表 5）→ 本模块的技术名
TECHNIQUE_IDS = {
    0x0000: "lsv", 0x0001: "dpv", 0x0002: "swv", 0x0003: "npv", 0x0004: "acv", 0x0005: "cv", 0x0006: "scp",
    0x0007: "ca", 0x0008: "pad", 0x0009: "fca", 0x000A: "cp", 0x000B: "ocp", 0x000D: "eis", 0x000E: "geis",
    0x000F: "lsp", 0x0010: "fcv", 0x0011: "ca_alt_mux", 0x0012: "cp_alt_mux", 0x0013: "ocp_alt_mux",
    0x0014: "eis_dual",
}
TECHNIQUE_CODES = {name: code for code, name in TECHNIQUE_IDS.items()}
STATUS_TIMING, STATUS_OVERLOAD, STATUS_UNDERLOAD, STATUS_OVERLOAD_WARNING = 0x1, 0x2, 0x4, 0x8

# 错误码（手册附录 A，用自己的话写的；只列这个网关用得上、或现场最可能碰到的）
ERRORS = {
    0x0001: "未指明的错误", 0x0002: "变量类型不对", 0x0003: "命令不认识", 0x0006: "当前通讯模式下不能用这个命令",
    0x0007: "参数值不对", 0x0008: "命令超长", 0x0009: "命令超时", 0x000C: "没有加载脚本就要运行",
    0x000E: "平均测量值时溢出", 0x000F: "电位不合法", 0x0010: "变量成了 NaN 或无穷", 0x0011: "频率不合法",
    0x0012: "振幅不合法", 0x0014: "电池开着时不能测开路电位", 0x001B: "这台仪器不支持这条命令（或其中一部分）",
    0x001C: "步进电位小于 DAC 的 1 个最低位", 0x001E: "振幅小于 DAC 的 1 个最低位", 0x001F: "这台仪器没有这个技术的许可",
    0x0021: "不支持这个 PGStat 模式", 0x0023: "当前 PGStat 模式下不能用这条命令", 0x0032: "电池严重过载，仪器中止测量以免损坏",
    0x0058: "快速测量时序出错（可能是通讯太慢）", 0x005A: "仪器达不到要求的测量时序", 0x0080: "对电极（CE）振荡",
    0x4001: "脚本命令不认识", 0x4004: "脚本里有意外的字符", 0x4005: "脚本太大，仪器内存放不下",
    0x4008: "这条命令不接受这个可选参数", 0x400B: "测量循环不能嵌套", 0x400C: "当前情况下不能用这条命令",
    0x4012: "脚本出了意外", 0x4018: "脚本意外结束", 0x401A: "测量循环里不能用这条命令", 0x401C: "一个数据包的变量太多",
    0x4020: "脚本命令超时", 0x4026: "变量重复声明", 0x4027: "要先 cell_on", 0x4028: "要先 cell_off",
    0x4029: "这个技术至少要走一步", 0x402B: "变量名不合规（要以 a–z 开头，只能有 a–z、0–9、_）", 0x402C: "变量名太长",
    0x4032: "测量循环里不能改电位", 0x4200: "参数不能是负数", 0x4201: "参数不能是正数", 0x4202: "参数不能是 0",
    0x4203: "参数必须是负数", 0x4204: "参数必须是正数", 0x4205: "参数超出这条命令允许的范围",
    0x4206: "这台仪器不能用这个参数值", 0x4207: "参数类型（浮点 / 整数）不对", 0x4209: "参数的变量类型不对",
    0x420A: "多了一个参数", 0x420B: "参数用的变量没有声明", 0x7FFF: "致命错误，仪器要复位",
}
# 加载脚本时仪器拒收：按错误码分成「不支持」「忙」，其余都是参数 / 脚本不对（invalid）
UNSUPPORTED_ERRORS = {0x001B, 0x001F, 0x0021, 0x4008}
BUSY_ERRORS = {0x0006}

# 各型号的极限（手册附录 B.1、B.2、B.3）。电位范围按 PGStat 模式：2 低速（直流技术用）、3 高速（EIS 必须用）；
# window 是一次测量里能扫的电位跨度（EmStat Pico 的「动态电位窗口」，EmStat4 / Nexus 就是整个范围）
_PICO = {
    "modes": {2: (-1.25, 2.0, 2.2), 3: (-1.7, 2.0, 1.214)},
    "eis_max_hz": 200e3, "eis_max_vrms": 0.429, "i_max_A": 3e-3,
}
_ES4_LR = {"modes": {2: (-3.0, 3.0, 6.0), 3: (-3.0, 3.0, 6.0)}, "eis_max_hz": 200e3, "eis_max_vrms": 0.9,
           "i_max_A": 30e-3}
_ES4_HR = {"modes": {2: (-6.0, 6.0, 12.0), 3: (-6.0, 6.0, 12.0)}, "eis_max_hz": 200e3, "eis_max_vrms": 0.9,
           "i_max_A": 200e-3}
_NEXUS = {"modes": {2: (-10.0, 10.0, 20.0), 3: (-10.0, 10.0, 20.0)}, "eis_max_hz": 1e6, "eis_max_vrms": 0.3,
          "i_max_A": 1.0}
DEVICE_TYPES: dict[str, dict[str, Any]] = {
    "espico": {"model": "EmStat Pico", **_PICO}, "senswb": {"model": "Sensit Wearable", **_PICO},
    "es4_lr": {"model": "EmStat4 LR", **_ES4_LR}, "es4_hr": {"model": "EmStat4 HR", **_ES4_HR},
    "mes4lr": {"model": "MultiEmStat4 LR", **_ES4_LR}, "mes4hr": {"model": "MultiEmStat4 HR", **_ES4_HR},
    "nexus1": {"model": "Nexus", **_NEXUS},
}

_IDENTIFIER = re.compile(r"[a-z][a-z0-9_]*\Z")
_ERROR = re.compile(r"!([0-9A-Fa-f]{4})(?::\s*Line\s+(\d+)(?:,\s*Col\s+(\d+))?)?")
_FLOAT_LITERAL = re.compile(r"([+-]?\d+)([afpnumkMGTPE]?)\Z")
_INT_LITERAL = re.compile(r"([+-]?\d+)i\Z")
_HEX_LITERAL = re.compile(r"0x([0-9A-Fa-f]+)i?\Z")
_BIN_LITERAL = re.compile(r"0b([01]+)i?\Z")


class PackageError(ValueError):
    """数据包写法不对（不是 MethodSCRIPT 的数据包，或读到了半截）。"""


@dataclass(frozen=True)
class Variable:
    """数据包里的一个变量。`value` 是 SI 单位的值（整数类型是 int，无效值是 NaN）。"""

    type: str
    value: float
    status: int = 0
    range: int | None = None
    noise: int | None = None

    @property
    def unit(self) -> str:
        return VAR_TYPES.get(self.type, ("", ""))[1]


# ---------- 数据包 ----------

def decode_value(field: str) -> float:
    """8 个字符（7 位十六进制 + 前缀）→ 值。前缀是空格时可能被行尾裁掉，按 7 个字符也认。"""
    if field == NAN_FIELD or field.strip() == "nan":
        return math.nan
    digits, prefix = field[:7], field[7:8] or " "
    if len(digits) != 7 or not all(char in "0123456789abcdefABCDEF" for char in digits):
        raise PackageError(f"值 {field!r} 不是 7 位十六进制")
    raw = int(digits, 16) - OFFSET
    if prefix == "i":
        return raw
    if prefix not in SI_PREFIXES:
        raise PackageError(f"值 {field!r} 的前缀 {prefix!r} 不认识")
    return raw * SI_PREFIXES[prefix]


def encode_value(value: float, *, integer: bool = False) -> str:
    """值 → 8 个字符（假仪器用）。浮点数挑能放下的最细的前缀（和真仪器一样：0.1 V 写成 n、几十 µA 写成 p）。"""
    if integer:
        number = int(value)
        if abs(number) > PACKAGE_LIMIT:
            return NAN_FIELD
        return f"{number + OFFSET:07X}i"
    if not math.isfinite(value):
        return NAN_FIELD
    if value == 0:
        return f"{OFFSET:07X} "
    for prefix in ("a", "f", "p", "n", "u", "m", " ", "k", "M", "G", "T", "P", "E"):
        scaled = round(value / SI_PREFIXES[prefix])
        if abs(scaled) <= PACKAGE_LIMIT:
            return f"{scaled + OFFSET:07X}{prefix}"
    return NAN_FIELD


def parse_variable(text: str) -> Variable:
    """`ttHHHHHHHp[,MV..]` → Variable。"""
    if len(text) < 9:
        raise PackageError(f"变量 {text!r} 太短")
    kind = text[:2]
    if not ("a" <= kind[0] <= "j" and "a" <= kind[1] <= "z"):
        raise PackageError(f"变量类型 {kind!r} 不认识")
    field, _, rest = text[2:].partition(",")
    value = decode_value(field)
    status, current_range, noise = 0, None, None
    for token in rest.split(",") if rest else []:
        if not token:
            continue
        try:
            if token[0] == "1" and len(token) == 2:
                status = int(token[1], 16)
            elif token[0] == "2" and len(token) == 3:
                current_range = int(token[1:], 16)
            elif token[0] == "4" and len(token) == 2:
                noise = int(token[1], 16)
        except ValueError as exc:
            raise PackageError(f"变量 {text!r} 的元数据 {token!r} 不是十六进制") from exc
        # 不认识的元数据类型跳过：以后的固件可能加新的
    return Variable(kind, value, status, current_range, noise)


def parse_package(line: str) -> list[Variable]:
    """`P...` 一行 → 变量列表。"""
    if not line.startswith("P"):
        raise PackageError(f"数据包要以 P 开头：{line[:40]!r}")
    body = line[1:].rstrip("\r\n")
    if not body:
        raise PackageError("空的数据包")
    return [parse_variable(part) for part in body.split(";")]


def format_package(variables: list[tuple[str, float, int, int | None]]) -> str:
    """假仪器用：[(类型, 值, 状态, 量程号)] → `P...` 一行（不含换行）。"""
    parts = []
    for kind, value, status, current_range in variables:
        text = kind + encode_value(value, integer=isinstance(value, int) and not isinstance(value, bool))
        if status or current_range is not None:
            text += f",1{status:X}"
            if current_range is not None:
                text += f",2{current_range:02X}"
        parts.append(text)
    return "P" + ";".join(parts)


# ---------- 脚本里的数 ----------

def literal(value: float) -> str:
    """浮点数 → 脚本里的写法：整数 + SI 前缀，挑最粗的、能精确写成整数的前缀（0.0001 → 100u，6 → 6，1.5 → 1500m）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{value!r} 不是有限数，写不进脚本")
    if value == 0:
        return "0"
    for digits in (12, 9, 6):
        rounded = float(f"{value:.{digits}g}")
        for prefix in ("T", "G", "M", "k", " ", "m", "u", "n", "p", "f", "a"):
            scaled = rounded / SI_PREFIXES[prefix]
            nearest = round(scaled)
            if nearest == 0 or abs(nearest) > LITERAL_LIMIT:
                continue
            if abs(scaled - nearest) <= 1e-9 * abs(nearest):
                return f"{nearest}{prefix.strip()}"
    raise ValueError(f"{value!r} 写不成「整数 + SI 前缀」")


def integer_literal(value: int) -> str:
    return f"{int(value)}i"


def parse_literal(text: str) -> tuple[float, bool]:
    """假仪器用：脚本里的数 → (值, 是不是整数)。写法不对抛 ValueError。"""
    for pattern, base in ((_HEX_LITERAL, 16), (_BIN_LITERAL, 2)):
        match = pattern.match(text)
        if match:
            return int(match.group(1), base), True
    match = _INT_LITERAL.match(text)
    if match:
        return int(match.group(1)), True
    match = _FLOAT_LITERAL.match(text)
    if match:
        return int(match.group(1)) * SI_PREFIXES[match.group(2) or " "], False
    raise ValueError(f"{text!r} 不是 MethodSCRIPT 的数")


def identifier(name: str) -> bool:
    """变量名：a–z 开头，只有 a–z、0–9、_。"""
    return bool(_IDENTIFIER.match(name))


# ---------- 错误、固件 ----------

@dataclass(frozen=True)
class DeviceError:
    """仪器报的错：`!XXXX`，加载时带行号、列号，运行时只带行号。"""

    code: int
    line: int | None = None
    column: int | None = None

    @property
    def description(self) -> str:
        return ERRORS.get(self.code, "未登记的错误码")

    def text(self, script: list[str] | None = None) -> str:
        where = ""
        if self.line is not None:
            where = f"（脚本第 {self.line} 行" + (f"第 {self.column} 列" if self.column is not None else "")
            if script and 1 <= self.line <= len(script):
                where += f"：{script[self.line - 1].strip()}"
            where += "）"
        return f"!{self.code:04X} {self.description}{where}"

    @property
    def kind(self) -> str:
        """加载时拒收的类别（`Rejected` 的 kind）。"""
        if self.code in UNSUPPORTED_ERRORS:
            return "unsupported"
        if self.code in BUSY_ERRORS:
            return "busy"
        return "invalid"


def parse_error(line: str) -> DeviceError | None:
    """一行里有 `!XXXX` 就是错误（可能带着命令的回显，如 `l!4001: Line 1, Col 27`、`Z!0006`）。"""
    match = _ERROR.search(line)
    if match is None:
        return None
    return DeviceError(int(match.group(1), 16), int(match.group(2)) if match.group(2) else None,
                       int(match.group(3)) if match.group(3) else None)


def parse_firmware(first: str, second: str) -> dict[str, str]:
    """`t` 命令的两行应答 → {device_type, version, build, release}。

        tes4_hr1100#Jan 28 2022 11:04:43      设备类型 6 个字符 + 版本（2 位 x.y 或 4 位 x.y.zz）+ # + 编译时间
        R*                                     R 正式版、B 测试版，* 结束
    """
    if not first.startswith("t") or "#" not in first or not second.endswith("*"):
        raise ValueError(f"固件版本应答不对：{first!r} / {second!r}")
    head, _, build = first[1:].partition("#")
    device_type, digits = head[:6], head[6:]
    if len(digits) == 2 and digits.isdigit():
        version = f"{digits[0]}.{digits[1]}"
    elif len(digits) == 4 and digits.isdigit():
        version = f"{digits[0]}.{digits[1]}.{digits[2:]}"
    else:
        version = digits
    release = {"R": "正式版", "B": "测试版"}.get(second[:1], second[:-1])
    return {"device_type": device_type, "version": version, "build": " ".join(build.split()), "release": release}


def device_info(device_type: str) -> dict[str, Any] | None:
    return DEVICE_TYPES.get(device_type)


def model_matches(model: str, device_type: str) -> bool:
    """配置写的型号和仪器自报的设备类型对不对得上：配置里的每个词都要在仪器型号里（`EmStat4` 认 es4_hr / es4_lr，
    `EmStat4 LR` 不认 es4_hr）。没写型号、或仪器类型不认识时不比。"""
    info = DEVICE_TYPES.get(device_type)
    if not model or info is None:
        return True
    wanted = {word for word in model.lower().split() if word != "palmsens"}
    return wanted <= set(info["model"].lower().split())
