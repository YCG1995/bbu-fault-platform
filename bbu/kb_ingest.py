"""故障知识库导入：CSV/TXT/MD/HTML/DOCX/XLSX/PDF/图片的无依赖解析。

文本型 PDF 抽取失败时不做假成功；扫描 PDF/图片进入人工转写队列，
保留附件与低置信度草稿，管理员确认后才可能 active。
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from .kb_formats import extract_docx, extract_pdf, extract_xlsx, image_info
from .kb_text import (
    CaseDraft, build_cases, clean_title, detect_boards, detect_domains,
    detect_versions, normalize_text, mask_pii,
)

CSV_EXTENSIONS = {".csv", ".tsv"}
TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".log", ".text"}
HTML_EXTENSIONS = {".html", ".htm"}
OFFICE_EXTENSIONS = {".docx", ".xlsx", ".pdf"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
SUPPORTED_EXTENSIONS = (CSV_EXTENSIONS | TEXT_EXTENSIONS | HTML_EXTENSIONS
                        | OFFICE_EXTENSIONS | IMAGE_EXTENSIONS)
QUESTION_KEYS = ("question", "问题", "标题", "故障现象", "title", "q")
ANSWER_KEYS = ("answer", "答案", "回答", "内容", "故障描述", "content", "a")
MAX_SECTION_CHARS = 2 * 1024 * 1024


@dataclass
class DocumentDraft:
    title: str
    title_clean: str
    raw_text: str
    masked_text: str
    source_name: str
    source_type: str
    source_sha256: str = ""
    cases: list[CaseDraft] = field(default_factory=list)
    boards: list[str] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)
    versions: list[str] = field(default_factory=list)
    confidence: float = 0.9
    warnings: list[str] = field(default_factory=list)
    force_review: bool = False
    pii_masked: bool = False

    def public(self) -> dict[str, Any]:
        return {
            "title": self.title, "title_clean": self.title_clean,
            "source_name": self.source_name, "source_type": self.source_type,
            "source_sha256": self.source_sha256, "boards": self.boards,
            "domains": self.domains, "versions": self.versions,
            "confidence": self.confidence, "warnings": self.warnings,
            "force_review": self.force_review, "pii_masked": self.pii_masked,
            "case_count": len(self.cases),
            "cases": [c.public() for c in self.cases],
            "raw_text": self.raw_text, "masked_text": self.masked_text,
        }


def decode_text(data: bytes) -> tuple[str, str]:
    """UTF-8(BOM) 优先，失败回落 GBK/GB18030；返回文本和实际编码。"""
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "gbk"):
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise ValueError("无法识别文本编码（支持 UTF-8/GBK/GB18030）")


class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in ("p", "br", "li", "tr", "div", "h1", "h2", "h3", "h4", "section"):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in ("p", "li", "tr", "div", "h1", "h2", "h3", "h4"):
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._in_title:
            self.title += data
        self.parts.append(data)


def _html_text(raw: str) -> tuple[str, str]:
    parser = _HTMLText()
    try:
        parser.feed(raw)
    except Exception:  # noqa: BLE001 - 坏 HTML 不应阻断纯文本回退
        return "", ""
    title = normalize_text(parser.title)
    text = normalize_text("".join(parser.parts))
    return title, text


def _pick_title(row: dict[str, str], fallback: str) -> str:
    for key in QUESTION_KEYS:
        for actual, value in row.items():
            if normalize_text(actual).lower() == key and str(value or "").strip():
                return str(value).strip()
    return fallback


def _pick_answer(row: dict[str, str], fallback: str) -> str:
    for key in ANSWER_KEYS:
        for actual, value in row.items():
            if normalize_text(actual).lower() == key and str(value or "").strip():
                return str(value).strip()
    return fallback


def _masked(raw_text: str, enabled: bool = True) -> tuple[str, bool]:
    if not enabled:
        return raw_text, False
    return mask_pii(raw_text)


def _document_from_text(title: str, body: str, *, source_name: str, source_type: str,
                        sha256: str = "", confidence: float = 0.9,
                        title_hint: str = "", mask_enabled: bool = True,
                        force_review: bool = False, warnings: list[str] | None = None) -> DocumentDraft:
    raw_text = normalize_text(body)
    masked_text, body_masked = _masked(raw_text, mask_enabled)
    title_value = normalize_text(title_hint or title or source_name)
    masked_title, title_masked = _masked(title_value, mask_enabled)
    # 案例字段是普通用户和模型的检索证据，必须从脱敏文本生成；
    # 原始文本只保留在文档表中供管理员查看。
    evidence_text = masked_text if body_masked else raw_text
    cases, build_warnings = build_cases(masked_title, evidence_text,
                                      source_confidence=confidence)
    extra_warnings = list(warnings or [])
    warnings = extra_warnings + build_warnings
    boards = detect_boards(masked_title + "\n" + evidence_text)
    domains = detect_domains(masked_title + "\n" + evidence_text)
    versions = detect_versions(masked_title + "\n" + evidence_text)
    return DocumentDraft(
        title=masked_title, title_clean=clean_title(masked_title), raw_text=raw_text,
        masked_text=masked_text, source_name=source_name, source_type=source_type,
        source_sha256=sha256, cases=cases, boards=boards, domains=domains,
        versions=versions, confidence=confidence, warnings=warnings,
        force_review=force_review,
        pii_masked=body_masked or title_masked,
    )


def parse_csv_bytes(data: bytes, *, source_name: str, sha256: str = "",
                    mask_enabled: bool = True) -> list[DocumentDraft]:
    text, _encoding = decode_text(data)
    text = text.lstrip("\ufeff")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    if not reader.fieldnames:
        raise ValueError("CSV 缺少表头")
    rows: list[DocumentDraft] = []
    for index, row in enumerate(reader, start=1):
        if not isinstance(row, dict):
            continue
        title = _pick_title(row, f"{source_name} 第 {index} 条")
        answer = _pick_answer(row, "\n".join(str(v or "") for v in row.values()))
        if not str(answer or "").strip():
            continue
        row_sha = hashlib.sha256(
            f"{title}\n{answer}".encode("utf-8")).hexdigest()
        rows.append(_document_from_text(
            title, answer, source_name=source_name, source_type="csv",
            sha256=row_sha, title_hint=title, mask_enabled=mask_enabled))
    return rows


def parse_text_bytes(data: bytes, *, source_name: str, suffix: str = ".txt",
                     sha256: str = "", mask_enabled: bool = True) -> list[DocumentDraft]:
    text, _encoding = decode_text(data)
    raw = text[:MAX_SECTION_CHARS]
    title = ""
    body = raw
    if suffix in {".md", ".markdown"}:
        for line in raw.splitlines():
            match = re.match(r"^\s*#{1,3}\s+(.+?)\s*$", line)
            if match:
                title = match.group(1).strip()
                break
        if not title:
            title = next((line.strip() for line in raw.splitlines()
                          if line.strip() and not line.strip().startswith("#")),
                         Path(source_name).stem)
    else:
        first = next((line.strip() for line in raw.splitlines() if line.strip()), "")
        title = first[:120] if len(first) <= 120 and not first.startswith("【") else Path(source_name).stem
        # 模板文本的第一行通常就是问题标题；正文仍保留整篇供抽取。
        body = raw
    return [_document_from_text(title, body, source_name=source_name,
                                source_type=suffix.lstrip("."), sha256=sha256,
                                title_hint=title, mask_enabled=mask_enabled)]


def parse_html_bytes(data: bytes, *, source_name: str, sha256: str = "",
                     mask_enabled: bool = True) -> list[DocumentDraft]:
    text, _encoding = decode_text(data)
    html_title, body = _html_text(text)
    title = html_title or Path(source_name).stem
    if not body:
        body = normalize_text(re.sub(r"<[^>]+>", " ", text))
    return [_document_from_text(title, body, source_name=source_name,
                                source_type="html", sha256=sha256, title_hint=title,
                                mask_enabled=mask_enabled)]


def parse_file(path: Path, filename: str, *, sha256: str = "",
               pii_masked: bool = True) -> list[DocumentDraft]:
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise ValueError(
            f"暂不支持 {suffix or '未知'} 格式；当前支持 CSV/TXT/MD/HTML/DOCX/XLSX/PDF/PNG/JPG/WebP")
    data = path.read_bytes()
    if suffix in CSV_EXTENSIONS:
        docs = parse_csv_bytes(data, source_name=filename, sha256=sha256,
                               mask_enabled=pii_masked)
    elif suffix in HTML_EXTENSIONS:
        docs = parse_html_bytes(data, source_name=filename, sha256=sha256,
                                mask_enabled=pii_masked)
    elif suffix == ".docx":
        docs = parse_docx_bytes(data, source_name=filename, sha256=sha256,
                                mask_enabled=pii_masked)
    elif suffix == ".xlsx":
        docs = parse_xlsx_bytes(data, source_name=filename, sha256=sha256,
                                mask_enabled=pii_masked)
    elif suffix == ".pdf":
        docs = parse_pdf_bytes(data, source_name=filename, sha256=sha256,
                               mask_enabled=pii_masked)
    elif suffix in IMAGE_EXTENSIONS:
        docs = parse_image_bytes(data, source_name=filename, sha256=sha256,
                                 mask_enabled=pii_masked)
    else:
        docs = parse_text_bytes(data, source_name=filename, suffix=suffix, sha256=sha256,
                                mask_enabled=pii_masked)
    if not docs:
        raise ValueError("文件没有解析出可导入内容")
    return docs


def parse_docx_bytes(data: bytes, *, source_name: str, sha256: str = "",
                     mask_enabled: bool = True) -> list[DocumentDraft]:
    doc_title, body, warnings = extract_docx(data)
    return [_document_from_text(doc_title, body, source_name=source_name,
                                source_type="docx", sha256=sha256,
                                title_hint=doc_title, mask_enabled=mask_enabled,
                                warnings=warnings)]


def parse_xlsx_bytes(data: bytes, *, source_name: str, sha256: str = "",
                     mask_enabled: bool = True) -> list[DocumentDraft]:
    sheets = extract_xlsx(data)
    docs: list[DocumentDraft] = []
    for sheet in sheets:
        headers = [str(x) for x in sheet.get("headers") or []]
        rows = sheet.get("rows") or []
        for index, values in enumerate(rows, start=1):
            row = {headers[i] if i < len(headers) else f"列{i + 1}": str(value or "")
                   for i, value in enumerate(values)}
            title = _pick_title(row, f"{source_name} / {sheet['sheet']} 第 {index} 条")
            answer = _pick_answer(row, "\n".join(row.values()))
            if not str(answer or "").strip():
                continue
            row_sha = hashlib.sha256(
                f"{sheet['sheet']}\n{title}\n{answer}".encode("utf-8")).hexdigest()
            docs.append(_document_from_text(
                title, answer, source_name=source_name, source_type="xlsx",
                sha256=row_sha, title_hint=title, mask_enabled=mask_enabled,
                warnings=[f"来源工作表：{sheet['sheet']}"]))
    if not docs:
        raise ValueError("XLSX 没有解析出可导入内容")
    return docs


def _placeholder_document(*, title: str, source_name: str, source_type: str,
                          sha256: str, reason: str, mask_enabled: bool,
                          detail: str = "") -> DocumentDraft:
    body = f"【详细描述】{detail or title}\n【人工转写】{reason}"
    return _document_from_text(
        title or Path(source_name).stem, body, source_name=source_name,
        source_type=source_type, sha256=sha256, confidence=0.2,
        title_hint=title or Path(source_name).stem, mask_enabled=mask_enabled,
        force_review=True, warnings=[reason])


def parse_pdf_bytes(data: bytes, *, source_name: str, sha256: str = "",
                    mask_enabled: bool = True) -> list[DocumentDraft]:
    text, warnings = extract_pdf(data)
    if not text:
        return [_placeholder_document(
            title=Path(source_name).stem, source_name=source_name, source_type="pdf",
            sha256=sha256, reason="PDF 无可提取文本层，需 OCR 或人工转写",
            mask_enabled=mask_enabled)]
    title = next((line.strip() for line in text.splitlines() if line.strip()),
                 Path(source_name).stem)
    return [_document_from_text(title, text, source_name=source_name,
                                source_type="pdf", sha256=sha256,
                                title_hint=title, mask_enabled=mask_enabled,
                                warnings=warnings)]


def parse_image_bytes(data: bytes, *, source_name: str, sha256: str = "",
                      mask_enabled: bool = True) -> list[DocumentDraft]:
    mime, width, height = image_info(data)
    size_note = f"{mime} {width}x{height}" if width and height else mime
    return [_placeholder_document(
        title=Path(source_name).stem, source_name=source_name, source_type="image",
        sha256=sha256, reason="图片未做本地 OCR；有视觉模型时转写，否则人工补录",
        mask_enabled=mask_enabled, detail=f"{source_name}（{size_note}）")]


def parse_paste(text: str, *, title: str = "", source_name: str = "粘贴文本") -> list[DocumentDraft]:
    raw = normalize_text(text)
    if not raw:
        raise ValueError("粘贴文本为空")
    title_value = title.strip() or next(
        (line.strip() for line in raw.splitlines() if line.strip()), source_name)
    return [_document_from_text(title_value, raw, source_name=source_name,
                                source_type="paste", title_hint=title_value)]
