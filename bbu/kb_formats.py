"""知识库文档格式适配（纯标准库）。

支持的容器：
- DOCX：zip + xml，抽取段落/表格文本；
- XLSX：zip + xml，抽取共享字符串/内联字符串/公式缓存值；
- 文本型 PDF：读取 FlateDecode 流中的 Tj/TJ 文本操作符；
- PNG/JPEG/WebP：只做格式与尺寸校验，不本地 OCR。

扫描 PDF、图片和嵌入图片不在这里伪造文本：调用方会将其放入人工
转写队列，保留附件并标记 needs_review。
"""
from __future__ import annotations

import io
import re
import struct
import zipfile
import zlib
import xml.etree.ElementTree as ET
from pathlib import Path

WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
SHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
MAX_XML_BYTES = 80 * 1024 * 1024
MAX_CELL_ROWS = 50000
MAX_PDF_STREAM_BYTES = 20 * 1024 * 1024


def _w(tag: str) -> str:
    return f"{{{WORD_NS}}}{tag}"


def _s(tag: str) -> str:
    return f"{{{SHEET_NS}}}{tag}"


def _open_zip(data: bytes, *, label: str) -> zipfile.ZipFile:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ValueError(f"{label} 压缩包损坏或不是有效的 Office 文件") from exc
    total = sum(max(0, int(info.file_size or 0)) for info in archive.infolist())
    if total > MAX_XML_BYTES:
        raise ValueError(f"{label} 解压后超过 {MAX_XML_BYTES // (1024 * 1024)} MB")
    return archive


def _read_xml(archive: zipfile.ZipFile, name: str) -> ET.Element:
    try:
        raw = archive.read(name)
    except KeyError as exc:
        raise ValueError(f"缺少 {name}") from exc
    if len(raw) > MAX_XML_BYTES:
        raise ValueError(f"{name} 过大")
    try:
        return ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ValueError(f"{name} XML 解析失败：{exc}") from exc


def _word_text(element: ET.Element) -> str:
    """抽取 w:p / w:tc 中的可见文本，保留 tab 与换行。"""
    parts: list[str] = []
    for node in element.iter():
        if node.tag == _w("t"):
            parts.append(node.text or "")
        elif node.tag == _w("tab"):
            parts.append("\t")
        elif node.tag in {_w("br"), _w("cr")}:
            parts.append("\n")
    return "".join(parts).strip()


def extract_docx(data: bytes) -> tuple[str, str, list[str]]:
    """返回 (标题, 正文, 警告)。"""
    archive = _open_zip(data, label="DOCX")
    root = _read_xml(archive, "word/document.xml")
    body = root.find(_w("body"))
    if body is None:
        raise ValueError("DOCX 缺少正文")
    lines: list[str] = []
    title = ""
    for block in list(body):
        if block.tag == _w("p"):
            text = _word_text(block)
            if not text:
                continue
            style_node = block.find(f"{_w('pPr')}/{_w('pStyle')}")
            style = (style_node.get(_w("val")) or "") if style_node is not None else ""
            if style.lower().startswith("heading"):
                lines.append(f"# {text}")
                if not title:
                    title = text
            else:
                lines.append(text)
        elif block.tag == _w("tbl"):
            for row in block.findall(_w("tr")):
                cells = [_word_text(cell) for cell in row.findall(_w("tc"))]
                if any(cells):
                    lines.append(" | ".join(cells))
    text = "\n".join(lines).strip()
    media = [n for n in archive.namelist() if n.startswith("word/media/")]
    warnings: list[str] = []
    if media:
        warnings.append(f"DOCX 含 {len(media)} 个嵌入图片，当前未做视觉转写")
    if not text:
        raise ValueError("DOCX 没有可提取的文字")
    if not title:
        title = next((line.lstrip("# ").strip() for line in lines if line.strip()),
                     Path("document").stem)
    return title[:200], text, warnings


def _xlsx_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = _read_xml(archive, "xl/sharedStrings.xml")
    values: list[str] = []
    for item in root.findall(_s("si")):
        parts = [node.text or "" for node in item.iter() if node.tag == _s("t")]
        values.append("".join(parts))
    return values


