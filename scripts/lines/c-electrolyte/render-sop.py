#!/usr/bin/env python
"""按 sop.json（或给的另一份，如整任务方式的 sop-alab.json）生成配液线 SOP 草案的附件 PDF（与演示 SOP 同一版式）。

    api/.venv/bin/python scripts/lines/c-electrolyte/render-sop.py [sop-alab.json]

生成的 SOP-ELY-01-<版本>.pdf 随仓库提交，load-electrolyte-line.py 登记 SOP 时上传它（导入脚本只用标准库，
不在部署机上生成 PDF）。改了 sop.json 就升版本号、重新生成并提交；旧版本的 PDF 已随那一版登记进系统，仓库里删掉。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2] / "api"))

from app.seed.sop_document import render  # noqa: E402

NOTE = "产线草案（按产线定义、客户确认的工艺与配液经验起草，工艺规则待客户确认，现场审定前以现场规程为准）"


def main() -> int:
    source = HERE / (sys.argv[1] if len(sys.argv) > 1 else "sop.json")
    row = json.loads(source.read_text(encoding="utf-8"))
    target = HERE / f"{row['code']}-{row['version']}.pdf"
    target.write_bytes(render(row, "QA", note=NOTE))
    print(f"已生成 {target}（{target.stat().st_size} 字节）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
