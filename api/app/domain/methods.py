"""设备方法的纯规则：方法定义是否完整、流程步骤引用方法时的解析与冻结。

「流程负责做什么，方法负责怎么做」：流程的设备步骤写能力与本步骤要改的参数；引用一条设备方法后，
- 能力必须与方法一致；
- 没写的参数取方法缺省值，写了的必须落在方法允许的范围内；
- 只有方法适用的仪器型号、且驱动自报支持该设备端程序的工位才能承接（见 `capability.station_fits`）；
- 建批次时方法内容（编号、版本、程序、输出规则）冻结进步骤快照，指令带着程序下发。

引用只接受已发布的方法；方法修订发布后旧版本退役，引用旧版本的流程要改引用并重新评审——
和子流程一样，不在一个已批准的流程里悄悄换掉怎么做。

纯函数：方法怎么取由调用方传入的 `resolve` 决定。
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Callable

from .steps import DEVICE, kind_of, step_id_of

STATES = ("draft", "released", "retired")


@dataclass(frozen=True)
class MethodSpec:
    id: str
    code: str
    version: int
    name: str
    capability_id: str
    state: str
    program: str = ""
    instrument_models: tuple[str, ...] = ()
    params: dict[str, dict[str, Any]] = field(default_factory=dict)
    outputs: tuple[dict[str, Any], ...] = ()
    dur_min: float = 0
    latest_version: int = 0


Resolver = Callable[[str], "MethodSpec | None"]


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def definition_issues(
    capability_id: str, params: dict[str, Any], outputs: list[Any], capabilities: dict[str, dict],
    name: str = "", dur_min: Any = 0, metrics: dict[str, dict] | None = None,
) -> list[str]:
    """方法定义本身的问题。发布前必须为空。

    `metrics` 是本组织的指标定义 {id: {code, unit, value_type, state}}：输出项关联了指标时，指标要存在、在用、
    是数值型、单位与输出项相同——设备回报的数是按输出项的单位原样写成结果的，单位不同就差出倍数。没给就不查关联。
    """
    issues: list[str] = []
    if not str(name or "").strip():
        issues.append("方法名称为空")
    capability = capabilities.get(capability_id)
    if capability is None:
        issues.append(f"能力 {capability_id or '（未选）'} 不存在")
    elif capability.get("retired"):
        issues.append(f"能力 {capability_id} 已停用")
    known = set((capability or {}).get("params") or {})
    for key, rule in (params or {}).items():
        if not isinstance(rule, dict):
            issues.append(f"参数 {key} 定义格式不对")
            continue
        if capability is not None and key not in known:
            issues.append(f"参数 {key} 不是能力 {capability_id} 的参数（可用：{'、'.join(sorted(known)) or '无'}）")
        if capability is not None and key in known:
            from .params import spec_of

            if spec_of(capability, key)["type"] == "enum":
                issues.extend(_enum_rule_issues(key, rule, spec_of(capability, key)))
                continue
        lo, hi, default = _num(rule.get("min")), _num(rule.get("max")), _num(rule.get("default"))
        if lo is not None and hi is not None and lo > hi:
            issues.append(f"参数 {key} 下限 {lo:g} 大于上限 {hi:g}")
        if default is not None and ((lo is not None and default < lo) or (hi is not None and default > hi)):
            issues.append(f"参数 {key} 缺省值 {default:g} 不在 [{rule.get('min')}, {rule.get('max')}] 内")
        if capability is not None:
            from .params import canonical_unit, spec_of, value_issues

            spec = spec_of(capability, key)
            if default is not None:
                issues.extend(f"参数 {key} 的缺省值：{text}" for text in value_issues(spec, default))
            unit = canonical_unit(rule.get("unit"))
            if unit and spec["unit"] and unit != spec["unit"]:
                issues.append(f"参数 {key} 的单位 {unit} 与能力登记的单位 {spec['unit']} 不同：设定值是原样下发的")
    seen: set[str] = set()
    linked: dict[str, str] = {}
    for row in outputs or []:
        key = str((row or {}).get("key") or "").strip() if isinstance(row, dict) else ""
        if not key:
            issues.append("输出规则有一行没有指标键")
            continue
        if key in seen:
            issues.append(f"输出规则 {key} 重复")
        seen.add(key)
        lo, hi = _num(row.get("lo")), _num(row.get("hi"))
        if lo is not None and hi is not None and lo > hi:
            issues.append(f"输出 {key} 下限 {lo:g} 大于上限 {hi:g}")
        metric_id = str(row.get("metric_id") or "").strip()
        if metric_id and metrics is not None:
            from .params import canonical_unit

            metric = metrics.get(metric_id)
            if metric is None:
                issues.append(f"输出 {key} 关联的指标 {metric_id} 不存在")
            elif metric.get("state") != "active":
                issues.append(f"输出 {key} 关联的指标 {metric.get('code')} 已停用")
            elif metric.get("value_type") != "number":
                issues.append(f"输出 {key} 关联的指标 {metric.get('code')} 不是数值型：设备回报的是数")
            elif canonical_unit(row.get("unit")) != canonical_unit(metric.get("unit")):
                issues.append(
                    f"输出 {key} 的单位 {row.get('unit') or '（未填）'} 与指标 {metric.get('code')} 的单位 "
                    f"{metric.get('unit') or '（未填）'} 不同：结果按输出项的单位原样入库"
                )
            if metric_id in linked:
                issues.append(f"输出 {key} 与 {linked[metric_id]} 关联了同一个指标：一个样本每个指标只有一条当前结果")
            linked[metric_id] = key
    duration = _num(dur_min)
    if duration is not None and duration < 0:
        issues.append("缺省时长不能为负")
    return issues


def method_ref(step: dict[str, Any]) -> str:
    method = step.get("method")
    return str(method.get("id") or "") if isinstance(method, dict) else ""


def references(steps: list[dict[str, Any]]) -> list[str]:
    return [ref for ref in (method_ref(step) for step in steps or [] if isinstance(step, dict)) if ref]


def snapshot(spec: MethodSpec) -> dict[str, Any]:
    """冻结进步骤的方法内容。指令、结果判定都只看这份，不回头查方法表。"""
    return {
        "id": spec.id, "code": spec.code, "version": spec.version, "name": spec.name,
        "capability_id": spec.capability_id, "program": spec.program,
        "instrument_models": list(spec.instrument_models),
        "params": copy.deepcopy(spec.params), "outputs": copy.deepcopy(list(spec.outputs)),
    }


def _enum_rule_issues(key: str, rule: dict, spec: dict) -> list[str]:
    """选项型参数的方法规则：可以收窄允许的选项（`options`，要是能力登记选项的子集），缺省值是其中之一；没有上下限与单位。"""
    issues: list[str] = []
    if rule.get("min") not in (None, "") or rule.get("max") not in (None, ""):
        issues.append(f"参数 {key} 是选项型，不写上下限，用 options 列允许的选项")
    if rule.get("unit"):
        issues.append(f"参数 {key} 是选项型，没有单位")
    allowed = rule.get("options")
    if allowed not in (None, []):
        if not isinstance(allowed, list) or not all(isinstance(item, str) for item in allowed):
            issues.append(f"参数 {key} 的允许选项必须是文字列表")
            allowed = []
        else:
            unknown = [item for item in allowed if item not in spec["options"]]
            if unknown:
                issues.append(f"参数 {key} 的允许选项 {'、'.join(unknown)} 不是能力登记的选项")
    default = rule.get("default")
    if default not in (None, ""):
        choices = allowed or spec["options"]
        if not isinstance(default, str) or default not in choices:
            issues.append(f"参数 {key} 缺省值 {default!r} 不在允许的选项 {'、'.join(choices)} 里")
    return issues


def rule_default(rule: dict) -> Any:
    """方法规则的缺省值：数值参数取数，选项型参数取文字；没有返回 None。"""
    default = (rule or {}).get("default")
    if isinstance(default, str) and default.strip() and _num(default) is None:
        return default
    return _num(default)


def step_problems(step: dict[str, Any], spec: MethodSpec | None) -> list[str]:
    ref = method_ref(step)
    if not ref:
        return []
    if spec is None:
        return [f"引用的设备方法 {ref} 不存在"]
    label = f"{spec.code} v{spec.version}（{spec.name}）"
    problems: list[str] = []
    if spec.state == "draft":
        problems.append(f"设备方法 {label} 还是草稿：只能引用已发布的方法")
    elif spec.state == "retired":
        newer = f"，请改引用 v{spec.latest_version}" if spec.latest_version > spec.version else ""
        problems.append(f"设备方法 {label} 已退役{newer}")
    if step.get("cap") and step.get("cap") != spec.capability_id:
        problems.append(f"步骤能力 {step.get('cap')} 与设备方法的能力 {spec.capability_id} 不一致")
    for key, value in (step.get("params") or {}).items():
        rule = spec.params.get(key)
        if rule is None and not spec.params:
            continue  # 方法没有参数表：步骤参数只受工位极限约束
        if rule is None:
            problems.append(f"参数 {key} 不在设备方法 {spec.code} 的参数表里")
            continue
        if isinstance(value, str):
            allowed = rule.get("options") or []
            if allowed and value not in allowed:
                problems.append(f"参数 {key}={value} 不在设备方法允许的选项 {'、'.join(allowed)} 里")
            continue
        number = _num(value)
        lo, hi = _num(rule.get("min")), _num(rule.get("max"))
        if number is None:
            continue
        if (lo is not None and number < lo) or (hi is not None and number > hi):
            problems.append(f"参数 {key}={number:g} 超出设备方法允许的 [{rule.get('min')}, {rule.get('max')}]")
    return problems


def apply(
    steps: list[dict[str, Any]], resolve: Resolver,
) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    """把方法引用解析进步骤：补缺省参数、写入方法快照。返回（解析后的步骤, 按步骤标识的问题）。

    有问题的引用也照样补上能取到的内容，让校验能继续往下报工位匹配等问题；是否放行由调用方按问题裁决。
    """
    out: list[dict[str, Any]] = []
    problems: dict[str, list[str]] = {}
    cache: dict[str, MethodSpec | None] = {}
    for index, step in enumerate(steps or []):
        ref = method_ref(step) if kind_of(step) == DEVICE else ""
        if not ref:
            out.append(step)
            continue
        if ref not in cache:
            cache[ref] = resolve(ref)
        spec = cache[ref]
        found = step_problems(step, spec)
        if found:
            problems[step_id_of(step, index)] = found
        if spec is None:
            out.append(step)
            continue
        row = copy.deepcopy(step)
        row["cap"] = step.get("cap") or spec.capability_id
        # 取自上游结果的参数不补缺省值：它在下发时才有值，补一个固定值等于绕过前馈
        bound = set((step.get("bindings") or {}) if isinstance(step.get("bindings"), dict) else ())
        defaults = {
            key: rule_default(rule) for key, rule in spec.params.items()
            if rule_default(rule) is not None and key not in bound
        }
        row["params"] = {**defaults, **(step.get("params") or {})}
        if not step.get("dur") and spec.dur_min:
            row["dur"] = spec.dur_min
        row["method"] = snapshot(spec)
        out.append(row)
    return out, problems


def command_method(step: dict[str, Any]) -> dict[str, Any]:
    """指令上带的方法信息：驱动据此选设备端程序；审计、结果追溯据此知道用的是哪一版方法。"""
    method = step.get("method") if isinstance(step.get("method"), dict) else {}
    if not method.get("code"):
        return {}
    return {key: method.get(key) for key in ("id", "code", "version", "name", "program")}


def station_allows(model: str, programs: tuple[str, ...], step: dict[str, Any]) -> list[str]:
    """工位能不能按步骤引用的方法执行：型号在适用清单里、驱动自报过的程序目录包含该程序。

    驱动没报过方法目录（空）时不据此排除：老设备、协议带不了目录的设备照常可用，程序由现场配置保证。
    """
    method = step.get("method") if isinstance(step.get("method"), dict) else {}
    reasons: list[str] = []
    models = [str(value) for value in method.get("instrument_models") or [] if str(value).strip()]
    if models and model not in models:
        reasons.append(f"型号 {model or '未登记'} 不在方法适用型号 {'、'.join(models)} 内")
    program = str(method.get("program") or "")
    if program and programs and "*" not in programs and program not in programs:
        reasons.append(f"设备未报告支持程序 {program}")
    return reasons