def _xlsx_sheets(archive: zipfile.ZipFile) -> list[tuple[str, str]]:
    """返回 (显示名, worksheet 路径)，按 workbook.xml 顺序。"""
    if "xl/workbook.xml" not in archive.namelist():
        raise ValueError("XLSX 缺少 workbook.xml")
    root = _read_xml(archive, "xl/workbook.xml")
    rels: dict[str, str] = {}
    if "xl/_rels/workbook.xml.rels" in archive.namelist():
        rel_root = _read_xml(archive, "xl/_rels/workbook.xml.rels")
        for rel in rel_root:
            rel_id = rel.get("Id")
            target = rel.get("Target")
            if rel_id and target:
                rels[rel_id] = target
    sheets: list[tuple[str, str]] = []
    for sheet in root.iter(_s("sheet")):
        name = sheet.get("name") or f"Sheet{len(sheets) + 1}"
        rel_id = sheet.get(f"{{{REL_NS}}}id") or ""
        target = rels.get(rel_id, "")
        if not target:
            target = f"worksheets/sheet{len(sheets) + 1}.xml"
        path = target.lstrip("/")
        if not path.startswith("xl/"):
            path = "xl/" + path
        if path in archive.namelist():
            sheets.append((name, path))
    if not sheets:
        for path in sorted(n for n in archive.namelist()
                           if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")):
            sheets.append((Path(path).stem, path))
    return sheets


def _cell_text(cell: ET.Element, shared: list[str]) -> str:
    kind = cell.get("t") or ""
    value_node = cell.find(_s("v"))
    inline_node = cell.find(f"{_s('is')}/{_s('t')}")
    if kind == "inlineStr":
        return "".join(n.text or "" for n in cell.iter(_s("t"))).strip()
    raw = value_node.text if value_node is not None else ""
    if raw is None:
        return ""
    if kind == "s":
        try:
            return shared[int(raw)]
        except (ValueError, IndexError):
            return ""
    if kind == "b":
        return "TRUE" if raw == "1" else "FALSE"
    return str(raw).strip()


def _column_index(ref: str) -> int:
    letters = re.match(r"([A-Za-z]+)", ref or "")
    if not letters:
        return 0
    n = 0
    for ch in letters.group(1).upper():
        n = n * 26 + (ord(ch) - 64)
    return max(0, n - 1)


def extract_xlsx(data: bytes) -> list[dict[str, object]]:
    """按 sheet 抽取行，返回 [{sheet, headers, rows}]。"""
    archive = _open_zip(data, label="XLSX")
    shared = _xlsx_shared_strings(archive)
    result: list[dict[str, object]] = []
    for sheet_name, sheet_path in _xlsx_sheets(archive):
        root = _read_xml(archive, sheet_path)
        raw_rows: list[list[str]] = []
        for row in root.iter(_s("row")):
            values: list[str] = []
            for cell in row.findall(_s("c")):
                idx = _column_index(cell.get("r") or "")
                while len(values) < idx:
                    values.append("")
                values.append(_cell_text(cell, shared))
            raw_rows.append(values)
            if len(raw_rows) > MAX_CELL_ROWS:
                raise ValueError(f"{sheet_name} 行数超过上限")
        raw_rows = [r for r in raw_rows if any(str(v or "").strip() for v in r)]
        if not raw_rows:
            continue
        # 第一行是否为表头：只要出现问题/答案等已知列名；否则按数据行处理。
        known = {"问题", "答案", "标题", "故障现象", "question", "answer", "title", "content"}
        first = [str(v or "").strip().casefold() for v in raw_rows[0]]
        has_header = any(v in known for v in first)
        if has_header:
            headers = [str(v or "").strip() or f"列{idx + 1}"
                       for idx, v in enumerate(raw_rows[0])]
            rows_data = raw_rows[1:]
        else:
            headers = [f"列{idx + 1}" for idx in range(max(len(r) for r in raw_rows))]
            rows_data = raw_rows
        result.append({"sheet": sheet_name, "headers": headers, "rows": rows_data})
    if not result:
        raise ValueError("XLSX 没有可读取的数据行")
    return result


_PDF_STRING = re.compile(rb"\((?:\\.|[^()\\])*\)")
_PDF_HEX = re.compile(rb"<([0-9A-Fa-f\s]+)>")


def _pdf_unescape(raw: bytes) -> bytes:
    out: list[int] = []
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch != 0x5c:
            out.append(ch)
            i += 1
            continue
        i += 1
        if i >= len(raw):
            break
        c = raw[i]
        mapping = {0x6e: 10, 0x72: 13, 0x74: 9, 0x62: 8, 0x66: 12,
                   0x28: 0x28, 0x29: 0x29, 0x5c: 0x5c}
        if c in mapping:
            out.append(mapping[c]); i += 1
        elif 0x30 <= c <= 0x37:
            digits = 0
            value = 0
            while i < len(raw) and digits < 3 and 0x30 <= raw[i] <= 0x37:
                value = value * 8 + (raw[i] - 0x30)
                i += 1; digits += 1
            out.append(value)
        elif c in (0x0a, 0x0d):
            i += 1
            if c == 0x0d and i < len(raw) and raw[i] == 0x0a:
                i += 1
        else:
            out.append(c); i += 1
    return bytes(out)


def _decode_pdf_string(raw: bytes) -> str:
    if not raw:
        return ""
    if raw.startswith(b"\xfe\xff"):
        candidate = raw[2:]
        try:
            return candidate.decode("utf-16-be")
        except UnicodeDecodeError:
            return ""
    try:
        text = raw.decode("utf-8")
        if any(ord(ch) > 127 for ch in text):
            return text
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode("latin-1")
    except UnicodeDecodeError:
        return ""


def _pdf_stream_text(stream: bytes) -> str:
    parts: list[str] = []
    # 先处理 [ ... ] TJ 数组，再处理 ( ... ) Tj；避免数组内字符串重复。
    for array in re.finditer(rb"\[(.*?)\]\s*TJ", stream, re.S):
        for match in _PDF_STRING.finditer(array.group(1)):
            parts.append(_decode_pdf_string(_pdf_unescape(match.group(0)[1:-1])))
    for match in re.finditer(rb"\((?:\\.|[^()\\])*\)\s*Tj", stream):
        token = match.group(0).split(b")", 1)[0] + b")"
        parts.append(_decode_pdf_string(_pdf_unescape(token[1:-1])))
    for match in _PDF_HEX.finditer(stream):
        hex_text = re.sub(rb"\s+", b"", match.group(1))
        if len(hex_text) % 2:
            hex_text += b"0"
        try:
            parts.append(_decode_pdf_string(bytes.fromhex(hex_text.decode("ascii"))))
        except (ValueError, UnicodeDecodeError):
            continue
    return "".join(parts)


def extract_pdf(data: bytes) -> tuple[str, list[str]]:
    """抽取文本型 PDF；返回文本和警告。扫描件返回空文本。"""
    if not data.startswith(b"%PDF"):
        raise ValueError("不是 PDF 文件")
    chunks: list[str] = []
    warnings: list[str] = []
    for match in re.finditer(rb"stream\r?\n", data):
        start = match.end()
        end = data.find(b"endstream", start)
        if end < 0:
            continue
        raw = data[start:end].rstrip(b"\r\n")
        if len(raw) > MAX_PDF_STREAM_BYTES:
            warnings.append("PDF 存在超大流，已跳过")
            continue
        header = data[max(0, match.start() - 400):match.start()].lower()
        if b"/image" in header:
            continue
        if b"flate" in header:
            try:
                raw = zlib.decompress(raw)
            except zlib.error:
                continue
        if len(raw) > MAX_PDF_STREAM_BYTES:
            continue
        if b"BT" not in raw and b"Tj" not in raw and b"TJ" not in raw:
            continue
        chunks.append(_pdf_stream_text(raw))
    text = re.sub(r"[ \t]+", " ", "\n".join(chunks)).strip()
    # 过滤只有乱码/控制字符的“假文本”。
    good = sum(1 for ch in text if ch.isalnum() or ch in "，。；：、（）【】\n ")
    if text and good / max(1, len(text)) < 0.25:
        text = ""
        warnings.append("PDF 文本层不可识别，需要 OCR 或人工转写")
    if not text:
        warnings.append("PDF 没有可提取的文本层，需要 OCR 或人工转写")
    return text, warnings


_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}


