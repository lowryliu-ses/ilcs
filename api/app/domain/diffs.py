"""版本快照的字段级差异。方案、SOP 用同一套：逐字段比较，列出改了什么，旧值 → 新值。"""
from __future__ import annotations

import json
from typing import Any


def _text(value: Any) -> str:
    if value is None or value == "" or value == [] or value == {}:
        return "—"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def diff(before: dict, after: dict, labels: dict[str, str], ignore: tuple[str, ...] = ()) -> list[dict]:
    rows = []
    keys = [key for key in labels if key not in ignore]
    keys += [key for key in sorted(set(before) | set(after)) if key not in labels and key not in ignore]
    for key in keys:
        old, new = before.get(key), after.get(key)
        if _text(old) == _text(new):
            continue
        rows.append({"field": key, "label": labels.get(key, key), "before": _text(old), "after": _text(new)})
    return rows
