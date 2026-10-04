"""读实验表格（.xlsx / .csv）成二维表。只用标准库。

配方表是研究员手工整理后导入的：第一行表头，其余每行一瓶。这里只负责把文件读成
`list[list[str | float | None]]`，列怎么解释（序列号、试剂、单位）归 rules.py。

- 行号与表格对得上：表里第 i 行（从 1 起）就是文件里的第 i 行。中间的空行、隐藏行都留成空行，
  只去掉末尾的空行——问题提示「第 N 行」要能直接在 Excel 里找到；预览把这张表原样交回前端、
  前端再提交回来，行号也不会走样。
- .csv：utf-8（带或不带 BOM）优先，失败再试 gbk（国内 Excel 另存的 csv 常是 gbk）；逗号分隔。
  单元格一律按文字返回：序列号「0001」不能被当成数字 1。
- .xlsx：zip + xml，读第一个**可见**工作表（按 workbook.xml 与它的关系文件找路径，不假设叫 sheet1.xml）。
  数字单元格返回 float；单元格格式是补零（如 0000、"ELY-"0000）的整数按 Excel 显示的文字返回，
  否则瓶身上印的 0001 会被登记成 1。公式取 Excel 保存时的缓存值——没有缓存值说明文件是程序生成、
  没在 Excel 里算过，直接拒绝，不自己算公式；缓存值是空文字（=IF(…,"",…)）按空单元格。
  隐藏 / 筛选掉的行不导入（看不见的瓶子不能悄悄进方案），用 warnings 提醒。
- 流式解析（iterparse，处理完一行就丢掉），内存不随文件里的空单元格、空行增长；只有格式没有值的行不算内容。
- 大小 1 MiB、有内容的行 200 行、100 列封顶，超了报错而不是截断（截掉的瓶子没人会发现）。
  坏文件、加密、结构异常一律 SpreadsheetError（接口 422），不让它变成 500。
"""
from __future__ import annotations

import csv
import io
import lzma
import math
import posixpath
import re
import zipfile
import zlib
from typing import Iterator, NamedTuple
from xml.etree import ElementTree

MAX_BYTES = 1024 * 1024
MAX_ROWS = 200
MAX_COLUMNS = 100
# 有内容的最后一行不能超过这个行号：中间空行要留着对行号，表格总长也得有个头
MAX_SPAN = MAX_ROWS * 5
# 解压后的上限：1 MiB 的 zip 可以解出几个 G（压缩炸弹），只读需要的几个部件，合计超过这个数就拒绝。
# 真实的 200 行 × 100 列配方表不到 1 MiB 的 XML
MAX_UNZIPPED = 8 * 1024 * 1024
# 共享字符串 / 样式条数上限：一个工作簿里别的工作表也往里放字符串，给足余量，只防恶意文件
MAX_SHARED = MAX_ROWS * MAX_COLUMNS * 5
MAX_STYLES = 65536
# Excel 一行最多 16384 列，超过就是坏文件；一个单元格 / 共享字符串里的子元素也不该上千
MAX_ROW_CELLS = 16384
MAX_NESTED = 1000
MAX_SHEET_ROW = 1048576

MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PACKAGE_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
CELL_REF = re.compile(r"^([A-Z]{1,3})([0-9]{1,7})$")
DIGITS = re.compile(r"[0-9]{1,7}")
# 数字格式里可以不加引号直接写的字面字符
FORMAT_LITERALS = set("$-+/():!^&'~{}<>= ")

Cell = str | float | None


class SpreadsheetError(ValueError):
    """表格读不了：格式、编码、大小或内容问题。消息直接给导入的人看。"""


class Sheet(NamedTuple):
    rows: list[list[Cell]]
    warnings: list[str]


def column_index(letters: str) -> int:
    """列字母 → 从 1 起的序号：A → 1，Z → 26，AA → 27。"""
    number = 0
    for char in letters:
        number = number * 26 + (ord(char) - ord("A") + 1)
    return number


