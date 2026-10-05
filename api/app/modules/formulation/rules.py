"""配液模板与实验表格：一张配方表（每行一瓶、每列一种试剂的用量）→ 流程步骤 + 矩阵方案。纯函数，不碰数据库。

配液线的流程骨架是固定的（物料准备段、过渡舱、测试段），会变的只有「加哪几种料、按什么顺序、每瓶多少」：
配方按 AI 预测变，量、组分、顺序都会变。所以模板只描述规则——固定步骤、加料阶段、物料类别 → 加法、
什么时候插搅拌、每次实验可配的参数——每次拿表格现生成：

- 表格列 = 试剂，按表格列顺序逐个生成加料步骤；整列为 0 的不生成；
- 0 是有意不加，空白是没填：同一列有的格填了、有的空着是问题（不加请填 0），整列空白 = 这次不用这种料；
- 搅拌按瓶执行：「加料后搅拌」带 `applies_to`，只处理在前一步真加了料的瓶子（某瓶这种料是 0，加料和随后的搅拌
  一起跳过）；阶段最后一种料加完不搅的阶段里，这瓶之后在本阶段还要再加一种才搅。每瓶的加料与搅拌只由它自己的配方
  决定，不随同批别的瓶子变；
- 类别可以声明 `not_last`：这类料不能是一瓶在本阶段的最后一种（EC 常温是固体，加完要紧接着加下一种溶剂）；
- `volume_check` 按每瓶总质量 ÷ 密度估算母液体积，分装瓶数 × 每瓶分装量 + 母瓶留样放不下就是问题；
- 步骤上只写「投哪种料、用量取哪个参数」（material / material_param），参数值写 0：每瓶的量不属于流程，
  由方案因子按孔位给出——同结构不同量的下一张表因此能沿用同一个已发布流程，不必每次重新评审；
- 流程 BOM 留空，用量全由方案因子给出（因子带 material，建批次时按各瓶之和预留）；
- 方案是矩阵：每行一瓶，完全相同的配方行是同一条件的重复瓶；瓶身序列号按「条件顺序 × 重复号」排进 sample_ids。

结构上可以按线调整（都写在模板配置里，不用改代码）：
- 加料后步骤：类别（`routes.{类别}.stir`）或阶段（`stages[].stir`）可以各写一份，覆盖全局的 `stir`——
  锂盐加完冷藏搅拌、添加剂加完只涡旋，各走各的设备；
- 逐瓶参数列（`row_params`）：表格里除了用量还有别的列（终混温度、分装量），按表头认，每瓶的值成为作用于某个
  固定步骤参数的方案因子，按孔位下发；
- 阶段不串行（`stages[].chain: false`）：这个阶段只接它 `after` 写的步骤，不接上一个阶段的尾巴，后面的阶段或后段
  再把没汇合的尾巴一起接上；
- 加料顺序（`stages[].order`）：`table` 按表格列顺序（缺省），`routes` 按 routes 里类别的先后、同类再按表格顺序；
- 分装量核对优先用物料主数据里登记的密度（换算「1 mL = x g」），没有登记的才用 `volume_check.density`；
  按体积填的列直接算体积。

整任务方式（`task: {step, slots}`）：上位机收整份实验任务、自己调度线内各模组（A-Lab 一类整线）时，流程不再逐种料
生成加料与搅拌步骤，而是由一个固定设备步骤（`task.step`，在 prefix / suffix 里）一步投完一瓶的全部组分：
- 阶段与类别照旧决定加料顺序（阶段先后、阶段内 `order`）与 `not_last` 检查，但只用来排顺序——阶段里不写 then、
  stir，类别不写 step、param、stir：这些动作由上位机按它的工艺执行；
- 这张表要加的料按加料顺序依次占用 `task.slots` 列出的能力参数（加料位 1、2……），步骤上写
  `materials: [{material, param}]`、各参数写 0，每瓶的量照旧由方案因子按孔位给出；
- 同样几种料、同样顺序的下一张表生成的步骤完全一样，沿用同一个已发布流程。

生成结果确定性：同输入同输出（步骤标识按生成顺序 s01、s02…，每一步都写显式 after），服务层据此判断能否沿用已有流程。
"""
from __future__ import annotations

import copy
import math
import re
from decimal import Context, Decimal, InvalidOperation
from typing import Any

from ...domain.matrix import ordered_levels
from ...domain.params import canonical_unit, decimal_of, spec_of, value_issues
from ...domain.steps import DEVICE, kind_of

DEFAULT_SERIAL_HEADERS = ("序列号", "编号", "瓶号", "样品编号", "serial", "id")
MAX_REPEATS = 12
MAX_PLATE = 96
MAX_SERIAL = 64
# 表头的单位后缀：EC (g)、EMC(g)、EC（g）
UNIT_SUFFIX = re.compile(r"^(?P<name>.*?)\s*[(（]\s*(?P<unit>[^()（）]*?)\s*[)）]\s*$")
NAME_LIMIT = 60
# 用量与实验参数的量级上限：远超实验室尺度的数（误贴进来的长编号之类）当问题报，不往下算
MAGNITUDE = Decimal("1e12")
# 阶段内的加料顺序：表格列顺序，或 routes 里类别的先后
ORDERS = ("table", "routes")
QUANTUM = Decimal("0.000001")
# 量化用的精度要够：默认 28 位有效数字时 1e22 以上 quantize 会抛 InvalidOperation
WIDE = Context(prec=60)


# ---------- 小工具 ----------

def header_key(text: Any) -> str:
    """认列名用的比较键：不区分大小写、去掉所有空白。"""
    return re.sub(r"\s+", "", str(text or "")).lower()


def split_header(header: Any, default_unit: str) -> tuple[str, str]:
    """表头 → (试剂名, 单位)。没写单位的按模板缺省单位。"""
    text = str(header or "").strip()
    match = UNIT_SUFFIX.match(text)
    if match and match.group("name").strip():
        return match.group("name").strip(), canonical_unit(match.group("unit")) or canonical_unit(default_unit)
    return text, canonical_unit(default_unit)


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _text(value: Any) -> str:
    """单元格文字。xlsx 里的整数序列号读出来是 1001.0，按 1001 算。"""
    if _blank(value):
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _number(value: Any) -> Decimal | None:
    if isinstance(value, str):
        value = value.strip()
    elif isinstance(value, float) and math.isfinite(value):
        # xlsx 里公式算出的数带二进制浮点尾差（0.1+0.2 = 0.30000000000000004）：按 15 位有效数字取，
        # 与 Excel 显示的一致，不把尾差当成「超出精度」
        value = format(value, ".15g")
    return decimal_of(value)


def _plain(number: Decimal) -> int | float:
    """方案因子水平与设备参数要是 JSON 数字：整数给 int，其余 float（6 位小数以内，与库存精度一致）。"""
    try:
        number = number.quantize(QUANTUM, context=WIDE).normalize(WIDE)
    except InvalidOperation:
        # 调用方先按 MAGNITUDE 拦住了超大数，这里只是兜底：宁可给个近似的 float，也不让请求变成 500
        return float(number)
    return int(number) if number == number.to_integral_value(context=WIDE) else float(number)


def _shown(value: Any) -> str:
    """提示里怎么写一个单元格的值：文字原样，整数 float 不带 .0，其余用 Python 的写法（1e+30）。"""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
        return str(int(value))
    return repr(value)


def _dedupe(items: list[str | None]) -> list[str]:
    out: list[str] = []
    for item in items:
        if item and item not in out:
            out.append(item)
    return out


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def fixed_sections(config: dict) -> list[tuple[str, Any]]:
    """固定步骤按生成顺序分段：prefix → 各阶段的 then → suffix。after 只能引用排在前面的段。"""
    sections: list[tuple[str, Any]] = [("prefix", config.get("prefix", []))]
    for stage in config.get("stages") or []:
        if isinstance(stage, dict):
            sections.append((f"阶段「{stage.get('label') or stage.get('key')}」的 then", stage.get("then", [])))
    sections.append(("suffix", config.get("suffix", [])))
    return sections


