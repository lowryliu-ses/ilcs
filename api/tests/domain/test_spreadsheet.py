"""实验表格读取：csv 编码、xlsx 的共享字符串 / 行内字符串 / 数字 / 公式缓存值 / 空行与上限、
隐藏行与隐藏工作表、补零格式、坏文件、流式解析的内存上限。xlsx 用代码现场生成。"""
import io
import struct
import tracemalloc
import zipfile

import pytest

from app.modules.formulation import spreadsheet
from app.modules.formulation.spreadsheet import MAX_COLUMNS, MAX_ROWS, MAX_SPAN, SpreadsheetError, column_index, read_sheet, read_table

MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def xlsx(rows_xml: str, shared: list[str] | None = None, sheet_path: str = "worksheets/data.xml",
         rich: bool = False, styles: str | None = None, first_state: str = "", second_state: str = "",
         compression: int = zipfile.ZIP_STORED) -> bytes:
    """最小 xlsx：工作簿 → 关系 → 第一个工作表（故意不叫 sheet1.xml）+ 可选共享字符串、样式表、工作表可见性。"""
    buffer = io.BytesIO()

    def state(value):
        return f' state="{value}"' if value else ""

    with zipfile.ZipFile(buffer, "w", compression=compression) as archive:
        archive.writestr("xl/workbook.xml", (
            f'<workbook xmlns="{MAIN}" xmlns:r="{REL}"><sheets>'
            f'<sheet name="配方" sheetId="1"{state(first_state)} r:id="rId7"/>'
            f'<sheet name="备注" sheetId="2"{state(second_state)} r:id="rId8"/>'
            "</sheets></workbook>"
        ))
        archive.writestr("xl/_rels/workbook.xml.rels", (
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="rId8" Type="{REL}/worksheet" Target="worksheets/other.xml"/>'
            f'<Relationship Id="rId7" Type="{REL}/worksheet" Target="{sheet_path}"/>'
            "</Relationships>"
        ))
        archive.writestr(f"xl/{sheet_path}", f'<worksheet xmlns="{MAIN}"><sheetData>{rows_xml}</sheetData></worksheet>')
        archive.writestr("xl/worksheets/other.xml", f'<worksheet xmlns="{MAIN}"><sheetData>'
                                                    '<row r="1"><c r="A1"><v>999</v></c></row></sheetData></worksheet>')
        if shared is not None:
            items = []
            for text in shared:
                if rich:
                    half = len(text) // 2
                    items.append(f"<si><r><t>{text[:half]}</t></r><r><rPr/><t>{text[half:]}</t></r>"
                                 "<rPh><t>ignored</t></rPh></si>")
                else:
                    items.append(f"<si><t>{text}</t></si>")
            archive.writestr("xl/sharedStrings.xml", f'<sst xmlns="{MAIN}">{"".join(items)}</sst>')
        if styles is not None:
            archive.writestr("xl/styles.xml", f'<styleSheet xmlns="{MAIN}">{styles}</styleSheet>')
    return buffer.getvalue()


def test_column_letters():
    assert column_index("A") == 1 and column_index("Z") == 26 and column_index("AA") == 27 and column_index("AZ") == 52


def test_csv_utf8_with_or_without_bom_and_gbk():
    text = "序列号,EC (g),EMC(g)\nELY-0001,10.6632,\n\n0002, 1.5 ,2\n\n\n"
    # 中间空行留着（行号与文件一致），末尾空行去掉
    expected = [["序列号", "EC (g)", "EMC(g)"], ["ELY-0001", "10.6632", None], [None, None, None], ["0002", "1.5", "2"]]
    assert read_table("f.csv", text.encode("utf-8")) == expected
    assert read_table("f.csv", text.encode("utf-8-sig")) == expected
    assert read_table("F.CSV", text.encode("gbk")) == expected, "Excel 另存的 csv 常是 gbk"


def test_csv_rows_are_padded_to_the_widest_row():
    assert read_table("f.csv", "a,b,c\n1\n".encode()) == [["a", "b", "c"], ["1", None, None]]


def test_xlsx_shared_inline_numbers_booleans_and_blank_rows():
    rows = (
        '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c><c r="D1" t="inlineStr"><is><t>LiBF4</t></is></c></row>'
        '<row r="2"/>'
        '<row r="3"><c r="A3" t="s"><v>2</v></c><c r="B3"><v>10.6632</v></c><c r="C3" t="b"><v>1</v></c>'
        '<c r="D3"><f>B3/10</f><v>1.06632</v></c></row>'
        '<row r="5"><c r="A5" t="str"><f>"ELY-"&amp;"0002"</f><v>ELY-0002</v></c><c r="B5" t="n"><v>3</v></c></row>'
    )
    table = read_table("配方.xlsx", xlsx(rows, ["序列号", "EC (g)", "ELY-0001"]))
    assert table == [
        ["序列号", "EC (g)", None, "LiBF4"],
        [None, None, None, None],
        ["ELY-0001", 10.6632, "TRUE", 1.06632],
        [None, None, None, None],
        ["ELY-0002", 3.0, None, None],
    ], "空行与缺的行留成空行：表里第 N 行就是工作表第 N 行"