def image_info(data: bytes) -> tuple[str, int, int]:
    """校验 magic bytes，返回 (mime, width, height)。不本地解码。"""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        if len(data) < 24 or data[12:16] != b"IHDR":
            raise ValueError("PNG 文件头损坏")
        width, height = struct.unpack(">II", data[16:24])
        return "image/png", int(width), int(height)
    if data.startswith(b"\xff\xd8\xff"):
        pos = 2
        while pos + 4 <= len(data):
            if data[pos] != 0xFF:
                pos += 1; continue
            marker = data[pos + 1]
            pos += 2
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                continue
            if pos + 2 > len(data):
                break
            length = struct.unpack(">H", data[pos:pos + 2])[0]
            if length < 2 or pos + length > len(data):
                break
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                          0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                if length >= 7:
                    height, width = struct.unpack(">HH", data[pos + 3:pos + 7])
                    return "image/jpeg", int(width), int(height)
            pos += length
        raise ValueError("JPEG 没有可识别的尺寸信息")
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        if len(data) < 30:
            raise ValueError("WebP 文件头损坏")
        chunk = data[12:16]
        if chunk == b"VP8X":
            width = 1 + int.from_bytes(data[24:27], "little")
            height = 1 + int.from_bytes(data[27:30], "little")
            return "image/webp", width, height
        # VP8/VP8L 尺寸解析对少量变体不稳定，校验通过即可；由视觉/人工补全。
        return "image/webp", 0, 0
    raise ValueError("不支持的图片格式（仅 PNG/JPEG/WebP）")