def column_letters(index: int) -> str:
    letters = ""
    while index > 0:
        index, rest = divmod(index - 1, 26)
        letters = chr(ord("A") + rest) + letters
    return letters


def read_table(filename: str, data: bytes) -> list[list[Cell]]:
    """按扩展名读表格。返回去掉末尾空行、按最宽一行补齐的二维表（中间空行保留，行号与文件一致）。"""
    return read_sheet(filename, data).rows


def read_sheet(filename: str, data: bytes) -> Sheet:
    """同 read_table，另带读表时的提醒（隐藏行、隐藏工作表没有读）。"""
    if len(data) > MAX_BYTES:
        raise SpreadsheetError(f"文件 {len(data) / 1024 / 1024:.1f} MiB，超过 1 MiB 上限")
    lowered = (filename or "").lower()
    warnings: list[str] = []
    if lowered.endswith(".csv"):
        rows = _read_csv(data)
    elif lowered.endswith(".xlsx"):
        try:
            rows = _read_xlsx(data, warnings)
        except (zipfile.BadZipFile, zlib.error, lzma.LZMAError, NotImplementedError, EOFError, OSError, RuntimeError) as exc:
            # 兜底：zip 各层的异常种类很多，漏网的也按坏文件报，不变成 500
            raise SpreadsheetError("不是有效的 .xlsx 文件（文件已损坏或加密）") from exc
    elif lowered.endswith(".xls"):
        raise SpreadsheetError("不支持旧版 .xls：请在 Excel 里另存为 .xlsx 或 .csv 后再导入")
    else:
        raise SpreadsheetError("只支持 .xlsx 与 .csv 文件")
    return Sheet(check_limits(rows), warnings)


def check_limits(rows: list[list[Cell]]) -> list[list[Cell]]:
    """末尾空行、尾部空列去掉，各行补齐；中间空行留着（行号对得上）。再核行列上限。前端直接提交的表格也走这里。"""
    kept = [list(row) for row in rows]
    while kept and all(_blank(cell) for cell in kept[-1]):
        kept.pop()
    if len(kept) > MAX_SPAN:
        raise SpreadsheetError(f"表格第 {len(kept)} 行还有内容：内容要在前 {MAX_SPAN} 行以内")
    filled = sum(1 for row in kept if any(not _blank(cell) for cell in row))
    if filled > MAX_ROWS:
        raise SpreadsheetError(f"表格有 {filled} 行（含表头），超过 {MAX_ROWS} 行上限")
    width = max((index + 1 for row in kept for index, cell in enumerate(row) if not _blank(cell)), default=0)
    if width > MAX_COLUMNS:
        raise SpreadsheetError(f"表格有 {width} 列，超过 {MAX_COLUMNS} 列上限")
    return [[None if _blank(cell) else cell for cell in (row + [None] * width)[:width]] for row in kept]


def _blank(cell: Cell) -> bool:
    return cell is None or (isinstance(cell, str) and not cell.strip())


def _place(rows: list[list[Cell]], number: int, values: list[Cell]) -> None:
    """把有内容的一行放到第 number 行（从 1 起），中间缺的补空行。先核上限再补，空行不会无限增长。"""
    if number > MAX_SPAN:
        raise SpreadsheetError(f"表格第 {number} 行还有内容：内容要在前 {MAX_SPAN} 行以内")
    while len(rows) < number - 1:
        rows.append([])
    if len(rows) >= number:
        # 同一行号出现两次（不规范的文件）：按列合并，后出现的非空值为准
        old = rows[number - 1]
        rows[number - 1] = [
            new if new is not None else (old[index] if index < len(old) else None)
            for index, new in enumerate(values + [None] * max(0, len(old) - len(values)))
        ]
    else:
        rows.append(values)


# ---------- csv ----------