def test_xlsx_rich_text_shared_strings_skip_phonetic_runs():
    rows = '<row r="1"><c r="A1" t="s"><v>0</v></c></row><row r="2"><c r="A2" t="s"><v>1</v></c></row>'
    assert read_table("f.xlsx", xlsx(rows, ["序列号", "ELY-9"], rich=True)) == [["序列号"], ["ELY-9"]]


def test_xlsx_absolute_relationship_target():
    rows = '<row r="1"><c r="A1" t="inlineStr"><is><t>序列号</t></is></c></row>'
    data = xlsx(rows, sheet_path="worksheets/data.xml")
    # 关系里写成包内绝对路径同样能找到
    patched = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(patched, "w") as target:
        for item in source.infolist():
            content = source.read(item.filename)
            if item.filename == "xl/_rels/workbook.xml.rels":
                content = content.replace(b'Target="worksheets/data.xml"', b'Target="/xl/worksheets/data.xml"')
            target.writestr(item, content)
    assert read_table("f.xlsx", patched.getvalue()) == [["序列号"]]


def test_xlsx_formula_without_cached_value_is_rejected():
    rows = '<row r="1"><c r="A1"><v>1</v></c></row><row r="2"><c r="C2"><f>A1*2</f></c></row>'
    with pytest.raises(SpreadsheetError, match="第 2 行 C 列是公式但没有计算结果，请在 Excel 里保存后再导入"):
        read_table("f.xlsx", xlsx(rows))


def test_bad_files_and_limits():
    with pytest.raises(SpreadsheetError, match="不是有效的 .xlsx"):
        read_table("f.xlsx", b"not a zip")
    with pytest.raises(SpreadsheetError, match="另存为 .xlsx"):
        read_table("f.xls", b"x")
    with pytest.raises(SpreadsheetError, match="只支持"):
        read_table("f.txt", b"x")
    with pytest.raises(SpreadsheetError, match="1 MiB"):
        read_table("f.csv", b"a" * (1024 * 1024 + 1))
    with pytest.raises(SpreadsheetError, match=f"超过 {MAX_ROWS} 行"):
        read_table("f.csv", ("a\n" * (MAX_ROWS + 1)).encode())
    with pytest.raises(SpreadsheetError, match=f"超过 {MAX_COLUMNS} 列"):
        read_table("f.csv", (",".join(["x"] * (MAX_COLUMNS + 1))).encode())
    with pytest.raises(SpreadsheetError, match="既不是 UTF-8 也不是 GBK"):
        read_table("f.csv", b"\xff\xfe\xfa\xfb\x80\x81")


def test_csv_quoted_newline_keeps_later_rows_on_their_file_line():
    table = read_table("f.csv", '序列号,备注\nB-1,"两行\n备注"\nB-2,x\n'.encode())
    assert table == [["序列号", "备注"], ["B-1", "两行\n备注"], [None, None], ["B-2", "x"]]


def test_xlsx_formula_with_empty_string_result_is_an_empty_cell():
    for empty in ("<v></v>", "<v/>"):
        rows = ('<row r="1"><c r="A1" t="inlineStr"><is><t>序列号</t></is></c></row>'
                f'<row r="2"><c r="A2" t="inlineStr"><is><t>0001</t></is></c><c r="B2"><v>3</v></c>'
                f'<c r="C2" t="str"><f>IF(B2&gt;5,"high","")</f>{empty}</c>'
                f'<c r="CZ2" t="str"><f>IF(B2&gt;5,"x","")</f>{empty}</c></row>')
        assert read_table("f.xlsx", xlsx(rows)) == [["序列号", None], ["0001", 3.0]]
    # 没有 t 的公式没有缓存值，仍按「没在 Excel 里算过」拒绝
    with pytest.raises(SpreadsheetError, match="是公式但没有计算结果"):
        read_table("f.xlsx", xlsx('<row r="1"><c r="C1"><f>A1*2</f><v/></c></row>'))