def experiment_defaults(config: dict) -> dict[str, Any]:
    return {row["key"]: row.get("default") for row in config.get("experiment_params") or []
            if isinstance(row, dict) and row.get("key")}


# ---------- 模板校验 ----------

def template_issues(
    config: Any, capabilities: dict[str, dict], released_methods: dict[str, dict], metrics: set[str] | dict,
) -> list[str]:
    """模板配置能不能用。`released_methods` 是本组织已发布的设备方法 {id: {capability_id, code, name}}，
    `metrics` 是有效的检测指标 ID。全部问题一次列出，不在第一个问题处停下。"""
    if not isinstance(config, dict):
        return ["模板配置必须是 JSON 对象"]
    issues: list[str] = []
    plate = config.get("plate")
    if not isinstance(plate, int) or isinstance(plate, bool) or not 1 <= plate <= MAX_PLATE:
        issues.append(f"每批样品位 plate 必须是 1–{MAX_PLATE} 的整数")
    if not isinstance(config.get("risk"), str) or not config["risk"].strip():
        issues.append("要填生成流程的风险评估编号 risk（开跑检查要求非空）")
    if not isinstance(config.get("unit"), str) or not canonical_unit(config["unit"]):
        issues.append("要填表格里没写单位时的缺省单位 unit")
    for key in ("design", "sample_type"):
        if key in config and not isinstance(config[key], str):
            issues.append(f"{key} 必须是文字")
    headers = config.get("serial_headers", list(DEFAULT_SERIAL_HEADERS))
    if not isinstance(headers, list) or not headers or not all(isinstance(h, str) and h.strip() for h in headers):
        issues.append("序列号列名 serial_headers 必须是非空文字列表")
    required = config.get("required_metrics")
    if not isinstance(required, list) or not required or not all(isinstance(m, str) for m in required):
        issues.append("至少指定一个必测指标 required_metrics（方案锁定要求）")
    else:
        issues.extend(f"必测指标 {m} 没有登记或已停用" for m in required if m not in metrics)

    def device_issues(step: dict, label: str) -> list[str]:
        problems: list[str] = []
        cap = step.get("cap")
        spec = capabilities.get(cap) if isinstance(cap, str) else None
        if spec is None:
            return [f"{label}的能力 {cap or '（未填）'} 没有登记"]
        if spec.get("retired"):
            problems.append(f"{label}的能力 {cap} 已停用")
        params = step.get("params", {})
        if not isinstance(params, dict):
            problems.append(f"{label}的 params 必须是对象")
        else:
            declared = spec.get("params") or {}
            problems.extend(f"{label}的参数 {key} 不属于能力 {cap}" for key in params if key not in declared)
            for key, value in params.items():
                rule = spec_of(spec, key)
                if rule["type"] in ("enum", "program"):
                    problems.extend(f"{label}的{text}" for text in value_issues(rule, value))
                elif not _is_number(value):
                    problems.append(f"{label}的参数 {key} 必须是数字")
        if "method" in step:
            method = step.get("method")
            ref = method.get("id") if isinstance(method, dict) else None
            found = released_methods.get(ref) if isinstance(ref, str) else None
            if found is None:
                problems.append(f"{label}引用的设备方法 {ref or '（未填）'} 不存在或未发布")
            elif found.get("capability_id") != cap:
                problems.append(
                    f"{label}引用的设备方法 {found.get('code') or ref} 的能力是 {found.get('capability_id')}，与步骤能力 {cap} 不一致"
                )
        return problems

    def step_label(step: dict, fallback: str) -> str:
        return f"「{step.get('name') or step.get('key') or fallback}」"

    def stir_template_issues(step: Any, label: str) -> None:
        if not isinstance(step, dict) or kind_of(step) != DEVICE or step.get("kind") not in (None, "", DEVICE):
            issues.append(f"{label}必须是设备步骤")
        else:
            issues.extend(device_issues(step, label))

    # 固定步骤：key 唯一，after 只引用排在前面的固定步骤
    fixed: dict[str, dict] = {}
    stages = config.get("stages")
    stage_list = stages if isinstance(stages, list) else []
    stage_keys: list[str] = []
    if not isinstance(stages, list) or not stages:
        issues.append("至少要有一个加料阶段 stages")
    # 整任务方式：阶段与类别只排加料顺序，加料、搅拌由上位机做
    task_mode = "task" in config

    def check_fixed(label: str, steps: Any) -> None:
        if not isinstance(steps, list):
            issues.append(f"{label} 必须是步骤列表")
            return
        for position, step in enumerate(steps, start=1):
            where = f"{label}第 {position} 步"
            if not isinstance(step, dict):
                issues.append(f"{where}必须是对象")
                continue
            key = step.get("key")
            if not isinstance(key, str) or not key.strip():
                issues.append(f"{where}缺少局部名 key")
            elif key in fixed:
                issues.append(f"固定步骤 key「{key}」重复")
            if not isinstance(step.get("name"), str) or not step["name"].strip():
                issues.append(f"{where}缺少名称")
            if "after" in step:
                after = step["after"]
                if not isinstance(after, list) or not all(isinstance(ref, str) for ref in after):
                    issues.append(f"固定步骤「{key}」的 after 必须是 key 列表")
                else:
                    issues.extend(
                        f"固定步骤「{key}」的 after 引用了 {ref}：不存在或排在它之后" for ref in after if ref not in fixed
                    )
            if kind_of(step) == DEVICE:
                issues.extend(device_issues(step, f"固定步骤{step_label(step, where)}"))
            if isinstance(key, str) and key.strip() and key not in fixed:
                fixed[key] = step

    check_fixed("prefix", config.get("prefix", []))
    for position, stage in enumerate(stage_list, start=1):
        if not isinstance(stage, dict):
            issues.append(f"第 {position} 个阶段必须是对象")
            continue
        key = stage.get("key")
        if not isinstance(key, str) or not key.strip():
            issues.append(f"第 {position} 个阶段缺少 key")
        elif key in stage_keys:
            issues.append(f"阶段 key「{key}」重复")
        else:
            stage_keys.append(key)
        if task_mode:
            # 整任务方式：阶段只决定加料顺序
            issues.extend(
                f"整任务方式（task）下阶段「{stage.get('label') or key}」只写 key、label、order，{field} 不用写："
                f"加料、搅拌、拧盖这些由上位机按它的工艺做"
                for field in ("after", "then", "stir", "stir_after_last", "chain") if field in stage
            )
            if stage.get("order", "table") not in ORDERS:
                issues.append(f"阶段「{key}」的加料顺序 order 只能是 {'、'.join(ORDERS)}")
            continue
        after = stage.get("after", [])
        if not isinstance(after, list) or not all(isinstance(ref, str) for ref in after):
            issues.append(f"阶段「{key}」的 after 必须是固定步骤 key 列表")
        else:
            issues.extend(f"阶段「{key}」的 after 引用了 {ref}：不存在或排在它之后" for ref in after if ref not in fixed)
        if "stir_after_last" in stage and not isinstance(stage["stir_after_last"], bool):
            issues.append(f"阶段「{key}」的 stir_after_last 只能是是或否")
        if "chain" in stage and not isinstance(stage["chain"], bool):
            issues.append(f"阶段「{key}」的 chain 只能是是或否")
        elif stage.get("chain") is False and position > 1 and not stage.get("after"):
            issues.append(f"阶段「{key}」不接上一个阶段（chain: false），要用 after 写明从哪一步开始")
        if stage.get("order", "table") not in ORDERS:
            issues.append(f"阶段「{key}」的加料顺序 order 只能是 {'、'.join(ORDERS)}")
        if "stir" in stage:
            stir_template_issues(stage["stir"], f"阶段「{stage.get('label') or key}」的加料后步骤")
        check_fixed(f"阶段「{stage.get('label') or key}」的 then", stage.get("then", []))
    check_fixed("suffix", config.get("suffix", []))

    # 物料类别 → 加法
    routes = config.get("routes")
    if not isinstance(routes, dict) or not routes:
        issues.append("至少要有一种物料类别的加法 routes")
        routes = {}
    stage_stir = {stage.get("key"): stage.get("stir") for stage in stage_list if isinstance(stage, dict)}
    stage_last = {stage.get("key"): stage.get("stir_after_last", True) is not False
                  for stage in stage_list if isinstance(stage, dict)}
    # 用得到全局搅拌模板的类别：要搅（本类加完搅，或阶段最后一种加完搅），又没有类别或阶段自己的搅拌模板
    needs_default = False
    for category, route in routes.items():
        label = f"加法「{category}」"
        if not isinstance(route, dict):
            issues.append(f"{label}必须是对象")
            continue
        if route.get("stage") not in stage_keys:
            issues.append(f"{label}的阶段 {route.get('stage') or '（未填）'} 不存在")
        if task_mode:
            issues.extend(_not_last_issue(route, label))
            issues.extend(
                f"整任务方式（task）下{label}只写阶段 stage 与 not_last，{field} 不用写：这类料怎么加由上位机定"
                for field in ("step", "param", "stir", "stir_after") if field in route
            )
            continue
        if "stir_after" in route and not isinstance(route["stir_after"], bool):
            issues.append(f"{label}的 stir_after 只能是是或否")
        if "stir" in route:
            stir_template_issues(route["stir"], f"{label}的加料后步骤")
        stirs = route.get("stir_after", True) is not False or stage_last.get(route.get("stage"), True)
        if stirs and "stir" not in route and not stage_stir.get(route.get("stage")):
            needs_default = True
        issues.extend(_not_last_issue(route, label))
        step = route.get("step")
        if not isinstance(step, dict) or kind_of(step) != DEVICE or step.get("kind") not in (None, "", DEVICE):
            issues.append(f"{label}的步骤模板必须是设备步骤")
            continue
        issues.extend(device_issues(step, f"{label}的步骤模板"))
        param = route.get("param")
        spec = capabilities.get(step.get("cap")) or {}
        if not isinstance(param, str) or not param:
            issues.append(f"{label}缺少用量参数 param")
        elif spec and param not in (spec.get("params") or {}):
            issues.append(f"{label}的用量参数 {param} 不是能力 {step.get('cap')} 的参数")
        elif spec and not spec_of(spec, param)["unit"]:
            issues.append(f"{label}的用量参数 {param} 没有登记单位，无法与表格单位对账")

    stir = config.get("stir")
    if task_mode:
        if stir is not None:
            issues.append("整任务方式（task）下不写搅拌步骤模板 stir：加料后的搅拌由上位机按任务参数做")
        issues.extend(_task_issues(config.get("task"), fixed, capabilities))
    elif stir is not None or needs_default:
        if not isinstance(stir, dict) or kind_of(stir) != DEVICE or stir.get("kind") not in (None, "", DEVICE):
            issues.append("搅拌步骤模板 stir 必须是设备步骤（有类别加完要搅拌、又没写自己的加料后步骤）")
        else:
            issues.extend(device_issues(stir, "搅拌步骤模板"))

    # 每次实验可配的参数 → 方案里的单水平因子，作用到某个设备固定步骤的能力参数
    rows = config.get("experiment_params", [])
    if not isinstance(rows, list):
        issues.append("experiment_params 必须是列表")
        rows = []
    keys: set[str] = set()
    targets: set[tuple[str, str]] = set()
    for position, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            issues.append(f"第 {position} 个实验参数必须是对象")
            continue
        key = row.get("key")
        label = f"实验参数「{row.get('label') or key or position}」"
        if not isinstance(key, str) or not key.strip():
            issues.append(f"第 {position} 个实验参数缺少 key")
        elif key in keys:
            issues.append(f"实验参数 key「{key}」重复")
        else:
            keys.add(key)
        step = fixed.get(row.get("step")) if isinstance(row.get("step"), str) else None
        if step is None or kind_of(step) != DEVICE:
            issues.append(f"{label}指向的 {row.get('step') or '（未填）'} 不是设备固定步骤")
        else:
            declared = (capabilities.get(step.get("cap")) or {}).get("params") or {}
            if row.get("param") not in declared:
                issues.append(f"{label}的参数 {row.get('param') or '（未填）'} 不是能力 {step.get('cap')} 的参数")
            target = (row["step"], str(row.get("param")))
            if target in targets:
                issues.append(f"{label}与别的实验参数作用于同一参数 {row['step']}.{row.get('param')}")
            targets.add(target)
        rule = _experiment_rule(row, fixed, capabilities)
        if rule is not None and rule["type"] == "enum":
            issues.extend(f"{label}的缺省值：{text}" for text in value_issues(rule, row.get("default")))
        elif not _is_number(row.get("default")):
            issues.append(f"{label}要给数字缺省值 default")
    # 逐瓶参数列：表格里的一列 → 某个固定设备步骤的参数，每瓶一个值
    params_rows = config.get("row_params", [])
    if not isinstance(params_rows, list):
        issues.append("row_params 必须是列表")
        params_rows = []
    headers: set[str] = set()
    for position, row in enumerate(params_rows, start=1):
        if not isinstance(row, dict):
            issues.append(f"第 {position} 个逐瓶参数必须是对象")
            continue
        key = row.get("key")
        label = f"逐瓶参数「{row.get('header') or key or position}」"
        if not isinstance(key, str) or not key.strip():
            issues.append(f"第 {position} 个逐瓶参数缺少 key")
        elif key in keys:
            issues.append(f"逐瓶参数 key「{key}」与别的实验参数或逐瓶参数重复")
        else:
            keys.add(key)
        header = row.get("header")
        if not isinstance(header, str) or not header.strip():
            issues.append(f"{label}要写表头 header（表格里这一列的列名）")
        elif header_key(header) in headers:
            issues.append(f"{label}的表头与别的逐瓶参数重复")
        else:
            headers.add(header_key(header))
        step = fixed.get(row.get("step")) if isinstance(row.get("step"), str) else None
        if step is None or kind_of(step) != DEVICE:
            issues.append(f"{label}指向的 {row.get('step') or '（未填）'} 不是设备固定步骤")
            continue
        capability = capabilities.get(step.get("cap")) or {}
        if row.get("param") not in (capability.get("params") or {}):
            issues.append(f"{label}的参数 {row.get('param') or '（未填）'} 不是能力 {step.get('cap')} 的参数")
            continue
        rule = spec_of(capability, row["param"])
        if rule["type"] not in ("number", "integer", "enum"):
            issues.append(f"{label}的参数 {row['param']} 不是数值或选项，表格里一格给不出")
        elif rule["type"] != "enum" and canonical_unit(row.get("unit") or rule["unit"]) != rule["unit"]:
            issues.append(f"{label}的单位 {row.get('unit')} 与参数 {row['param']} 登记的单位 {rule['unit'] or '（未登记）'} 不同")
        target = (row["step"], str(row["param"]))
        if target in targets:
            issues.append(f"{label}与别的实验参数作用于同一参数 {row['step']}.{row['param']}")
        targets.add(target)
        if "default" in row and row["default"] is not None:
            issues.extend(f"{label}的缺省值：{text}" for text in value_issues(rule, row["default"]))
    check = config.get("volume_check")
    if check is not None:
        if not isinstance(check, dict):
            issues.append("volume_check 必须是对象")
        else:
            issues.extend(f"volume_check 的 {key} 要写一个实验参数的 key" for key in ("bottles", "volume")
                          if check.get(key) not in keys)
            if "density" in check and (not _is_number(check.get("density")) or check["density"] <= 0):
                issues.append("volume_check 的 density（物料主数据没登记密度时估算母液体积用，g/mL）必须是正数")
            if "reserve" in check and (not _is_number(check["reserve"]) or check["reserve"] < 0):
                issues.append("volume_check 的 reserve（母瓶至少留多少 mL）必须是不小于 0 的数")
    issues.extend(sop_issues(config))
    return issues


