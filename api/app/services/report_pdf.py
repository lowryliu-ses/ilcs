"""固定模板报告的 PDF 渲染。

用 CID 内置中日韩字体（STSong-Light），不依赖部署机上有没有装字体文件——
少一个「本地能出、线上出乱码」的失败模式。
"""
from __future__ import annotations

import io

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

FONT = "STSong-Light"
# CID 字体的字形映射不是 Unicode：U+00B7（·）会画成「▲」，U+2022（•）会画成「煉」。
# 项目符号只用实测过的 U+30FB，别换成看起来更合适的那几个。
BULLET = "\u30fb"
_registered = False

LEFT = 20 * mm
RIGHT = A4[0] - 20 * mm
TOP = A4[1] - 20 * mm
BOTTOM = 20 * mm
LINE = 5.2 * mm


def _ensure_font() -> None:
    global _registered
    if not _registered:
        pdfmetrics.registerFont(UnicodeCIDFont(FONT))
        _registered = True


class Page:
    def __init__(self, pdf: canvas.Canvas, title: str, subtitle: str):
        self.pdf = pdf
        self.title = title
        self.subtitle = subtitle
        self.y = TOP
        self.page_no = 1
        self._header()

    def _header(self) -> None:
        self.pdf.setFont(FONT, 14)
        self.pdf.drawString(LEFT, self.y, self.title)
        self.y -= 6 * mm
        self.pdf.setFont(FONT, 8)
        self.pdf.drawString(LEFT, self.y, self.subtitle)
        self.pdf.setLineWidth(0.5)
        self.y -= 2.5 * mm
        self.pdf.line(LEFT, self.y, RIGHT, self.y)
        self.y -= 6 * mm

    def _footer(self) -> None:
        self.pdf.setFont(FONT, 7.5)
        self.pdf.drawRightString(RIGHT, BOTTOM - 6 * mm, f"第 {self.page_no} 页")

    def space(self, needed: float = LINE) -> None:
        if self.y - needed < BOTTOM:
            self._footer()
            self.pdf.showPage()
            self.page_no += 1
            self.y = TOP
            self._header()

    def heading(self, text: str) -> None:
        self.space(12 * mm)
        self.y -= 2 * mm
        self.pdf.setFont(FONT, 11)
        self.pdf.drawString(LEFT, self.y, text)
        self.y -= 1.8 * mm
        self.pdf.setLineWidth(0.3)
        self.pdf.line(LEFT, self.y, RIGHT, self.y)
        self.y -= 5.5 * mm

    def text(self, value: str, size: float = 9, indent: float = 0) -> None:
        for line in self._wrap(value, size, RIGHT - LEFT - indent):
            self.space()
            self.pdf.setFont(FONT, size)
            self.pdf.drawString(LEFT + indent, self.y, line)
            self.y -= LINE

    def field(self, label: str, value: str) -> None:
        self.space()
        self.pdf.setFont(FONT, 9)
        self.pdf.drawString(LEFT, self.y, f"{label}：")
        width = pdfmetrics.stringWidth(f"{label}：", FONT, 9)
        self.y += LINE  # text() 会先退一行，这里抵消
        self.y -= LINE
        for index, line in enumerate(self._wrap(value or "—", 9, RIGHT - LEFT - width - 2)):
            if index:
                self.space()
            self.pdf.setFont(FONT, 9)
            self.pdf.drawString(LEFT + width + 2, self.y, line)
            self.y -= LINE

    def table(self, header: list[str], rows: list[list[str]], widths: list[float]) -> None:
        total = sum(widths)
        scale = (RIGHT - LEFT) / total if total else 1
        columns = [w * scale for w in widths]

        def draw_row(values: list[str], size: float, bold_line: bool) -> None:
            self.space(LINE + 1 * mm)
            x = LEFT
            self.pdf.setFont(FONT, size)
            for value, width in zip(values, columns):
                self.pdf.drawString(x + 1, self.y, self._clip(str(value), size, width - 2))
                x += width
            self.y -= 1.5 * mm
            if bold_line:
                self.pdf.setLineWidth(0.4)
                self.pdf.line(LEFT, self.y, RIGHT, self.y)
            self.y -= LINE - 1.5 * mm

        draw_row(header, 8.5, True)
        for row in rows:
            draw_row(row, 8.5, False)
        self.y -= 2 * mm

    @staticmethod
    def _wrap(value: str, size: float, width: float) -> list[str]:
        lines: list[str] = []
        for paragraph in str(value or "").split("\n"):
            current = ""
            for char in paragraph:
                if pdfmetrics.stringWidth(current + char, FONT, size) > width:
                    lines.append(current)
                    current = char
                else:
                    current += char
            lines.append(current)
        return lines or [""]

    @staticmethod
    def _clip(value: str, size: float, width: float) -> str:
        if pdfmetrics.stringWidth(value, FONT, size) <= width:
            return value
        clipped = value
        while clipped and pdfmetrics.stringWidth(clipped + "…", FONT, size) > width:
            clipped = clipped[:-1]
        return clipped + "…"

    def finish(self) -> None:
        self._footer()
        self.pdf.showPage()
        self.pdf.save()


