"""固定模板报告的 PDF 渲染。

用 CID 内置中日韩字体（STSong-Light），不依赖部署机上有没有装字体文件——
少一个「本地能出、线上出乱码」的失败模式。
"""
from __future__ import annotations

import io

from ..domain.report_templates import SECTION_TITLES
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
    """按报告模板的章节顺序渲染（`domain/report_templates.py`）。老报告没有模板信息，按原固定章节渲染。"""
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

    sections = (content.get("template") or {}).get("sections") or LEGACY_SECTIONS
    for number, key in enumerate(sections, start=1):
        renderer = RENDERERS.get(key)
        if renderer is None:
            continue
        page.heading(f"{_chinese(number)}、{SECTION_TITLES.get(key, key)}")
        renderer(page, content)

    page.finish()
    return buffer.getvalue()


LEGACY_SECTIONS = ["plan", "method", "samples", "resources", "execution", "results", "exclusions",
                   "statistics", "conclusion", "approval"]
NUMERALS = "零一二三四五六七八九"


def _chinese(number: int) -> str:
    if number < 10:
        return NUMERALS[number]
    tens, ones = divmod(number, 10)
    return ("" if tens == 1 else NUMERALS[tens]) + "十" + (NUMERALS[ones] if ones else "")


def _plan(page: "Page", content: dict) -> None:
    for label, key in (("方案", "plan"), ("方案版本", "plan_version"), ("实验任务", "task"), ("目的", "goal")):
        page.field(label, str((content.get("plan_section") or {}).get(key, "") or "—"))


def _method(page: "Page", content: dict) -> None:
    method = content.get("method_section") or {}
    for label, key in (("流程", "recipe"), ("流程版本", "recipe_version"), ("SOP", "sop"), ("SOP 版本", "sop_version"),
                       ("附件摘要", "sop_checksum"), ("风险评估", "risk")):
        page.field(label, str(method.get(key, "") or "—"))


def _samples(page: "Page", content: dict) -> None:
    samples = content.get("samples") or []
    if not samples:
        page.text("无样本记录")
        return
    page.table(
        ["样本", "条码", "来源", "类型", "位置", "状态"],
        [[s.get("id", ""), s.get("barcode", ""), s.get("source", ""), s.get("sample_type", ""),
          s.get("location", ""), s.get("state", "")] for s in samples],
        [22, 20, 22, 14, 14, 12],
    )


def _resources(page: "Page", content: dict) -> None:
    resources = content.get("resources") or {}
    page.field("负责人", resources.get("owner", "—"))
    page.field("执行人", resources.get("assignee", "—"))
    page.field("复核人", resources.get("reviewer", "—"))
    page.field("设备", "、".join(resources.get("stations") or []) or "—")
    materials = resources.get("materials") or []
    if materials:
        page.table(
            ["批号", "物料", "授权预留", "实际消耗", "损耗", "单位"],
            [[m.get("lot_id", ""), m.get("material", ""), m.get("qty", ""), m.get("consumed", ""),
              m.get("loss", ""), m.get("unit", "")] for m in materials],
            [24, 26, 14, 14, 12, 10],
        )
    else:
        page.text("本流程无物料需求")


def _instruments(page: "Page", content: dict) -> None:
    rows = content.get("instruments") or []
    if not rows:
        page.text("本批次没有使用设备工位")
        return
    for row in rows:
        page.text(f"{row.get('station_id', '')} {row.get('name', '')}（{row.get('kind', '')}）", size=9.5)
        page.field("型号 / 厂商", f"{row.get('model') or '—'} / {row.get('vendor') or '—'}")
        page.field("资产号 / 序列号", f"{row.get('asset_no') or '—'} / {row.get('serial') or '—'}")
        page.field("固件 / 驱动", f"{row.get('firmware') or '—'} / {row.get('driver') or '—'}")
        page.field("校准", row.get("calibration") or "—")
        if row.get("methods"):
            page.field("设备方法", "、".join(row["methods"]))
        page.field("执行步骤", "、".join(row.get("steps") or []) or "—")


def _execution(page: "Page", content: dict) -> None:
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


def _operation_log(page: "Page", content: dict) -> None:
    rows = content.get("operation_log") or []
    if not rows:
        page.text("无操作记录")
        return
    page.table(
        ["时间", "操作人", "动作", "变更", "说明"],
        [[row.get("time", "").replace("T", " ")[5:16], row.get("user", ""),
          row.get("action", "") + ("（签名）" if row.get("signed") else ""),
          f"{row.get('before') or ''} → {row.get('after') or ''}" if (row.get("before") or row.get("after")) else "",
          row.get("detail", "")] for row in rows],
        [14, 14, 20, 20, 32],
    )