def _not_last_issue(route: dict, label: str) -> list[str]:
    not_last = route.get("not_last")
    if "not_last" in route and not (isinstance(not_last, bool) or (isinstance(not_last, str) and not_last.strip())):
        return [f"{label}的 not_last 只能是是或否，或写明原因的文字"]
    return []


def _task_issues(task: Any, fixed: dict[str, dict], capabilities: dict[str, dict]) -> list[str]:
    """整任务方式：`task.step` 是一个固定设备步骤，`task.slots` 是它的能力里用来放各种料用量的参数（加料位），
    按顺序一种料占一个。加料位都要是登记了单位的数值参数、单位相同——表格里每列的单位都按它核对。"""
    if not isinstance(task, dict):
        return ['task 要写成 {"step": 固定步骤 key, "slots": [加料位参数…]}']
    issues: list[str] = []
    step = fixed.get(task.get("step")) if isinstance(task.get("step"), str) else None
    if step is None or kind_of(step) != DEVICE:
        return [f"task.step 指向的 {task.get('step') or '（未填）'} 不是 prefix / suffix 里的设备固定步骤"]
    label = f"整任务步骤「{step.get('name') or task['step']}」"
    if step.get("material") or step.get("material_param") or step.get("materials"):
        issues.append(f"{label}不用写 material / materials：投哪几种料按表格生成")
    slots = task.get("slots")
    if not isinstance(slots, list) or not slots or not all(isinstance(slot, str) and slot.strip() for slot in slots):
        return [*issues, "task.slots 要列出加料位参数（至少一个）"]
    if len(set(slots)) != len(slots):
        issues.append("task.slots 里有重复的参数")
    capability = capabilities.get(step.get("cap")) if isinstance(step.get("cap"), str) else None
    if capability is None:
        return issues  # 能力没登记已经在固定步骤里报过
    units: set[str] = set()
    for slot in slots:
        if slot not in (capability.get("params") or {}):
            issues.append(f"加料位 {slot} 不是能力 {step.get('cap')} 的参数")
            continue
        rule = spec_of(capability, slot)
        if rule["type"] in ("enum", "program"):
            issues.append(f"加料位 {slot} 不是数值参数")
        elif not rule["unit"]:
            issues.append(f"加料位 {slot} 没有登记单位，无法与表格单位对账")
        else:
            units.add(rule["unit"])
    if len(units) > 1:
        issues.append(f"加料位的单位不一致（{'、'.join(sorted(units))}）：表格各列按同一个单位核对")
    return issues


