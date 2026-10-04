#!/usr/bin/env python
"""从当前 FastAPI 应用生成稳定的 OpenAPI 契约快照。"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "api"))

from app.modules import AVAILABLE  # noqa: E402

# 契约覆盖全部可选模块，不随本机 ILCS_MODULES 变
os.environ["ILCS_MODULES"] = ",".join(AVAILABLE)

from app.main import app  # noqa: E402


def main() -> int:
    target = ROOT / "api" / "openapi.json"
    target.write_text(
        json.dumps(app.openapi(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"已生成 {target}（{len(app.openapi().get('paths', {}))} 个路径）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