def _read_csv(data: bytes) -> list[list[Cell]]:
    for encoding in ("utf-8-sig", "gbk"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise SpreadsheetError("csv 文件既不是 UTF-8 也不是 GBK 编码：请在 Excel 里另存为「CSV UTF-8」")
    rows: list[list[Cell]] = []
    reader = csv.reader(io.StringIO(text, newline=""))
    line = 0
    filled = 0
    try:
        for record in reader:
            # 带换行的引号单元格一条记录占多行：按起始行号放，与文件里的行号对得上
            start = line + 1
            line = reader.line_num
            values = [cell.strip() or None for cell in record]
            if all(value is None for value in values):
                continue
            filled += 1
            if filled > MAX_ROWS:
                raise SpreadsheetError(f"表格超过 {MAX_ROWS} 行上限（含表头）")
            _place(rows, start, values)
    except csv.Error as exc:
        raise SpreadsheetError(f"csv 文件格式不正确：{exc}") from exc
    return rows


# ---------- xlsx ----------

class _Package:
    """xlsx 包：按需读部件，累计解压量封顶；部件一律流式解析。"""

    def __init__(self, data: bytes):
        try:
            self.zip = zipfile.ZipFile(io.BytesIO(data))
            self.names = set(self.zip.namelist())
        except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, EOFError, ValueError, NotImplementedError) as exc:
            raise SpreadsheetError("不是有效的 .xlsx 文件（无法按 zip 打开）") from exc
        self.budget = MAX_UNZIPPED

    def read(self, name: str) -> bytes | None:
        if name not in self.names:
            return None
        info = self.zip.getinfo(name)
        # 按声明的解压大小扣预算；zipfile 读出的量不会超过声明值（超了按 CRC 错报坏文件）
        self.budget -= info.file_size
        if self.budget < 0:
            raise SpreadsheetError("xlsx 解压后过大，已拒绝")
        try:
            return self.zip.read(name)
        except (zipfile.BadZipFile, zlib.error, lzma.LZMAError, RuntimeError, NotImplementedError, EOFError, OSError) as exc:
            raise SpreadsheetError(f"xlsx 里的 {name} 已损坏或加密，无法读取") from exc

    def stream(
        self, name: str, wanted: set[str], keep: set[str] | None = None,
    ) -> Iterator[tuple[str, str, ElementTree.Element]]:
        """流式读一个部件，只交出 wanted 里的元素：开始时交 ("start", 父标签, 元素)（只有属性），
        结束时交 ("end", 父标签, 元素)。交出后元素从父元素上摘掉。

        `keep`（缺省同 wanted）里的元素要带着整棵子树交出（单元格要读 <v>/<f>/<is>，共享字符串要读 <t>/<r>）；
        其余元素结束就摘掉——包括 wanted 但不在 keep 里的元素（如 <row>）的杂项子元素。内存只和一个单元格有关，
        与文件长短、行里夹了多少无关的元素都无关。每个 wanted 元素里的非 wanted 元素计数封顶，防恶意文件。"""
        raw = self.read(name)
        if raw is None:
            return
        keep = wanted if keep is None else keep
        stack: list[ElementTree.Element] = []
        # 每个打开的 wanted 元素一个计数（外层的计数不因为内层 wanted 元素开始而清零）
        counts: list[int] = []
        kept = 0
        try:
            for event, elem in ElementTree.iterparse(io.BytesIO(raw), events=("start", "end")):
                if event == "start":
                    parent_tag = stack[-1].tag if stack else ""
                    stack.append(elem)
                    if elem.tag in keep:
                        kept += 1
                    if elem.tag in wanted:
                        counts.append(0)
                        yield "start", parent_tag, elem
                    elif counts:
                        counts[-1] += 1
                        if counts[-1] > MAX_NESTED:
                            raise SpreadsheetError(f"xlsx 里的 {name} 结构异常（单个元素的子元素过多）")
                    continue
                stack.pop()
                parent = stack[-1] if stack else None
                if elem.tag in keep:
                    kept -= 1
                if elem.tag in wanted:
                    counts.pop()
                    yield "end", parent.tag if parent is not None else "", elem
                elif kept:
                    continue  # 属于正在读的元素（单元格、共享字符串）的内容，等它结束一起丢
                elem.clear()
                if parent is not None:
                    parent.remove(elem)
        except ElementTree.ParseError as exc:
            raise SpreadsheetError(f"xlsx 里的 {name} 不是有效的 XML") from exc