def task_slots(config: dict, capabilities: dict[str, dict] | None) -> tuple[list[str], str]:
    """整任务方式的加料位与它们的单位（取第一个加料位登记的单位；没给能力表时为空）。"""
    task = config.get("task") if isinstance(config.get("task"), dict) else {}
    slots = [slot for slot in task.get("slots") or [] if isinstance(slot, str) and slot.strip()]
    fixed = {
        step.get("key"): step for _, steps in fixed_sections(config) for step in (steps if isinstance(steps, list) else [])
        if isinstance(step, dict) and step.get("key")
    }
    step = fixed.get(task.get("step")) or {}
    unit = spec_of((capabilities or {}).get(step.get("cap")), slots[0])["unit"] if slots and capabilities else ""
    return slots, unit


def _experiment_rule(row: dict, fixed: dict[str, dict], capabilities: dict[str, dict] | None) -> dict | None:
    """实验参数作用的那个能力参数的规格；指向不明或没给能力表时为 None（按数值处理）。"""
    step = fixed.get(row.get("step")) if isinstance(row.get("step"), str) else None
    if step is None or capabilities is None or kind_of(step) != DEVICE:
        return None
    capability = capabilities.get(step.get("cap"))
    if capability is None or row.get("param") not in (capability.get("params") or {}):
        return None
    return spec_of(capability, row["param"])


def sop_templates(config: dict) -> list[tuple[str, dict]]:
    """模板里会变成流程步骤的所有步骤模板：固定步骤、各类别的加料步骤、搅拌步骤。按 (说明, 步骤) 给出。"""
    rows: list[tuple[str, dict]] = []
    for section, steps in fixed_sections(config):
        for step in steps if isinstance(steps, list) else []:
            if isinstance(step, dict):
                rows.append((f"{section} 的「{step.get('name') or step.get('key')}」", step))
    for category, route in (config.get("routes") or {}).items() if isinstance(config.get("routes"), dict) else []:
        if isinstance(route, dict) and isinstance(route.get("step"), dict):
            rows.append((f"类别「{category}」的加料步骤", route["step"]))
        if isinstance(route, dict) and isinstance(route.get("stir"), dict):
            rows.append((f"类别「{category}」的加料后步骤", route["stir"]))
    for stage in config.get("stages") or [] if isinstance(config.get("stages"), list) else []:
        if isinstance(stage, dict) and isinstance(stage.get("stir"), dict):
            rows.append((f"阶段「{stage.get('label') or stage.get('key')}」的加料后步骤", stage["stir"]))
    if isinstance(config.get("stir"), dict):
        rows.append(("搅拌步骤", config["stir"]))
    return rows


def sop_issues(config: dict) -> list[str]:
    """`sop: {code}` 指定生成的流程按哪份 SOP 执行；步骤模板的 `sop_step` 写这份 SOP 里的步骤标题。
    标题在不在 SOP 里要等生成时按当时生效的版本核对（SOP 会修订），这里只查写法。"""
    issues: list[str] = []
    sop = config.get("sop")
    code = ""
    if sop is not None:
        if not isinstance(sop, dict) or not isinstance(sop.get("code"), str) or not sop["code"].strip():
            issues.append("sop 要写成 {\"code\": \"SOP 编号\"}")
        else:
            code = sop["code"].strip()
    for label, step in sop_templates(config):
        if "sop_step" not in step:
            continue
        if not isinstance(step["sop_step"], str) or not step["sop_step"].strip():
            issues.append(f"{label}的 sop_step 要写 SOP 步骤标题")
        elif not code:
            issues.append(f"{label}写了 sop_step，但模板没有用 sop 指定 SOP")
    return issues


# ---------- 生成 ----------

def _catalog_entry(catalog: dict[str, dict], name: str) -> tuple[str, dict] | None:
    """按名称找试剂。先精确匹配；再按不区分大小写、去空白找唯一一个（EC 与 ec 是同一种料），
    步骤上写的是物料主数据里的原名——批号与预留按名称精确对账。"""
    if name in catalog:
        return name, catalog[name]
    matches = [key for key in catalog if header_key(key) == header_key(name)]
    return (matches[0], catalog[matches[0]]) if len(matches) == 1 else None


def _stage_order(stage: dict, dosed: list[dict], routes: dict) -> list[dict]:
    """一个阶段里要加的料，按实际加料顺序：表格列顺序（缺省），或 `order: routes` 按 routes 里类别的先后、
    同类再按表格顺序（sorted 稳定，dosed 本来就是表格顺序）。生成步骤与 not_last 检查按同一个顺序。"""
    items = [item for item in dosed if item["stage"] == stage.get("key")]
    if stage.get("order") == "routes":
        rank = {category: position for position, category in enumerate(routes)}
        items = sorted(items, key=lambda item: rank.get(item["category"], len(rank)))
    return items


