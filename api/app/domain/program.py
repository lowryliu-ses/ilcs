"""程序表参数：一个参数的值是一张表——充放电工步、升温程序、梯度洗脱（`param_specs` 的 `type: program`）。纯函数。

规格写在能力的 param_specs 里：

    {"type": "program", "max_rows": 50, "columns": [
        {"key": "mode", "label": "工步", "type": "enum", "options": ["恒流充电", "恒压充电", "恒流放电", "静置"],
         "required": true},
        {"key": "current", "label": "电流", "type": "number", "unit": "C"},
        {"key": "voltage", "label": "截止电压", "type": "number", "unit": "V"},
        {"key": "time", "label": "时长", "type": "number", "unit": "min"}]}

值是行的列表，每行 `{列: 值}`，值按列的类型核对；没写的格子就是这一步不用这一列（静置没有电流），标了 required 的列
每行都要写。数值列的格子也可以写 `{"param": "rate"}`：引用本步另一个数值参数。程序的结构固定、要变的量做成本步参数，
方案因子按孔位改它，下发时代进程序表（`resolve`）——设备收到的程序表里只有具体的数，不认识引用。

工位极限：`{列: 极限}`，数值列 [下限, 上限]、选项列是允许的选项。没写的列不约束：工位登记了这个参数就是能跑程序，
列上的极限是额外的安全边界。引用的格子不按列极限查，查的是被引用的那个参数本身（它是普通的能力参数，有自己的极限）。

程序表不能作方案因子的水平、不能取自上游结果、不能当用量参数：要变的就是某一列的某个数，做成参数再引用。
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

PROGRAM = "program"
COLUMN_TYPES = {"number": "数值", "integer": "整数", "enum": "选项"}
MAX_ROWS = 200
MAX_COLUMNS = 20
KEY_LIMIT = 32


def _number(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _numeric(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def is_ref(cell: Any) -> bool:
    """引用本步另一个参数的格子：{"param": "rate"}。"""
    return isinstance(cell, dict) and set(cell) == {"param"} and isinstance(cell.get("param"), str) and bool(cell["param"])


def columns_of(spec: dict[str, Any] | None) -> list[dict[str, Any]]:
    return [row for row in (spec or {}).get("columns") or [] if isinstance(row, dict) and row.get("key")]


def max_rows_of(spec: dict[str, Any] | None) -> int:
    value = (spec or {}).get("max_rows")
    return value if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= MAX_ROWS else MAX_ROWS


def definition_issues(key: str, spec: dict[str, Any]) -> list[str]:
    """能力字典里程序表的列定义是否自洽。"""
    from .params import canonical_unit, option_list_issues

    issues: list[str] = []
    columns = spec.get("columns")
    if not isinstance(columns, list) or not columns:
        return [f"程序表参数 {key} 至少要有一列"]
    if len(columns) > MAX_COLUMNS:
        issues.append(f"程序表参数 {key} 最多 {MAX_COLUMNS} 列")
    seen: set[str] = set()
    for position, column in enumerate(columns, start=1):
        if not isinstance(column, dict):
            issues.append(f"程序表参数 {key} 的第 {position} 列格式不正确")
            continue
        name = column.get("key")
        label = f"程序表参数 {key} 的列 {name or position}"
        if not isinstance(name, str) or not name.strip() or len(name) > KEY_LIMIT or not name.replace("_", "").isalnum():
            issues.append(f"程序表参数 {key} 的第 {position} 列要有标识（字母、数字、下划线，最长 {KEY_LIMIT}）")
        elif name in seen:
            issues.append(f"程序表参数 {key} 的列 {name} 重复")
        else:
            seen.add(name)
        kind = column.get("type") or "number"
        if kind not in COLUMN_TYPES:
            issues.append(f"{label}的类型 {kind} 不受支持（只能是数值、整数或选项）")
        if kind == "enum":
            issues.extend(option_list_issues(f"{label} ", column.get("options")))
            if canonical_unit(column.get("unit")):
                issues.append(f"{label}是选项，没有单位")
        elif column.get("options"):
            issues.append(f"{label}不是选项列，不写选项")
        if "required" in column and not isinstance(column["required"], bool):
            issues.append(f"{label}的「必填」只能是是或否")
    rows = spec.get("max_rows")
    if rows is not None and (not isinstance(rows, int) or isinstance(rows, bool) or not 1 <= rows <= MAX_ROWS):
        issues.append(f"程序表参数 {key} 的最多行数要是 1–{MAX_ROWS} 的整数")
    if spec.get("unit"):
        issues.append(f"程序表参数 {key} 本身没有单位（单位写在列上）")
    return issues


def clean_definition(spec: dict[str, Any]) -> dict[str, Any]:
    """入库前的写法：列的单位收成规范写法，缺省值不存。"""
    from .params import canonical_unit

    columns = []
    for column in spec.get("columns") or []:
        if not isinstance(column, dict) or not column.get("key"):
            continue
        row: dict[str, Any] = {"key": str(column["key"]).strip(), "label": str(column.get("label") or column["key"]).strip()}
        kind = column.get("type") or "number"
        if kind != "number":
            row["type"] = kind
        if kind == "enum":
            row["options"] = [str(item).strip() for item in column.get("options") or [] if str(item).strip()]
        elif canonical_unit(column.get("unit")):
            row["unit"] = canonical_unit(column.get("unit"))
        if column.get("required") is True:
            row["required"] = True
        columns.append(row)
    out: dict[str, Any] = {"type": PROGRAM, "columns": columns}
    if isinstance(spec.get("max_rows"), int) and not isinstance(spec.get("max_rows"), bool):
        out["max_rows"] = spec["max_rows"]
    if spec.get("required") is False:
        out["required"] = False
    return out


def _cell_issues(column: dict[str, Any], cell: Any, where: str) -> list[str]:
    kind = column.get("type") or "number"
    label = f"{where}的{column.get('label') or column['key']}"
    if kind == "enum":
        options = column.get("options") or []
        return [] if isinstance(cell, str) and cell in options else [f"{label}只能是 {'、'.join(options)} 之一（现在是 {cell!r}）"]
    if is_ref(cell):
        return []  # 引用本步参数：指向谁、单位对不对由 ref_issues 按能力核
    number = _number(cell) if not isinstance(cell, str) else None
    if number is None:
        return [f"{label}必须是数值"]
    if kind == "integer" and number != number.to_integral_value():
        return [f"{label}必须是整数"]
    return []


def value_issues(spec: dict[str, Any], value: Any, label: str) -> list[str]:
    """一张程序表是否符合列定义：行数、每格的类型与选项、必填列。"""
    if not isinstance(value, list):
        return [f"{label} 是程序表，要写成行的列表"]
    if not value:
        return [f"{label} 至少要有一行"]
    limit = max_rows_of(spec)
    issues: list[str] = []
    if len(value) > limit:
        issues.append(f"{label} 最多 {limit} 行（现在 {len(value)} 行）")
    columns = {column["key"]: column for column in columns_of(spec)}
    for number, row in enumerate(value, start=1):
        where = f"{label}第 {number} 行"
        if not isinstance(row, dict):
            issues.append(f"{where}格式不正确")
            continue
        issues.extend(f"{where}的列 {key} 不在程序表的列定义里" for key in row if key not in columns)
        for key, column in columns.items():
            cell = row.get(key)
            if cell is None or cell == "":
                if column.get("required"):
                    issues.append(f"{where}的{column.get('label') or key}必填")
                continue
            issues.extend(_cell_issues(column, cell, where))
    return issues


def refs(value: Any) -> set[str]:
    """程序表里引用到的本步参数名。"""
    found: set[str] = set()
    for row in value if isinstance(value, list) else []:
        for cell in (row or {}).values() if isinstance(row, dict) else []:
            if is_ref(cell):
                found.add(cell["param"])
    return found


def ref_issues(spec: dict[str, Any], value: Any, capability: dict[str, Any] | None, label: str, key: str) -> list[str]:
    """引用的格子：只能引用本能力的数值参数（不能引用自己、选项或别的程序表），单位要与列相同。"""
    from .params import canonical_unit, spec_of

    issues: list[str] = []
    declared = (capability or {}).get("params") or {}
    columns = {column["key"]: column for column in columns_of(spec)}
    for number, row in enumerate(value if isinstance(value, list) else [], start=1):
        for column_key, cell in (row.items() if isinstance(row, dict) else []):
            if not is_ref(cell) or column_key not in columns:
                continue
            column = columns[column_key]
            where = f"{label}第 {number} 行的{column.get('label') or column_key}"
            target = cell["param"]
            if (column.get("type") or "number") == "enum":
                issues.append(f"{where}是选项列，不能引用参数")
                continue
            if target == key or target not in declared:
                issues.append(f"{where}引用的参数 {target} 不是本能力的另一个参数")
                continue
            referenced = spec_of(capability, target)
            if referenced["type"] not in ("number", "integer"):
                issues.append(f"{where}引用的 {referenced['label']} 不是数值参数")
                continue
            if canonical_unit(column.get("unit")) != referenced["unit"]:
                issues.append(
                    f"{where}的单位是 {column.get('unit') or '（未登记）'}，引用的 {referenced['label']} 是 "
                    f"{referenced['unit'] or '（未登记）'}：数值原样代入，单位要相同"
                )
    return issues


def fits(value: Any, window: Any) -> bool:
    """程序表的字面格子是否都落在工位的列极限里；没写极限的列不约束，引用的格子由被引用的参数自己的极限管。"""
    from .params import window_fits

    if not isinstance(value, list) or not isinstance(window, dict):
        return False
    for row in value:
        if not isinstance(row, dict):
            return False
        for column_key, cell in row.items():
            limit = window.get(column_key)
            if limit is None or is_ref(cell) or cell is None or cell == "":
                continue
            if not window_fits(cell, limit):
                return False
    return True


def misfits(value: Any, window: Any) -> list[str]:
    """哪几格超出了列极限（给人看）。"""
    from .params import window_fits

    if not isinstance(window, dict):
        return ["工位极限没有按列写"]
    reasons: list[str] = []
    for number, row in enumerate(value if isinstance(value, list) else [], start=1):
        for column_key, cell in (row.items() if isinstance(row, dict) else []):
            limit = window.get(column_key)
            if limit is None or is_ref(cell) or cell is None or cell == "":
                continue
            if not window_fits(cell, limit):
                shown = "、".join(map(str, limit)) if all(isinstance(item, str) for item in limit) else f"[{limit[0]}, {limit[1]}]"
                reasons.append(f"第 {number} 行 {column_key}={cell} 超出 {shown}")
    return reasons


def limit_issues(spec: dict[str, Any], window: Any, name: str) -> list[str]:
    """工位上程序表参数的极限：{列: 极限}，数值列 [下限, 上限]，选项列是允许的选项；只能写定义了的列。"""
    from .params import limit_issues as single

    if not isinstance(window, dict):
        return [f"{name} 是程序表参数，极限要按列写：{{列: 极限}}"]
    columns = {column["key"]: column for column in columns_of(spec)}
    issues: list[str] = []
    for column_key, limit in window.items():
        column = columns.get(column_key)
        if column is None:
            issues.append(f"{name} 没有列 {column_key}")
            continue
        column_spec = {
            "label": column.get("label") or column_key, "type": column.get("type") or "number",
            "options": column.get("options") or [], "unit": column.get("unit") or "", "required": False,
        }
        issues.extend(single(column_spec, limit, f"{name}.{column_key}"))
    return issues


def resolve(value: list[dict[str, Any]], params: dict[str, Any]) -> list[dict[str, Any]]:
    """把引用代成本步参数的值：设备收到的程序表里只有具体的数。引用的参数没有值抛 KeyError。"""
    out: list[dict[str, Any]] = []
    for row in value:
        resolved: dict[str, Any] = {}
        for key, cell in row.items():
            if is_ref(cell):
                if cell["param"] not in params or params[cell["param"]] in (None, ""):
                    raise KeyError(cell["param"])
                resolved[key] = params[cell["param"]]
            else:
                resolved[key] = cell
        out.append(resolved)
    return out


def is_program(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(row, dict) for row in value)


def resolve_command(params: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """下发前把指令里每张程序表的引用代成具体的数：设备不认识引用。

    被引用的参数按孔位不同（方案因子、前馈写在 `params.wells` 里）时，每个孔位各代一份，写进 `params.wells[孔位][程序表]`；
    整批一个值的代进顶层。代不出来（引用的参数没有值）的列进问题，调用方据此不下发。"""
    wells = params.get("wells") if isinstance(params.get("wells"), dict) else {}
    out = dict(params)
    problems: list[str] = []
    for key, value in params.items():
        if key == "wells" or not is_program(value) or not refs(value):
            continue
        used = refs(value)
        try:
            out[key] = resolve(value, params)
        except KeyError as missing:
            problems.append(f"程序表 {key} 引用的参数 {missing.args[0]} 没有值")
            continue
        varying = {well: overrides for well, overrides in wells.items() if isinstance(overrides, dict) and used & set(overrides)}
        if varying:
            per_well = {well: dict(overrides) for well, overrides in (out.get("wells") or {}).items()}
            for well, overrides in varying.items():
                try:
                    per_well.setdefault(well, {})[key] = resolve(value, {**params, **overrides})
                except KeyError as missing:
                    problems.append(f"孔位 {well} 的程序表 {key} 引用的参数 {missing.args[0]} 没有值")
            out["wells"] = per_well
    return out, problems


def summary(value: Any, spec: dict[str, Any] | None = None) -> str:
    """一行说明：几步、按第一个选项列（通常是工步类型）列出。"""
    if not isinstance(value, list) or not value:
        return "空程序表"
    first = next((column["key"] for column in columns_of(spec) if column.get("type") == "enum"), None)
    names = [str(row.get(first)) for row in value if isinstance(row, dict) and first and row.get(first)]
    shown = " → ".join(names[:6]) + (" …" if len(names) > 6 else "")
    return f"{len(value)} 步" + (f"：{shown}" if shown else "")