def render(content: dict) -> bytes:
    """按固定模板渲染。章节顺序与需求 DEV-14.4 一致。"""
    _ensure_font()
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    header = content.get("header") or {}
    page = Page(
        pdf,
        content.get("title") or "实验报告",
        f" {BULLET} ".join(
            filter(
                None,
                [
                    header.get("code", ""),
                    f"版本 {header.get('version', '')}",
                    header.get("organization", ""),
                    header.get("published_at", "") or header.get("generated_at", ""),
                ],
            )
        ),
    )

    page.heading("一、方案与目的")
    for label, key in (("方案", "plan"), ("方案版本", "plan_version"), ("实验任务", "task"),
                       ("目的", "goal")):
        page.field(label, str((content.get("plan_section") or {}).get(key, "") or "—"))

    page.heading("二、方法与 SOP 版本")
    method = content.get("method_section") or {}
    for label, key in (("方法", "recipe"), ("方法版本", "recipe_version"),
                       ("SOP", "sop"), ("SOP 版本", "sop_version"),
                       ("附件摘要", "sop_checksum"), ("风险评估", "risk")):
        page.field(label, str(method.get(key, "") or "—"))

    page.heading("三、样本及来源")
    samples = content.get("samples") or []
    if samples:
        page.table(
            ["样本", "条码", "来源", "类型", "位置", "状态"],
            [
                [s.get("id", ""), s.get("barcode", ""), s.get("source", ""),
                 s.get("sample_type", ""), s.get("location", ""), s.get("state", "")]
                for s in samples
            ],
            [22, 20, 22, 14, 14, 12],
        )
    else:
        page.text("无样本记录")

    page.heading("四、人员、设备与物料")
    resources = content.get("resources") or {}
    page.field("负责人", resources.get("owner", "—"))
    page.field("执行人", resources.get("assignee", "—"))
    page.field("复核人", resources.get("reviewer", "—"))
    page.field("设备", "、".join(resources.get("stations") or []) or "—")
    materials = resources.get("materials") or []
    if materials:
        page.table(
            ["批号", "物料", "授权预留", "实际消耗", "损耗", "单位"],
            [
                [m.get("lot_id", ""), m.get("material", ""), m.get("qty", ""),
                 m.get("consumed", ""), m.get("loss", ""), m.get("unit", "")]
                for m in materials
            ],
            [24, 26, 14, 14, 12, 10],
        )
    else:
        page.text("本方法无物料需求")

    page.heading("五、执行与异常")
    for row in content.get("execution") or []:
        page.text(
            f"{BULLET} 第 {row.get('step_index', 0) + 1} 步（{row.get('kind_label', '')}）"
            f"{row.get('step_name', '')}：{row.get('state_label', '')}"
            f"{'，' + row.get('note', '') if row.get('note') else ''}"
        )
    for row in content.get("exceptions") or []:
        page.text(f"{BULLET} 异常：{row}")
    if not (content.get("execution") or content.get("exceptions")):
        page.text("无执行记录")

    page.heading("六、结果表")
    for block in content.get("results") or []:
        page.text(f"{block.get('metric_name', '')}（{block.get('unit', '')}）", size=9.5)
        rows = [
            [r.get("assignment_id", ""), r.get("condition_label", ""), str(r.get("round_no", "")),
             f"v{r.get('result_version', '')}", str(r.get("value", "")),
             r.get("quality_label", ""), r.get("review_label", "")]
            for r in block.get("rows") or []
        ]
        if rows:
            page.table(
                ["样本", "条件", "轮次", "版本", "数值", "质量", "审核"],
                rows, [22, 26, 10, 10, 16, 12, 12],
            )
        else:
            page.text("该指标没有纳入正式统计的记录", indent=4 * mm)

    page.heading("七、排除说明")
    exclusions = content.get("exclusions") or []
    if exclusions:
        page.table(
            ["样本", "指标", "版本", "排除原因", "质量", "审核"],
            [
                [e.get("assignment_id", ""), e.get("metric_name", ""),
                 f"v{e.get('result_version', '')}", e.get("reason_label", ""),
                 e.get("quality", ""), e.get("review_state", "")]
                for e in exclusions
            ],
            [22, 24, 10, 30, 12, 12],
        )
        page.text(
            "以上记录已列为被排除记录并说明原因，不进入正式统计结论。", size=8.5
        )
    else:
        page.text("无被排除记录")

    page.heading("八、统计")
    for block in content.get("statistics") or []:
        page.text(
            f"{block.get('metric_name', '')}：纳入 {block.get('included', 0)} 条、"
            f"排除 {block.get('excluded', 0)} 条；均值 {block.get('mean', '—')}"
            f"，SD {block.get('sd', '—')}，CV {block.get('cv_pct', '—')}%"
        )
        for group in block.get("groups") or []:
            page.text(
                f"{BULLET} {group.get('group', '')} {group.get('label', '')}："
                f"n={group.get('n_included', 0)}，均值 {group.get('mean', '—')}，"
                f"CV {group.get('cv_pct', '—')}%",
                size=8.5, indent=4 * mm,
            )
        for effect in block.get("effects") or []:
            levels = "、".join(
                f"{row.get('level')}→{row.get('mean') if row.get('mean') is not None else '—'}"
                for row in effect.get("levels") or []
            )
            page.text(f"{BULLET} 主效应 {effect.get('factor')}：{levels}", size=8.5, indent=4 * mm)

    page.heading("九、结论")
    page.text(content.get("conclusion") or "—")

    page.heading("十、复核与批准")
    approval = content.get("approval") or {}
    page.field("编写", approval.get("author", "—"))
    page.field("批准", approval.get("approver", "—"))
    page.field("签名含义", approval.get("signature_meaning", "—"))
    page.field("发布时间", approval.get("published_at", "—"))
    page.field("模板版本", approval.get("template_version", "—"))
    page.field("算法版本", approval.get("algorithm_version", "—"))
    page.field("结果版本快照", approval.get("result_versions", "—"))

    page.finish()
    return buffer.getvalue()