def _not_last_issues(rows: list[dict], stages: list[dict], dosed: list[dict], routes: dict) -> list[str]:
    """按瓶核对类别的 `not_last`：这类料不能是一瓶在本阶段加的最后一种（写了原因就带上原因）。"""
    issues: list[str] = []
    for row in rows:
        for stage in stages:
            added = [item for item in _stage_order(stage, dosed, routes)
                     if row["amounts"].get(item["name"], 0) > 0]
            rule = added[-1]["route"].get("not_last") if added else None
            if rule:
                reason = f"：{rule.strip()}" if isinstance(rule, str) else ""
                issues.append(
                    f"第 {row['row']} 行（{row['serial'] or '无序列号'}）{added[-1]['name']} 之后在"
                    f"「{stage.get('label') or stage.get('key')}」阶段没有再加别的料{reason}"
                )
    return issues


def _density_of(entry: dict | None) -> Decimal | None:
    """物料主数据登记的密度（g/mL）：基础单位是 g 时看「1 mL = x g」，基础单位是 mL 时看「1 g = x mL」。"""
    conversions = (entry or {}).get("conversions") or {}
    base = canonical_unit((entry or {}).get("base_unit"))
    if base == "g" and conversions.get("mL") is not None:
        value = _number(conversions["mL"])
        return value if value is not None and value > 0 else None
    if base == "mL" and conversions.get("g") is not None:
        value = _number(conversions["g"])
        return (Decimal(1) / value) if value is not None and value > 0 else None
    return None


def _volume_issues(
    config: dict, values: dict, reagents: list[dict], rows: list[dict], catalog: dict[str, dict] | None = None,
) -> tuple[list[str], list[str]]:
    """`volume_check`：估算每瓶母液体积，放不下「分装瓶数 × 每瓶分装量 + 母瓶留样」是问题。

    按体积填的列直接算体积；按质量填的列用物料主数据登记的密度（没登记的用模板的 `density`，取偏大的值，
    估出的体积偏小，核对偏保守）。分装瓶数、每瓶分装量可以是实验参数（整批一个值），也可以是逐瓶参数列。
    有列既不是质量也不是体积、或是质量却查不到密度，估算不了，只提醒。
    """
    from ...domain.params import convert, convertible

    check = config.get("volume_check")
    if not isinstance(check, dict) or not rows or not reagents:
        return [], []
    fallback = check.get("density")
    fallback = Decimal(str(fallback)) if _is_number(fallback) and fallback > 0 else None
    reserve = check.get("reserve", 0)
    if not _is_number(reserve):
        return [], []
    per_unit: dict[str, Decimal] = {}
    used_registered = used_fallback = False
    unknown: list[str] = []
    for item in reagents:
        unit = canonical_unit(item["unit"])
        if convertible(unit, "mL"):
            per_unit[item["name"]] = convert(Decimal(1), unit, "mL")
            continue
        if not convertible(unit, "g"):
            unknown.append(f"{item['name']}（{unit or '未写单位'}）")
            continue
        density = _density_of((catalog or {}).get(item["name"]))
        if density is not None:
            used_registered = True
        elif fallback is not None:
            density, used_fallback = fallback, True
        else:
            unknown.append(f"{item['name']}（没有登记密度）")
            continue
        per_unit[item["name"]] = convert(Decimal(1), unit, "g") / density
    if unknown:
        return [], [f"{'、'.join(unknown)} 估算不了体积（物料主数据换算写「1 mL = x g」登记密度），没有核对分装量"]
    all_mass = all(convertible(canonical_unit(item["unit"]), "g") and canonical_unit(item["unit"]) == "g" for item in reagents)
    short: list[str] = []
    sizes: set[tuple] = set()
    for row in rows:
        bottles = (row.get("params") or {}).get(check.get("bottles"), values.get(check.get("bottles")))
        volume = (row.get("params") or {}).get(check.get("volume"), values.get(check.get("volume")))
        if not _is_number(bottles) or not _is_number(volume):
            continue
        need = Decimal(str(bottles)) * Decimal(str(volume)) + Decimal(str(reserve))
        sizes.add((bottles, volume))
        estimate = sum((Decimal(str(amount)) * per_unit[name] for name, amount in row["amounts"].items() if name in per_unit),
                       Decimal(0))
        if estimate <= 0:
            continue  # 一种料都没加的行已单独报
        if need > estimate:
            # 都按质量填时带上总质量，便于和配方表对照；有按体积填的列就只说估出的体积
            mass = sum((Decimal(str(value)) for value in row["amounts"].values()), Decimal(0))
            weight = f"{_plain(mass)} g " if all_mass else ""
            short.append(f"第 {row['row']} 行（{row['serial'] or '无序列号'}）{weight}约 {estimate:.1f} mL"
                         + (f"，要 {_plain(need)} mL" if len(sizes) > 1 else ""))
    if not short:
        return [], []
    shown = "；".join(short[:5]) + (f" 等 {len(short)} 瓶" if len(short) > 5 else "")
    if used_registered and used_fallback:
        basis = f"按物料主数据登记的密度估算，没登记的按 {fallback:g} g/mL"
    elif used_registered:
        basis = "按物料主数据登记的密度估算"
    elif used_fallback:
        basis = f"总质量按 {fallback:g} g/mL 估算"
    else:
        basis = "按体积列直接相加"
    if len(sizes) == 1:
        bottles, volume = next(iter(sizes))
        head = (f"分装 {bottles:g} 瓶 × {volume:g} mL、母瓶至少留 {reserve:g} mL，共要 "
                f"{_plain(Decimal(str(bottles)) * Decimal(str(volume)) + Decimal(str(reserve)))} mL，")
    else:
        head = f"每瓶的分装量加母瓶留样 {reserve:g} mL，"
    return [f"{head}超过母液体积（{basis}）：{shown}；请减少分装瓶数或每瓶分装量，或加大配制量"], []