def _text_of(node: ElementTree.Element | None) -> str:
    """共享字符串 / 行内字符串：纯文本 <t>，或富文本的多段 <r><t>；注音 <rPh> 不算正文。"""
    if node is None:
        return ""
    direct = node.find(f"{MAIN}t")
    if direct is not None:
        return direct.text or ""
    return "".join((run.findtext(f"{MAIN}t") or "") for run in node.findall(f"{MAIN}r"))


def _first_sheet(package: _Package, warnings: list[str]) -> str:
    """第一个可见工作表的路径。隐藏（hidden / veryHidden）的跳过并提醒：看不见的表不能当配方导入。"""
    if "xl/workbook.xml" not in package.names:
        raise SpreadsheetError("xlsx 里没有 xl/workbook.xml")
    chosen: tuple[str, str] | None = None
    hidden: list[str] = []
    seen = 0
    for event, parent, sheet in package.stream("xl/workbook.xml", {f"{MAIN}sheet"}):
        if event != "end" or parent != f"{MAIN}sheets":
            continue
        seen += 1
        name = sheet.get("name") or f"第 {seen} 个"
        if (sheet.get("state") or "visible") in ("hidden", "veryHidden"):
            if len(hidden) < 20:
                hidden.append(name)
            continue
        chosen = (name, sheet.get(f"{REL}id") or "")
        break
    if not seen:
        raise SpreadsheetError("xlsx 里没有工作表")
    if chosen is None:
        raise SpreadsheetError("xlsx 里没有可见的工作表")
    name, rel_id = chosen
    if hidden:
        warnings.append(f"隐藏的工作表 {'、'.join(hidden)} 没有读，读的是第一个可见工作表「{name}」")
    target = ""
    for event, _, rel in package.stream("xl/_rels/workbook.xml.rels", {f"{PACKAGE_REL}Relationship"}):
        if event == "end" and rel.get("Id") == rel_id:
            target = rel.get("Target") or ""
            break
    if not target:
        raise SpreadsheetError("xlsx 的工作表关系缺失，找不到第一个工作表")
    # 关系里的路径相对 xl/；以 / 开头的是包内绝对路径
    path = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join("xl", target))
    if path not in package.names:
        raise SpreadsheetError(f"xlsx 里找不到工作表 {path}")
    return path


def _shared_strings(package: _Package) -> list[str]:
    shared: list[str] = []
    for event, _, item in package.stream("xl/sharedStrings.xml", {f"{MAIN}si"}):
        if event != "end":
            continue
        shared.append(_text_of(item))
        if len(shared) > MAX_SHARED:
            raise SpreadsheetError("xlsx 的共享字符串过多，已拒绝")
    return shared