def test_xlsx_hidden_rows_are_skipped_with_a_warning():
    rows = ('<row r="1"><c r="A1" t="inlineStr"><is><t>序列号</t></is></c></row>'
            '<row r="2" hidden="1"><c r="A2" t="inlineStr"><is><t>ELY-0001</t></is></c></row>'
            '<row r="3" hidden="true"><c r="A3" t="inlineStr"><is><t>ELY-0002</t></is></c></row>'
            '<row r="4"><c r="A4" t="inlineStr"><is><t>ELY-0003</t></is></c></row>'
            '<row r="5" hidden="1" s="2" customFormat="1"/>')
    sheet = read_sheet("f.xlsx", xlsx(rows))
    assert sheet.rows == [["序列号"], [None], [None], ["ELY-0003"]], "隐藏行留成空行，行号不变"
    assert sheet.warnings == ["第 2、3 行在 Excel 里被隐藏或筛选掉了，没有导入；要导入请在 Excel 里取消隐藏 / 筛选后重新保存"]


def test_xlsx_hidden_first_sheet_reads_the_first_visible_one():
    sheet = read_sheet("f.xlsx", xlsx('<row r="1"><c r="A1"><v>1</v></c></row>', first_state="hidden"))
    assert sheet.rows == [[999.0]]
    assert sheet.warnings == ["隐藏的工作表 配方 没有读，读的是第一个可见工作表「备注」"]
    with pytest.raises(SpreadsheetError, match="没有可见的工作表"):
        read_table("f.xlsx", xlsx("", first_state="hidden", second_state="veryHidden"))


def test_xlsx_styled_empty_rows_do_not_count_as_content():
    rows = ('<row r="1"><c r="A1" t="inlineStr"><is><t>序列号</t></is></c></row>'
            '<row r="2"><c r="A2" t="inlineStr"><is><t>0001</t></is></c></row>'
            + "".join(f'<row r="{n}" s="2" customFormat="1"><c r="A{n}" s="3"/><c r="F{n}" s="3"/></row>'
                      for n in range(3, 400))
            + '<row r="10001" s="2" customFormat="1"/><row r="20000" ht="20" customHeight="1"/>')
    assert read_table("f.xlsx", xlsx(rows)) == [["序列号"], ["0001"]]
    far = f'<row r="1"><c r="A1"><v>1</v></c></row><row r="{MAX_SPAN + 1}"><c r="A{MAX_SPAN + 1}"><v>2</v></c></row>'
    with pytest.raises(SpreadsheetError, match=f"第 {MAX_SPAN + 1} 行还有内容"):
        read_table("f.xlsx", xlsx(far))
    many = "".join(f'<row r="{n}"><c r="A{n}"><v>{n}</v></c></row>' for n in range(1, MAX_ROWS + 2))
    with pytest.raises(SpreadsheetError, match=f"超过 {MAX_ROWS} 行上限"):
        read_table("f.xlsx", xlsx(many))


def test_xlsx_zero_padded_number_formats_keep_the_displayed_text():
    styles = ('<numFmts count="3"><numFmt numFmtId="164" formatCode="0000"/>'
              '<numFmt numFmtId="165" formatCode="&quot;ELY-&quot;0000;[Red]-0000"/>'
              '<numFmt numFmtId="166" formatCode="0.0000"/></numFmts>'
              '<cellStyleXfs count="1"><xf numFmtId="164"/></cellStyleXfs>'
              '<cellXfs count="4"><xf numFmtId="0"/><xf numFmtId="164"/><xf numFmtId="165"/><xf numFmtId="166"/></cellXfs>')
    rows = ('<row r="1"><c r="A1" s="1"><v>1</v></c><c r="B1" s="2"><v>12</v></c><c r="C1" s="3"><v>2</v></c>'
            '<c r="D1" s="1"><v>1.5</v></c><c r="E1" s="0"><v>7</v></c><c r="F1" s="1"><v>123456</v></c></row>')
    assert read_table("f.xlsx", xlsx(rows, styles=styles)) == [["0001", "ELY-0012", 2.0, 1.5, 7.0, "123456"]]
    assert spreadsheet._zero_padded("0.00") is None and spreadsheet._zero_padded("0") is None
    assert spreadsheet._zero_padded("\\E\\L\\Y-000") == ("ELY-", 3, "")