def generate(
    config: dict, catalog: dict[str, dict], table: list[list[Any]], params: dict[str, Any] | None = None, *,
    capabilities: dict[str, dict] | None = None, filename: str = "", template_name: str = "",
    description: str = "",
) -> dict[str, Any]:
    """按模板把表格生成为流程步骤与方案。`catalog` 是试剂目录 {物料名: {category, base_unit}}，
    `capabilities` 给了就按能力参数登记的单位核对表格单位（没给按模板缺省单位核对）。

    issues 非空就不能导入；warnings 只提醒。问题尽量一次列全，能生成的部分照样给出，便于预览时对照。
    """
    params = dict(params or {})
    issues: list[str] = []
    warnings: list[str] = []
    default_unit = canonical_unit(config.get("unit"))
    routes = config.get("routes") or {}
    # 整任务方式：一个固定设备步骤一步投完一瓶的全部组分（见模块说明）
    task = config.get("task") if isinstance(config.get("task"), dict) else None
    slots, task_unit = task_slots(config, capabilities) if task is not None else ([], "")
    stages = [stage for stage in config.get("stages") or [] if isinstance(stage, dict)]
    stage_label = {stage.get("key"): stage.get("label") or stage.get("key") for stage in stages}
    result: dict[str, Any] = {
        "columns": [], "rows": [], "reagents": [], "steps": [], "bom": [],
        "plan": {}, "recipe": {}, "params": {}, "issues": issues, "warnings": warnings,
    }

    # 实验参数取值：没给的用缺省值
    values: dict[str, int | float | str] = {}
    fixed_steps = {
        step.get("key"): step for _, steps in fixed_sections(config) for step in (steps if isinstance(steps, list) else [])
        if isinstance(step, dict) and step.get("key")
    }
    for row in config.get("experiment_params") or []:
        key = row.get("key")
        raw = params.pop(key, row.get("default"))
        rule = _experiment_rule(row, fixed_steps, capabilities)
        if rule is not None and rule["type"] == "enum":
            # 选项型实验参数（如终混程序）：值是登记的选项之一，原样写进方案
            problems = value_issues(rule, raw)
            if problems:
                issues.extend(f"实验参数「{row.get('label') or key}」：{text}" for text in problems)
            else:
                values[key] = raw
            continue
        number = _number(raw)
        if number is None:
            issues.append(f"实验参数「{row.get('label') or key}」的值 {raw!r} 不是数字")
            continue
        if abs(number) >= MAGNITUDE:
            issues.append(f"实验参数「{row.get('label') or key}」的值 {_shown(raw)} 过大")
            continue
        values[key] = _plain(number)
    warnings.extend(f"实验参数 {key} 模板里没有，已忽略" for key in params)
    result["params"] = values

    table = [list(row) for row in table or []]
    # 表头是第一个非空行。表格保留了文件里的空行（check_limits 只去末尾空行），
    # 所以「第 N 行」= 表里的第 N 行 = Excel / csv 里的第 N 行
    start = next((index for index, row in enumerate(table) if not all(_blank(value) for value in row)), None)
    if start is None:
        issues.append("表格是空的")
        return result
    header, body = table[start], table[start + 1:]
    width = max(len(row) for row in table)
    serial_keys = {header_key(item) for item in config.get("serial_headers") or DEFAULT_SERIAL_HEADERS}

    def cell(row: list, index: int) -> Any:
        return row[index] if index < len(row) else None

    # 列识别
    columns: list[dict[str, Any]] = []
    for index in range(width):
        text = _text(cell(header, index))
        name, unit = split_header(text, default_unit)
        columns.append({"index": index, "header": text, "name": name, "unit": unit, "kind": "ignored",
                        "category": "", "stage": ""})
    serial_columns = [col for col in columns
                      if col["header"] and (header_key(col["header"]) in serial_keys or header_key(col["name"]) in serial_keys)]
    for col in serial_columns:
        col.update(kind="serial", unit="")
    if not serial_columns:
        issues.append(f"没有识别到序列号列：表头要是 {'、'.join(config.get('serial_headers') or DEFAULT_SERIAL_HEADERS)} 之一")
    elif len(serial_columns) > 1:
        issues.append(f"有 {len(serial_columns)} 列都像序列号列（{'、'.join(c['header'] for c in serial_columns)}），只能有一列")
    serial_col = serial_columns[0] if serial_columns else None

    # 逐瓶参数列：按表头认（不区分大小写、去空白，表头里的单位后缀照样认）
    row_param_specs: list[dict[str, Any]] = []
    fixed_steps_by_key = {
        step.get("key"): step for _, steps in fixed_sections(config) for step in (steps if isinstance(steps, list) else [])
        if isinstance(step, dict) and step.get("key")
    }
    for spec_row in config.get("row_params") or []:
        if not isinstance(spec_row, dict) or not spec_row.get("key") or not spec_row.get("header"):
            continue
        match = [col for col in columns if col["kind"] == "ignored" and col["header"]
                 and header_key(col["name"]) == header_key(spec_row["header"])]
        target_step = fixed_steps_by_key.get(spec_row.get("step")) or {}
        rule = spec_of((capabilities or {}).get(target_step.get("cap")), spec_row.get("param")) if capabilities else None
        entry = {**spec_row, "rule": rule, "column": match[0]["index"] if match else None}
        if match:
            match[0].update(kind="param", name=spec_row["header"], unit=canonical_unit(spec_row.get("unit")) or match[0]["unit"])
        elif "default" not in spec_row or spec_row.get("default") is None:
            issues.append(f"没有识别到逐瓶参数列「{spec_row['header']}」：表格要有这一列（或在模板里给它缺省值）")
        row_param_specs.append(entry)

    reagents: list[dict[str, Any]] = []
    for col in columns:
        if col["kind"] in ("serial", "param"):
            continue
        cells = [cell(row, col["index"]) for row in body]
        has_number = any(_number(value) is not None for value in cells)
        if not col["header"]:
            if any(not _blank(value) for value in cells):
                issues.append(f"第 {col['index'] + 1} 列没有表头，但有内容")
            continue
        found = _catalog_entry(catalog, col["name"])
        category = (found[1].get("category") or "") if found else ""
        if not found or not category:
            if has_number:
                issues.append(f"列 {col['header']} 不是已登记的试剂（物料主数据里没有同名物料或没有类别）")
            else:
                warnings.append(f"列 {col['header']} 不是试剂，已忽略")
            continue
        name, entry = found
        col.update(name=name, category=category)
        route = routes.get(category)
        if not isinstance(route, dict):
            if has_number:
                issues.append(f"列 {col['header']}：物料类别「{category}」在模板里没有对应的加法")
            else:
                warnings.append(f"列 {col['header']}：物料类别「{category}」在模板里没有对应的加法，列里也没有用量，已忽略")
            continue
        if any(item["name"] == name for item in reagents):
            issues.append(f"试剂 {name} 出现了两列")
            continue
        col.update(kind="reagent", stage=route.get("stage") or "")
        step = route.get("step") or {}
        expected = default_unit
        if task is not None:
            # 整任务方式：几种料都落在加料位上，按加料位登记的单位核对
            if capabilities is not None:
                expected = task_unit
            if col["unit"] != expected:
                issues.append(
                    f"列 {col['header']} 的单位 {col['unit'] or '（未写）'} 与整任务步骤加料位的单位 "
                    f"{expected or '（未登记）'} 不同"
                )
        else:
            if capabilities is not None:
                expected = spec_of(capabilities.get(step.get("cap") or ""), route.get("param") or "")["unit"]
            if col["unit"] != expected:
                issues.append(
                    f"列 {col['header']} 的单位 {col['unit'] or '（未写）'} 与「{category}」加法的用量参数 "
                    f"{route.get('param')} 的单位 {expected or '（未登记）'} 不同"
                )
        base = canonical_unit(entry.get("base_unit"))
        if base and base != col["unit"]:
            warnings.append(f"{name} 的物料主数据基本单位是 {base}，表格按 {col['unit']}：预留要有以 {col['unit']} 登记的已放行批号")
        reagents.append({"name": name, "category": category, "stage": col["stage"], "unit": col["unit"],
                         "column": col["index"], "route": route})

    # 逐行（每行一瓶）
    rows: list[dict[str, Any]] = []
    first_seen: dict[str, int] = {}
    numeric_serials: list[int] = []
    rounded: list[str] = []
    totals = {item["name"]: Decimal(0) for item in reagents}
    zeros = {item["name"]: 0 for item in reagents}
    # 每列的空白格（行号，整行都空的不算：那一行单独报）与这一列有没有填过的格
    blanks: dict[str, list[int]] = {item["name"]: [] for item in reagents}
    filled: set[str] = set()
    for offset, raw in enumerate(body):
        number = start + offset + 2
        if all(_blank(value) for value in raw):
            continue
        serial_value = cell(raw, serial_col["index"]) if serial_col else None
        serial = _text(serial_value)
        if _is_number(serial_value):
            numeric_serials.append(number)
        where = f"第 {number} 行（{serial or '无序列号'}）"
        if serial_col is not None:
            if not serial:
                issues.append(f"第 {number} 行没有序列号")
            elif serial in first_seen:
                issues.append(f"第 {number} 行的序列号 {serial} 与第 {first_seen[serial]} 行重复")
            elif len(serial) > MAX_SERIAL:
                issues.append(f"第 {number} 行的序列号超过 {MAX_SERIAL} 个字符")
            if serial and serial not in first_seen:
                first_seen[serial] = number
        amounts: dict[str, int | float] = {}
        bad_value = False
        empty = all(_blank(cell(raw, item["column"])) for item in reagents)
        for item in reagents:
            value = cell(raw, item["column"])
            if _blank(value):
                if not empty:
                    blanks[item["name"]].append(number)
            else:
                filled.add(item["name"])
            amount = Decimal(0) if _blank(value) else _number(value)
            if amount is None:
                issues.append(f"{where}{item['name']} 的值 {_text(value)!r} 不是数字")
                amount, bad_value = Decimal(0), True
            elif abs(amount) >= MAGNITUDE:
                issues.append(f"{where}{item['name']} 的值 {_shown(value)} 过大")
                amount, bad_value = Decimal(0), True
            elif amount < 0:
                issues.append(f"{where}{item['name']} 是负数 {_plain(amount)}")
                amount, bad_value = Decimal(0), True
            else:
                # 合计、全 0 判断、因子水平都用同一个量化后的值：否则 1e-9 这种量会生成一个水平全是 0 的加料步骤
                exact = amount
                amount = amount.quantize(QUANTUM, context=WIDE)
                if amount != exact:
                    rounded.append(f"{where}{item['name']} {_shown(value)} → {_plain(amount)}")
            totals[item["name"]] += amount
            zeros[item["name"]] += 1 if amount == 0 else 0
            amounts[item["name"]] = _plain(amount)
        row_values: dict[str, Any] = {}
        for entry in row_param_specs:
            raw_value = cell(raw, entry["column"]) if entry["column"] is not None else None
            label = entry.get("header") or entry["key"]
            if _blank(raw_value):
                if entry.get("default") is None:
                    issues.append(f"{where}{label} 是空白：逐瓶参数每瓶都要写（或在模板里给缺省值）")
                    continue
                raw_value = entry["default"]
            rule = entry.get("rule")
            if rule is not None and rule["type"] == "enum":
                text = _text(raw_value)
                problems = value_issues(rule, text)
                if problems:
                    issues.append(f"{where}{label}：{problems[0]}")
                    continue
                row_values[entry["key"]] = text
                continue
            number = _number(raw_value)
            if number is None or abs(number) >= MAGNITUDE:
                issues.append(f"{where}{label} 的值 {_shown(raw_value)!r} 不是数字")
                continue
            value = _plain(number)
            if rule is not None:
                problems = value_issues(rule, value)
                if problems:
                    issues.append(f"{where}{label}：{problems[0]}")
                    continue
            row_values[entry["key"]] = value
        if serial and reagents and not bad_value and all(amount == 0 for amount in amounts.values()):
            # 有序列号却一种料都不加：多半是预先贴了标签、配方没填。空瓶不能分装和检测，也不能悄悄丢掉这个序列号
            issues.append(f"第 {number} 行（序列号 {serial}）{'没有填任何试剂用量' if empty else '所有试剂都是 0'}")
        rows.append({"row": number, "serial": serial, "amounts": amounts,
                     **({"params": row_values} if row_param_specs else {})})
    for item in reagents:
        # 0 是有意不加，空白是没填：同一列里有的填了、有的空着，多半是漏填，不能替人决定按 0 配
        gaps = blanks[item["name"]]
        if gaps and item["name"] in filled:
            shown = "、".join(str(row) for row in gaps[:10]) + (" 等" if len(gaps) > 10 else "")
            issues.append(f"{item['name']} 列第 {shown} 行是空白：同一列有的填了、有的空着，不加这种料请填 0")
    if rounded:
        # 一格一条会刷屏：合成一条，列前 10 处
        shown = "；".join(rounded[:10]) + (f" 等 {len(rounded)} 处" if len(rounded) > 10 else "")
        warnings.append(f"有用量超出 6 位小数精度，已按 6 位小数计：{shown}")
    if numeric_serials:
        shown = "、".join(str(number) for number in numeric_serials[:10]) + (" 等" if len(numeric_serials) > 10 else "")
        warnings.append(f"第 {shown} 行的序列号是数字单元格，前导零或前缀可能丢失：请核对，或把序列号列设为文本格式")
    if not rows:
        issues.append("表格里没有任何瓶子（表头之后没有数据行）")

    dosed = [item for item in reagents if totals[item["name"]] > 0]
    for item in reagents:
        if rows and totals[item["name"]] == 0:
            warnings.append(f"{item['name']} 全为 0，不生成加料步骤")
    if reagents and rows and not dosed:
        issues.append("表格里没有任何需要加料的试剂")
    elif not reagents and serial_col is not None:
        issues.append("表格里没有识别到任何试剂列")
    issues.extend(_not_last_issues(rows, stages, dosed, routes))
    volume_issues, volume_warnings = _volume_issues(config, values, reagents, rows, catalog)
    issues.extend(volume_issues)
    warnings.extend(volume_warnings)
    result["columns"] = [{key: col[key] for key in ("header", "name", "unit", "kind", "category", "stage")}
                         for col in columns]
    result["rows"] = rows
    result["reagents"] = [{"name": item["name"], "category": item["category"], "stage": item["stage"],
                           "total": _plain(totals[item["name"]]), "zero_rows": zeros[item["name"]]}
                          for item in reagents]

    # 步骤序列：prefix → 各阶段（加料、搅拌、then）→ suffix
    steps: list[dict[str, Any]] = []
    fixed_ids: dict[str, str] = {}
    previous: list[str | None] = [None]
    # (试剂, 投它的步骤, 用量参数)：方案因子按它作用
    doses: list[tuple[dict, str, str]] = []

    def emit(step: dict, after: list[str | None]) -> str:
        step = {key: value for key, value in step.items() if key not in ("key", "after")}
        step_id = f"s{len(steps) + 1:02d}"
        steps.append({**step, "step_id": step_id, "after": _dedupe(after)})
        previous[0] = step_id
        return step_id

    def add_fixed(step: dict, extra: list[str] = (), chain: bool = True) -> None:
        if "after" in step:
            after = [fixed_ids.get(ref) for ref in step.get("after") or []]
        else:
            after = [previous[0]] if chain else []
        fixed_ids[step.get("key")] = emit(copy.deepcopy(step), [*after, *extra])

    if task is not None:
        _task_steps(config, task, slots, stages, dosed, routes, add_fixed, steps, fixed_ids, doses, issues)
        result["steps"] = steps
        return _plan_and_recipe(result, config, values, rows, doses, row_param_specs, fixed_ids, stage_label,
                                stages, issues, template_name, filename, description)
    for step in config.get("prefix") or []:
        add_fixed(step)
    # 阶段接在哪：第一个阶段的第一步写了 stage.after 就只等它们——从 prefix 里分叉出来，
    # prefix 最后那步（如「固体物料到加料位」）另走一路，到后面某个阶段的 stage.after 处汇合（客户流程图就是这样）。
    # 后面的阶段一律接上一个阶段的尾巴（瓶子按阶段顺序走），再加上 stage.after。
    # 阶段写了 chain: false 就只接它的 stage.after，不接上一个阶段的尾巴；没汇合的尾巴由后面的阶段或后段一起接上
    forking = True
    dangling: list[str] = []
    for number, stage in enumerate(stages):
        pending = [fixed_ids[ref] for ref in stage.get("after") or [] if ref in fixed_ids]
        detached = stage.get("chain") is False and number > 0 and bool(pending)
        if detached:
            if previous[0]:
                dangling.append(previous[0])
        elif dangling:
            pending = [*pending, *dangling]
            dangling = []
        stage_doses = _stage_order(stage, dosed, routes)
        dose_ids: list[str] = []
        stirs: list[tuple[dict, int]] = []
        for position, item in enumerate(stage_doses):
            route = item["route"]
            param = route.get("param")
            step = copy.deepcopy(route.get("step") or {})
            step["name"] = str(step.get("name") or "{material} 加料").replace("{material}", item["name"])
            step["consumes_materials"] = True
            step["material"] = item["name"]
            step["material_param"] = param
            # 每瓶的量不属于流程：由方案因子按孔位给出，流程上写 0
            step["params"] = {**(step.get("params") or {}), param: 0}
            step_id = emit(step, [*([] if (forking or detached) and pending else [previous[0]]), *pending])
            pending, forking, detached = [], False, False
            doses.append((item, step_id, param))
            dose_ids.append(step_id)
            last = position == len(stage_doses) - 1
            stir = stage.get("stir_after_last", True) if last else route.get("stir_after", True)
            # 加料后步骤：类别自己的 > 阶段自己的 > 全局的
            template = next((row for row in (route.get("stir"), stage.get("stir"), config.get("stir")) if isinstance(row, dict)),
                            None)
            if stir is not False and template is not None:
                stirring = copy.deepcopy(template)
                stirring["name"] = str(stirring.get("name") or "{material} 加料后搅拌").replace("{material}", item["name"])
                # 按瓶执行：只搅这一步真加了料的瓶子，某瓶这种料是 0 就连搅拌一起跳过
                stirring["applies_to"] = {"dosed": step_id}
                emit(stirring, [step_id])
                stirs.append((steps[-1], position))
        if stage.get("stir_after_last", True) is False:
            # 阶段最后一种料加完不搅，也按瓶算：这瓶之后在本阶段还要再加一种，这一次才搅
            for stirring, position in stirs:
                stirring["applies_to"]["then_any"] = dose_ids[position + 1:]
        for step in stage.get("then") or []:
            # 本阶段没有加料时，stage.after 落到第一个 then 步骤上，分叉规则同上
            add_fixed(step, pending, chain=not ((forking or detached) and pending))
            pending, forking, detached = [], False, False
    for position, step in enumerate(config.get("suffix") or []):
        # 后段第一步把还没汇合的阶段尾巴一起接上
        add_fixed(step, dangling if position == 0 and "after" not in step else [])
    result["steps"] = steps
    return _plan_and_recipe(result, config, values, rows, doses, row_param_specs, fixed_ids, stage_label, stages,
                            issues, template_name, filename, description)