def _zero_padded(code: str) -> tuple[str, int, str] | None:
    """数字格式是不是「字面前缀 + 一段补零 + 字面后缀」（0000、"ELY-"0000、\\E\\L\\Y-0000）。
    是就返回 (前缀, 位数, 后缀)；小数、千分位、颜色 / 条件段、日期等一概不认（按数字返回）。"""
    section, quoted = "", False
    for char in code:
        if char == '"':
            quoted = not quoted
        elif char == ";" and not quoted:
            break
        section += char
    prefix: list[str] = []
    suffix: list[str] = []
    zeros = 0
    index = 0
    while index < len(section):
        char = section[index]
        if char == '"':
            end = section.find('"', index + 1)
            if end < 0:
                return None
            literal, index = section[index + 1:end], end + 1
        elif char == "\\":
            if index + 1 >= len(section):
                return None
            literal, index = section[index + 1], index + 2
        elif char == "0":
            if zeros:
                return None  # 两段数字（如 00-00）不认
            while index < len(section) and section[index] == "0":
                zeros += 1
                index += 1
            continue
        elif char in FORMAT_LITERALS:
            literal, index = char, index + 1
        else:
            return None
        (suffix if zeros else prefix).append(literal)
    # 只有一个 0 就是普通整数格式；补零至少两位才会改变显示
    if zeros < 2:
        return None
    return "".join(prefix), zeros, "".join(suffix)


def _styles(package: _Package) -> list[tuple[str, int, str] | None]:
    """单元格样式序号 → 补零格式（没有就是 None）。内置格式都不补零，只看自定义的 numFmts。"""
    custom: dict[str, str] = {}
    cell_formats: list[str] = []
    for event, parent, elem in package.stream("xl/styles.xml", {f"{MAIN}numFmt", f"{MAIN}xf"}):
        if event != "end":
            continue
        if elem.tag == f"{MAIN}numFmt":
            # 只认 <numFmts> 下的：条件格式 <dxfs> 里也有 numFmt，可能复用同一个编号却是别的格式
            if parent == f"{MAIN}numFmts" and len(custom) < MAX_STYLES:
                custom[elem.get("numFmtId") or ""] = elem.get("formatCode") or ""
        elif parent == f"{MAIN}cellXfs":
            cell_formats.append(elem.get("numFmtId") or "0")
            if len(cell_formats) > MAX_STYLES:
                raise SpreadsheetError("xlsx 的样式表过大，已拒绝")
    return [_zero_padded(custom[fmt]) if fmt in custom else None for fmt in cell_formats]


def _row_number(raw: str | None, previous: int) -> int:
    if raw is None:
        return previous + 1
    raw = raw.strip()
    # 只认 ASCII 数字：str.isdigit() 也认「²」和上千位的数字串，int() 会抛普通 ValueError 变成 500
    if not DIGITS.fullmatch(raw) or not 0 < int(raw) <= MAX_SHEET_ROW:
        raise SpreadsheetError(f"工作表里的行号 {raw[:20]!r} 无效")
    return int(raw)


def _row_list(numbers: list[int]) -> str:
    shown = "、".join(str(number) for number in numbers[:20])
    return f"{shown} 等 {len(numbers)} 行" if len(numbers) > 20 else f"{shown} 行"