def test_xlsx_corrupt_encrypted_or_malformed_parts_are_spreadsheet_errors():
    rows = "".join(f'<row r="{n}"><c r="A{n}"><v>{n}</v></c></row>' for n in range(1, 50))
    data = bytearray(xlsx(rows, compression=zipfile.ZIP_DEFLATED))
    with zipfile.ZipFile(io.BytesIO(bytes(data))) as archive:
        info = archive.getinfo("xl/worksheets/data.xml")
    start = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra)
    for index in range(start + 5, start + 40):
        data[index] ^= 0xFF
    with pytest.raises(SpreadsheetError, match="已损坏或加密"):
        read_table("f.xlsx", bytes(data))

    # 中央目录里给工作表打上加密标志
    locked = bytearray(xlsx(rows))
    name = b"xl/worksheets/data.xml"
    at = locked.find(b"PK\x01\x02" + b"")
    while at >= 0:
        size = struct.unpack("<H", locked[at + 28:at + 30])[0]
        if locked[at + 46:at + 46 + size] == name:
            locked[at + 8] |= 0x01
        at = locked.find(b"PK\x01\x02", at + 4)
    with pytest.raises(SpreadsheetError, match="已损坏或加密"):
        read_table("f.xlsx", bytes(locked))

    with pytest.raises(SpreadsheetError, match="行号 'x1' 无效"):
        read_table("f.xlsx", xlsx('<row r="x1"><c r="A1"><v>1</v></c></row>'))
    with pytest.raises(SpreadsheetError, match="不是有效的 XML"):
        read_table("f.xlsx", xlsx('<row r="1"><c r="A1"><v>1</v></row>'))


def test_xlsx_parse_is_streamed_and_bounded(monkeypatch):
    # 约 3 MiB 的 XML：1000 行 × 300 个带样式的空单元格。整棵树解析要上百 MB，流式只和一行有关
    row = "".join('<c s="1"/>' for _ in range(300))
    rows = "".join(f'<row r="{n}">{row}</row>' for n in range(2, 1000))
    data = xlsx('<row r="1"><c r="A1"><v>1</v></c></row>' + rows, compression=zipfile.ZIP_DEFLATED)
    assert len(data) < 1024 * 1024
    tracemalloc.start()
    try:
        assert read_table("f.xlsx", data) == [[1.0]]
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 25 * 1024 * 1024, f"峰值 {peak / 1024 / 1024:.0f} MiB"
    # 解压预算：声明解压后超过上限直接拒绝
    huge = xlsx(rows * 3, compression=zipfile.ZIP_DEFLATED)
    with pytest.raises(SpreadsheetError, match="解压后过大"):
        read_table("f.xlsx", huge)
    # 共享字符串条数封顶
    monkeypatch.setattr(spreadsheet, "MAX_SHARED", 2)
    with pytest.raises(SpreadsheetError, match="共享字符串过多"):
        read_table("f.xlsx", xlsx('<row r="1"><c r="A1" t="s"><v>0</v></c></row>', ["a", "b", "c"]))


def test_xlsx_junk_inside_a_row_is_bounded_and_odd_numbers_are_rejected():
    # 行里夹大量无关元素：每个 <c> 开始时不能把外层计数清零，否则杂项子元素一直留在行上、摘除成本平方增长
    junk = ('<x/>' * 999 + '<c r="A1"><v>1</v></c>') * 50
    with pytest.raises(SpreadsheetError, match="结构异常"):
        read_table("f.xlsx", xlsx(f'<row r="1">{junk}</row>'))
    # 行号 / 样式号只认 ASCII 数字：「²」、上千位数字都按坏文件报，不是 500
    with pytest.raises(SpreadsheetError, match="行号"):
        read_table("f.xlsx", xlsx('<row r="²"><c r="A1"><v>1</v></c></row>'))
    with pytest.raises(SpreadsheetError, match="行号"):
        read_table("f.xlsx", xlsx(f'<row r="{"9" * 5000}"><c r="A1"><v>1</v></c></row>'))
    assert read_table("f.xlsx", xlsx('<row r="1"><c r="A1" s="²"><v>1</v></c></row>')) == [[1.0]]


def test_xlsx_lzma_damage_and_conditional_number_formats():
    rows = "".join(f'<row r="{n}"><c r="A{n}"><v>{n}</v></c></row>' for n in range(1, 50))
    data = bytearray(xlsx(rows, compression=zipfile.ZIP_LZMA))
    with zipfile.ZipFile(io.BytesIO(bytes(data))) as archive:
        info = archive.getinfo("xl/worksheets/data.xml")
    start = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra)
    for index in range(start + 5, start + 40):
        data[index] ^= 0xFF
    with pytest.raises(SpreadsheetError, match="已损坏或加密"):
        read_table("f.xlsx", bytes(data))
    # 条件格式 <dxfs> 里复用了同一个 numFmtId：不能盖掉单元格自己的补零格式
    styles = ('<numFmts><numFmt numFmtId="164" formatCode="0000"/></numFmts>'
              '<cellXfs><xf numFmtId="0"/><xf numFmtId="164"/></cellXfs>'
              '<dxfs><dxf><numFmt numFmtId="164" formatCode="0.00"/></dxf></dxfs>')
    assert read_table("f.xlsx", xlsx('<row r="1"><c r="A1" s="1"><v>5</v></c></row>', styles=styles)) == [["0005"]]