def _task_steps(
    config: dict, task: dict, slots: list[str], stages: list[dict], dosed: list[dict], routes: dict,
    add_fixed: Any, steps: list[dict], fixed_ids: dict[str, str], doses: list, issues: list[str],
) -> None:
    """整任务方式的步骤：只有固定步骤（prefix → suffix），其中 `task.step` 一步投完全部组分。
    这张表要加的料按阶段先后、阶段内的加料顺序依次占用加料位；表格没用到的加料位不写进步骤。"""
    for step in config.get("prefix") or []:
        add_fixed(step)
    for step in config.get("suffix") or []:
        add_fixed(step)
    ordered = [item for stage in stages for item in _stage_order(stage, dosed, routes)]
    if len(ordered) > len(slots):
        issues.append(f"这张表要加 {len(ordered)} 种料，整任务步骤只有 {len(slots)} 个加料位（task.slots）")
    step_id = fixed_ids.get(task.get("step"))
    target = next((row for row in steps if row["step_id"] == step_id), None)
    if target is None:
        return
    pairs = list(zip(ordered, slots))
    target["consumes_materials"] = True
    target["materials"] = [{"material": item["name"], "param": slot} for item, slot in pairs]
    # 每瓶的量不属于流程：由方案因子按孔位给出，流程上写 0
    target["params"] = {**(target.get("params") or {}), **{slot: 0 for _, slot in pairs}}
    doses.extend((item, step_id, slot) for item, slot in pairs)


