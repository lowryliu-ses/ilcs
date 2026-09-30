"""演示 SOP 的附件：按种子里写的目的、安全要求与结构化步骤生成一份 PDF。

只给演示库用。正式 SOP 由编写人上传经评审的受控文件，不由系统生成。
"""
from __future__ import annotations

import io

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from ..services.report_pdf import Page, _ensure_font

KIND_LABEL = {"device": "设备", "manual": "人工", "wait": "等待", "review": "审核"}


DEMO_NOTE = "演示受控文件（由种子生成，非正式 SOP）"


def render(row: dict, owner_name: str, note: str = DEMO_NOTE) -> bytes:
    """`note` 印在页眉版本号后面，说明这份文件的来历（演示种子、产线草案……），不冒充正式受控文件。"""
    _ensure_font()
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    pdf.setTitle(f"{row['code']} {row['title']} {row['version']}")
    page = Page(pdf, f"{row['code']}  {row['title']}", f"版本 {row['version']}，{note}")
    page.heading("1. 受控文件信息")
    page.field("分类", row.get("category", ""))
    page.field("负责人", owner_name)
    page.field("适用能力", "、".join(row.get("capability_scope") or []) or "全部")
    page.field("适用样本类型", "、".join(row.get("sample_types") or []) or "不限")
    page.field("培训确认", "需要" if row.get("requires_training_ack") else "不需要")
    page.heading("2. 目的")
    page.text(row.get("purpose", ""))
    page.heading("3. 安全、环境与应急")
    page.text(row.get("safety", ""))
    page.heading("4. 操作步骤")
    for index, step in enumerate(row.get("steps") or [], start=1):
        kind = KIND_LABEL.get(step.get("kind"), step.get("kind", ""))
        extra = f"  [{step['capability']}]" if step.get("capability") else ""
        page.text(f"{index}. {step['title']}（{kind}，约 {step.get('duration_min') or 0:g} min）{extra}", size=9.5)
        if step.get("instructions"):
            page.text(step["instructions"], indent=12)
        for check in step.get("checks") or []:
            page.text(f"□ {check}", indent=12)
    page.heading("5. 记录与偏差")
    page.text("按系统内批次记录执行；偏离本规程的操作须在批次中登记异常并由 QA 判定。")
    page._footer()
    pdf.save()
    return buffer.getvalue()