def _results(page: "Page", content: dict) -> None:
    for block in content.get("results") or []:
        page.text(f"{block.get('metric_name', '')}（{block.get('unit', '')}）", size=9.5)
        rows = [
            [r.get("assignment_id", ""), r.get("condition_label", ""), str(r.get("round_no", "")),
             f"v{r.get('result_version', '')}", str(r.get("value", "")), r.get("quality_label", ""),
             r.get("review_label", "")]
            for r in block.get("rows") or []
        ]
        if rows:
            page.table(["样本", "条件", "轮次", "版本", "数值", "质量", "审核"], rows, [22, 26, 10, 10, 16, 12, 12])
        else:
            page.text("该指标没有纳入正式统计的记录", indent=4 * mm)


def _exclusions(page: "Page", content: dict) -> None:
    exclusions = content.get("exclusions") or []
    if not exclusions:
        page.text("无被排除记录")
        return
    page.table(
        ["样本", "指标", "版本", "排除原因", "质量", "审核"],
        [[e.get("assignment_id", ""), e.get("metric_name", ""), f"v{e.get('result_version', '')}",
          e.get("reason_label", ""), e.get("quality", ""), e.get("review_state", "")] for e in exclusions],
        [22, 24, 10, 30, 12, 12],
    )
    page.text("以上记录已列为被排除记录并说明原因，不进入正式统计结论。", size=8.5)


def _data_flags(page: "Page", content: dict) -> None:
    rows = content.get("data_flags") or []
    if not rows:
        page.text("没有自动打标的数据")
        return
    page.table(
        ["范围", "对象", "标记", "说明"],
        [[row.get("scope", ""), row.get("target", ""), row.get("code", ""), row.get("message", "")] for row in rows],
        [12, 28, 14, 46],
    )
    page.text("自动打标不改变数值；是否纳入统计以审核结论为准。", size=8.5)


def _raw_files(page: "Page", content: dict) -> None:
    rows = content.get("raw_files") or []
    if not rows:
        page.text("没有关联的原始数据文件")
        return
    page.table(
        ["文件", "用途", "大小", "SHA-256 摘要"],
        [[row.get("filename", ""), "、".join(row.get("usage") or []), f"{row.get('size', 0)} B",
          (row.get("checksum") or "")[:24] + ("…" if len(row.get("checksum") or "") > 24 else "")] for row in rows],
        [28, 30, 12, 30],
    )
    page.text("原件保存在系统文件库，可按摘要核对未被替换。", size=8.5)


def _statistics(page: "Page", content: dict) -> None:
    for block in content.get("statistics") or []:
        page.text(
            f"{block.get('metric_name', '')}：纳入 {block.get('included', 0)} 条、排除 {block.get('excluded', 0)} 条；"
            f"均值 {block.get('mean', '—')}，SD {block.get('sd', '—')}，CV {block.get('cv_pct', '—')}%"
        )
        for group in block.get("groups") or []:
            page.text(
                f"{BULLET} {group.get('group', '')} {group.get('label', '')}：n={group.get('n_included', 0)}，"
                f"均值 {group.get('mean', '—')}，CV {group.get('cv_pct', '—')}%",
                size=8.5, indent=4 * mm,
            )
        for effect in block.get("effects") or []:
            levels = "、".join(
                f"{row.get('level')}→{row.get('mean') if row.get('mean') is not None else '—'}"
                for row in effect.get("levels") or []
            )
            page.text(f"{BULLET} 主效应 {effect.get('factor')}：{levels}", size=8.5, indent=4 * mm)


def _conclusion(page: "Page", content: dict) -> None:
    page.text(content.get("conclusion") or "—")


def _approval(page: "Page", content: dict) -> None:
    approval = content.get("approval") or {}
    for label, key in (("编写", "author"), ("批准", "approver"), ("签名含义", "signature_meaning"),
                       ("发布时间", "published_at"), ("模板版本", "template_version"),
                       ("算法版本", "algorithm_version"), ("结果版本快照", "result_versions")):
        page.field(label, approval.get(key, "—"))


RENDERERS = {
    "plan": _plan, "method": _method, "samples": _samples, "resources": _resources, "instruments": _instruments,
    "execution": _execution, "operation_log": _operation_log, "results": _results, "exclusions": _exclusions,
    "data_flags": _data_flags, "raw_files": _raw_files, "statistics": _statistics, "conclusion": _conclusion,
    "approval": _approval,
}