def _plan_and_recipe(
    result: dict[str, Any], config: dict, values: dict, rows: list[dict], doses: list, row_param_specs: list[dict],
    fixed_ids: dict[str, str], stage_label: dict, stages: list[dict], issues: list[str], template_name: str,
    filename: str, description: str,
) -> dict[str, Any]:
    """方案（每种料一个因子、实验参数与逐瓶参数各一个）与流程草稿的名称、说明。逐种料生成与整任务方式共用。"""
    # 方案：每个加料步骤一个因子（水平 = 各瓶用量去重排序），实验参数各一个单水平因子
    factors: list[dict[str, Any]] = []
    for item, step_id, param in doses:
        levels = sorted({row["amounts"][item["name"]] for row in rows})
        factors.append({
            "name": item["name"], "unit": item["unit"], "levels": levels,
            "target": {"step_id": step_id, "param": param},
            "material": {"name": item["name"], "unit": item["unit"], "per": 1},
        })
    experiment = [row for row in config.get("experiment_params") or [] if row.get("key") in values]
    for row in experiment:
        factors.append({
            "name": row.get("label") or row["key"], "unit": row.get("unit") or "", "levels": [values[row["key"]]],
            "target": {"step_id": fixed_ids.get(row.get("step")), "param": row.get("param")},
        })
    # 逐瓶参数：每个一个因子，水平是各瓶的值，作用于它指向的固定步骤参数（按孔位下发）
    per_row = [entry for entry in row_param_specs if all(entry["key"] in (row.get("params") or {}) for row in rows)]
    for entry in per_row:
        factors.append({
            "name": entry.get("label") or entry.get("header") or entry["key"],
            "unit": "" if (entry.get("rule") or {}).get("type") == "enum" else canonical_unit(entry.get("unit")),
            "levels": ordered_levels((row.get("params") or {})[entry["key"]] for row in rows),
            "target": {"step_id": fixed_ids.get(entry.get("step")), "param": entry.get("param")},
        })
    groups: dict[tuple, list[str]] = {}
    for row in rows:
        point = (tuple(row["amounts"][item["name"]] for item, _, _ in doses)
                 + tuple(values[r["key"]] for r in experiment)
                 + tuple((row.get("params") or {})[entry["key"]] for entry in per_row))
        groups.setdefault(point, []).append(row["serial"])
    counts = [len(serials) for serials in groups.values()]
    repeats = counts[0] if counts else 0
    if len(set(counts)) > 1:
        detail = "、".join(f"C{index:02d} {count} 瓶" for index, count in enumerate(counts, start=1))
        issues.append(f"配方重复数不一致：完全相同的配方行视为重复瓶，每个配方的瓶数要一样（{detail}）")
    if repeats > MAX_REPEATS:
        issues.append(f"每个配方 {repeats} 瓶，超过 {MAX_REPEATS} 次重复上限")
    plate = config.get("plate") or 0
    if groups and len(groups) * repeats > plate:
        issues.append(f"{len(groups)} 个配方 × {repeats} 瓶 = {len(groups) * repeats} 瓶，超过流程每批 {plate} 个样品位")
    result["plan"] = {
        "name": f"{template_name} · {filename}" if template_name else filename,
        "plan_type": "matrix", "factors": factors,
        "design_points": [list(point) for point in groups],
        "repeats": max(1, repeats),
        # 与建批次的对应规则一致：第 i 个样本 = 条件序号 × 重复数 + (重复号 − 1)
        "sample_ids": [serial for serials in groups.values() for serial in serials],
        "required_metrics": list(config.get("required_metrics") or []),
        "goal": f"配方表 {filename} 导入：{len(groups)} 个配方、{len(rows)} 瓶",
    }

    # 流程草稿：名称带试剂顺序，设计说明写模板说明 + 各阶段的加料顺序
    order = "、".join(item["name"] for item, _, _ in doses)
    if len(order) > NAME_LIMIT:
        order = order[:NAME_LIMIT - 1] + "…"
    def stage_sequence(stage: dict) -> str:
        return " → ".join(item["name"] for item, _, _ in doses if item["stage"] == stage.get("key"))

    sequence = "；".join(
        f"{stage_label.get(stage.get('key'))}：{stage_sequence(stage)}" for stage in stages if stage_sequence(stage)
    )
    prefix = (config.get("design") or description or "").strip()
    result["recipe"] = {
        "name": f"{template_name} · {order}" if template_name else order,
        "plate": plate, "risk": config.get("risk") or "",
        "design": "；".join(part for part in (prefix, f"加料顺序 {sequence}" if sequence else "") if part),
    }
    return result
