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
    name: str = "", dur_min: Any = 0,
) -> list[str]:
    """方法定义本身的问题。发布前必须为空。"""
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
        lo, hi, default = _num(rule.get("min")), _num(rule.get("max")), _num(rule.get("default"))
        if lo is not None and hi is not None and lo > hi:
            issues.append(f"参数 {key} 下限 {lo:g} 大于上限 {hi:g}")
        if default is not None and ((lo is not None and default < lo) or (hi is not None and default > hi)):
            issues.append(f"参数 {key} 缺省值 {default:g} 不在 [{rule.get('min')}, {rule.get('max')}] 内")
    seen: set[str] = set()
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
        defaults = {
            key: _num(rule.get("default")) for key, rule in spec.params.items() if _num(rule.get("default")) is not None
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