def _read_xlsx(data: bytes, warnings: list[str]) -> list[list[Cell]]:
    package = _Package(data)
    path = _first_sheet(package, warnings)
    shared = _shared_strings(package)
    formats = _styles(package)
    if path not in package.names:
        raise SpreadsheetError(f"xlsx 里找不到工作表 {path}")
    rows: list[list[Cell]] = []
    filled = 0
    hidden_rows: list[int] = []
    number = 0
    in_row = hidden = hidden_content = False
    values: dict[int, Cell] = {}
    next_col = cells = 0
    for event, _, elem in package.stream(path, {f"{MAIN}row", f"{MAIN}c"}, keep={f"{MAIN}c"}):
        if elem.tag == f"{MAIN}row":
            if event == "start":
                number = _row_number(elem.get("r"), number)
                hidden = (elem.get("hidden") or "").strip().lower() in ("1", "true")
                in_row, hidden_content, values, next_col, cells = True, False, {}, 0, 0
                continue
            in_row = False
            if hidden:
                if hidden_content:
                    hidden_rows.append(number)
            elif values:
                # 只有格式（样式、行高、带样式的空单元格）没有值的行不算内容
                filled += 1
                if filled > MAX_ROWS:
                    raise SpreadsheetError(f"表格超过 {MAX_ROWS} 行上限（含表头），第 {number} 行还有内容")
                width = max(values)
                _place(rows, number, [values.get(column) for column in range(1, width + 1)])
            continue
        if event != "end" or not in_row:
            continue
        cells += 1
        if cells > MAX_ROW_CELLS:
            raise SpreadsheetError(f"工作表第 {number} 行的单元格过多，文件结构异常")
        ref = CELL_REF.match(elem.get("r") or "")
        column = column_index(ref.group(1)) if ref else next_col + 1
        next_col = column
        if hidden:
            # 隐藏行只判断有没有内容（用来提醒），不校验、不导入
            try:
                hidden_content = hidden_content or _cell_value(elem, shared, formats, number, column) is not None
            except SpreadsheetError:
                hidden_content = True
            continue
        value = _cell_value(elem, shared, formats, number, column)
        if value is None:
            continue
        if column > MAX_COLUMNS:
            raise SpreadsheetError(f"表格超过 {MAX_COLUMNS} 列上限（第 {number} 行 {column_letters(column)} 列有内容）")
        values[column] = value
    if hidden_rows:
        warnings.append(
            f"第 {_row_list(hidden_rows)}在 Excel 里被隐藏或筛选掉了，没有导入；"
            "要导入请在 Excel 里取消隐藏 / 筛选后重新保存"
        )
    return rows


def _cell_value(cell: ElementTree.Element, shared: list[str], formats: list, row: int, column: int) -> Cell:
    kind = cell.get("t") or "n"
    value = cell.find(f"{MAIN}v")
    raw = value.text if value is not None else None
    # t="str" 是公式的文字结果：<v></v> 表示算出来就是空文字（=IF(…,"",…)），按空单元格；
    # 没有 t（或 t="n"）又没有缓存值，才是程序生成、没在 Excel 里算过的文件
    if cell.find(f"{MAIN}f") is not None and not raw and kind not in ("inlineStr", "str"):
        raise SpreadsheetError(
            f"第 {row} 行 {column_letters(column)} 列是公式但没有计算结果，请在 Excel 里保存后再导入"
        )
    if kind == "inlineStr":
        text = _text_of(cell.find(f"{MAIN}is")).strip()
        return text or None
    if raw is None or raw == "":
        return None
    if kind == "s":
        try:
            text = shared[int(raw)]
        except (ValueError, IndexError) as exc:
            raise SpreadsheetError(f"第 {row} 行 {column_letters(column)} 列引用的共享字符串不存在") from exc
        return text.strip() or None
    if kind == "b":
        return "TRUE" if raw.strip() == "1" else "FALSE"
    if kind == "n":
        try:
            number = float(raw)
        except ValueError as exc:
            raise SpreadsheetError(f"第 {row} 行 {column_letters(column)} 列的数字 {raw!r} 无法识别") from exc
        if not math.isfinite(number):
            # 超出 float 范围（如 1E+400）：原样按文字给出，由解释表格的一方报「不是数字」
            return raw.strip()
        return _formatted(cell, formats, number)
    # str（公式的文字结果）、e（错误值，如 #DIV/0!）、d（ISO 日期）原样按文字给出，由解释表格的一方判断
    return raw.strip() or None


def _formatted(cell: ElementTree.Element, formats: list, number: float) -> Cell:
    """补零格式的非负整数按 Excel 显示的文字返回（瓶身序列号 0001、ELY-0001）；其余照旧是 float。"""
    style = (cell.get("s") or "").strip()
    if not DIGITS.fullmatch(style) or int(style) >= len(formats):
        return number
    padded = formats[int(style)]
    if padded is None or number < 0 or not number.is_integer() or number >= 1e15:
        return number
    prefix, zeros, suffix = padded
    return f"{prefix}{str(int(number)).zfill(zeros)}{suffix}"
