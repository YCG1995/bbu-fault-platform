"""知识库批量导出报告：HTML/Markdown/CSV/最小 DOCX + ZIP 清单。

全部标准库实现。DOCX 直接写最小 OOXML，不依赖 python-docx；HTML 全量转义，
CSV 防公式注入，ZIP 只使用固定白名单路径。
"""
from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import zipfile
from typing import Any

from .kb_service import KBError
from .kb_text import mask_pii
from .util import iso_now, safe_filename

MAX_REPORT_ROWS = 2000
FORMATS = ("html", "md", "csv", "docx")


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _safe_cell(value: Any) -> str:
    text = str(value if value is not None else "")
    if text.startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


def _xml(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


class KBReport:
    def __init__(self, kb) -> None:  # noqa: ANN001
        self.kb = kb
        self.db = kb.db

    def _admin(self, user: dict[str, Any]) -> None:
        if (user or {}).get("role") != "admin":
            raise KBError("仅管理员可导出知识库报告", 403, "forbidden")

    def _text(self, value: Any, reveal: bool) -> str:
        text = str(value or "")
        return text if reveal else mask_pii(text)[0]

    def generate(self, user: dict[str, Any], *, kb_id: int = 1,
                 doc_ids: list[str] | None = None, case_ids: list[str] | None = None,
                 formats: list[str] | None = None, reveal: bool = False,
                 title: str = "") -> tuple[bytes, str, str]:
        self._admin(user)
        reveal = bool(reveal)
        selected_formats = [x for x in (formats or list(FORMATS)) if x in FORMATS]
        if not selected_formats:
            raise KBError("没有有效的导出格式", 400, "bad_format")
        docs, cases = self._collect(int(kb_id), doc_ids=doc_ids, case_ids=case_ids,
                                    reveal=reveal)
        if not docs:
            raise KBError("没有可导出的文档", 409, "empty_report")
        now = iso_now()
        filters = {"kb_id": int(kb_id), "doc_ids": [str(x) for x in (doc_ids or [])],
                   "case_ids": [str(x) for x in (case_ids or [])],
                   "reveal": reveal, "formats": selected_formats}
        files: list[tuple[str, bytes]] = []
        if "html" in selected_formats:
            files.append(("report.html", self._html(docs, cases, title, now).encode("utf-8")))
        if "md" in selected_formats:
            files.append(("report.md", self._markdown(docs, cases, title, now).encode("utf-8")))
        if "csv" in selected_formats:
            files.append(("data.csv", ("\ufeff" + self._csv(docs, cases)).encode("utf-8")))
        if "docx" in selected_formats:
            files.append(("report.docx", self._docx(docs, cases, title, now)))
        manifest = {
            "schema": "bbu-kb-report/1", "generated_at": now, "title": title,
            "kb_id": int(kb_id), "filters": filters, "documents": len(docs),
            "cases": len(cases), "files": {
                name: {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
                for name, data in files
            },
            "security": {"masked": not reveal, "pii_masked_default": True},
        }
        files.append(("manifest.json",
                      json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")))
        buf = io.BytesIO()
        names = ["report.html", "report.md", "data.csv", "report.docx", "manifest.json"]
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in files:
                if name not in names:
                    continue  # 固定白名单，杜绝 ../ 路径
                archive.writestr(name, data)
        stem = safe_filename(title or f"kb-report-{int(kb_id)}") or "kb-report"
        return buf.getvalue(), f"{stem}-{now.replace(':', '').replace('-', '')}.zip", "application/zip"

    def _collect(self, kb_id: int, *, doc_ids: list[str] | None = None,
                 case_ids: list[str] | None = None, reveal: bool = False
                 ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        where = ["d.kb_id=?", "d.deleted_at IS NULL"]
        args: list[Any] = [int(kb_id)]
        if doc_ids:
            ids = [str(x) for x in doc_ids if str(x)]
            if len(ids) > MAX_REPORT_ROWS:
                raise KBError(f"单次最多导出 {MAX_REPORT_ROWS} 个文档", 413, "too_many_docs")
            placeholders = ",".join("?" for _ in ids)
            where.append(f"d.id IN ({placeholders})")
            args.extend(ids)
        elif case_ids:
            ids = [str(x) for x in case_ids if str(x)]
            if len(ids) > MAX_REPORT_ROWS:
                raise KBError(f"单次最多导出 {MAX_REPORT_ROWS} 个案例", 413, "too_many_cases")
            placeholders = ",".join("?" for _ in ids)
            where.append(f"EXISTS (SELECT 1 FROM kb_cases c WHERE c.doc_id=d.id "
                         f"AND c.id IN ({placeholders}))")
            args.extend(ids)
        docs = [dict(x) for x in self.db.all(
            "SELECT d.*, (SELECT COUNT(*) FROM kb_cases c WHERE c.doc_id=d.id "
            "AND c.status='active') AS case_count FROM kb_documents d WHERE " +
            " AND ".join(where) + " ORDER BY d.created_at LIMIT ?",
            (*args, MAX_REPORT_ROWS))]
        if not docs:
            return [], []
        doc_ids_set = {str(d["id"]) for d in docs}
        case_where = ["c.doc_id IN (" + ",".join("?" for _ in doc_ids_set) + ")",
                      "c.status='active'"]
        case_args: list[Any] = list(doc_ids_set)
        if case_ids:
            wanted = {str(x) for x in case_ids}
            # 只导出用户选中案例；文档元数据仍保留以便定位。
            cases = [dict(x) for x in self.db.all(
                "SELECT * FROM kb_cases WHERE status='active' AND doc_id IN (" +
                ",".join("?" for _ in doc_ids_set) + ") ORDER BY doc_id,ordinal",
                tuple(doc_ids_set))]
            cases = [c for c in cases if str(c["id"]) in wanted]
        else:
            case_where = ["doc_id IN (" + ",".join("?" for _ in doc_ids_set) + ")",
                          "status='active'"]
            cases = [dict(x) for x in self.db.all(
                "SELECT * FROM kb_cases WHERE " + " AND ".join(case_where) +
                " ORDER BY doc_id,ordinal LIMIT ?", (*doc_ids_set, MAX_REPORT_ROWS))]
        public_docs = []
        for doc in docs:
            public_docs.append({
                "id": doc.get("id"), "title": self._text(doc.get("title"), reveal),
                "source_name": self._text(doc.get("source_name"), reveal),
                "source_type": doc.get("source_type") or "",
                "boards": _json_list(doc.get("boards_json")),
                "domains": _json_list(doc.get("domains_json")),
                "versions": _json_list(doc.get("versions_json")),
                "tags": _json_list(doc.get("tags_json")),
                "quality": float(doc.get("quality") or 0),
                "updated_at": doc.get("updated_at") or "",
                "case_count": int(doc.get("case_count") or 0),
                "text": (self._text(doc.get("raw_text"), reveal) if reveal
                         else self._text(doc.get("masked_text"), False)),
            })
        public_cases = []
        for case in cases:
            fields = {}
            for key in ("title", "symptom", "environment", "expected", "actual", "repro",
                        "cause", "cause_detail", "solution", "prevention"):
                fields[key] = self._text(case.get(key), reveal)
            fields.update({
                "id": case.get("id"), "doc_id": case.get("doc_id"),
                "ordinal": int(case.get("ordinal") or 1),
                "boards": _json_list(case.get("boards_json")),
                "domains": _json_list(case.get("domains_json")),
                "versions": _json_list(case.get("versions_json")),
                "quality": float(case.get("quality") or 0),
                "uri": f"faultkb://case/{case.get('id')}",
            })
            public_cases.append(fields)
        return public_docs, public_cases

    def _html(self, docs: list[dict[str, Any]], cases: list[dict[str, Any]],
              title: str, generated_at: str) -> str:
        rows = []
        for case in cases:
            rows.append(
                "<tr><td>" + html.escape(str(case.get("title") or "")) + "</td>"
                "<td>" + html.escape(" / ".join(str(x) for x in case.get("boards") or []))
                + "</td><td>" + html.escape(str(case.get("symptom") or "")) + "</td>"
                "<td>" + html.escape(str(case.get("cause") or case.get("cause_detail") or ""))
                + "</td><td>" + html.escape(str(case.get("solution") or "")) + "</td></tr>")
        toc = "".join(f"<li><a href=\"#doc-{i}\">{html.escape(str(d.get('title') or d.get('id')))}</a></li>"
                      for i, d in enumerate(docs))
        doc_blocks = []
        for i, doc in enumerate(docs):
            doc_cases = [c for c in cases if str(c.get("doc_id")) == str(doc.get("id"))]
            doc_blocks.append(
                f"<section id=\"doc-{i}\"><h2>{html.escape(str(doc.get('title') or doc.get('id')))}</h2>"
                f"<p class=\"meta\">来源：{html.escape(str(doc.get('source_name') or '—'))} · "
                f"{len(doc_cases)} 案例 · 更新 {html.escape(str(doc.get('updated_at') or '—'))}</p>"
                f"<p class=\"tags\">{[html.escape(str(x)) for x in (doc.get('boards') or []) + (doc.get('domains') or []) + (doc.get('versions') or [])]}</p>"
                f"<pre>{html.escape(str(doc.get('text') or ''))}</pre>")
            for case in doc_cases:
                doc_blocks.append(
                    f"<h3>{html.escape(str(case.get('title') or ''))}</h3>"
                    f"<p><b>现象：</b>{html.escape(str(case.get('symptom') or ''))}</p>"
                    f"<p><b>原因：</b>{html.escape(str(case.get('cause') or case.get('cause_detail') or ''))}</p>"
                    f"<p><b>方案：</b>{html.escape(str(case.get('solution') or ''))}</p>"
                    f"<p class=\"meta\">{html.escape(str(case.get('uri') or ''))}</p>")
            doc_blocks.append("</section>")
        return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>{html.escape(title or '知识库导出报告')}</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Microsoft YaHei',sans-serif;line-height:1.65;color:#1f2937;max-width:1100px;margin:32px auto;padding:0 24px}}
h1{{font-size:28px}} h2{{margin-top:32px;border-top:1px solid #e5e7eb;padding-top:20px}}
table{{border-collapse:collapse;width:100%;font-size:13px}} th,td{{border:1px solid #e5e7eb;padding:8px;vertical-align:top;text-align:left}}
th{{background:#f9fafb}} pre{{white-space:pre-wrap;background:#f9fafb;border:1px solid #e5e7eb;padding:12px;font-size:13px}}
.meta{{color:#6b7280;font-size:12px}} .tags{{color:#374151;font-size:12px}} .note{{background:#fff7ed;border:1px solid #fed7aa;padding:10px;border-radius:8px}}
@media print{{body{{max-width:none;margin:0}} .note{{display:none}}}}
</style></head><body>
<h1>{html.escape(title or '知识库导出报告')}</h1>
<p class="meta">生成时间 {html.escape(generated_at)} · {len(docs)} 文档 / {len(cases)} 案例</p>
<p class="note">默认导出脱敏文本；服务端不生成 PDF，请在浏览器中打开后“打印 → 另存为 PDF”。</p>
<h2>目录</h2><ul>{toc}</ul>
<h2>案例明细</h2><div style="overflow:auto"><table><thead><tr><th>标题</th><th>板卡</th><th>现象</th><th>原因</th><th>方案</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
{''.join(doc_blocks)}
</body></html>"""

    def _markdown(self, docs: list[dict[str, Any]], cases: list[dict[str, Any]],
                  title: str, generated_at: str) -> str:
        lines = [f"# {title or '知识库导出报告'}", "",
                 f"- 生成时间：{generated_at}",
                 f"- 文档：{len(docs)}；案例：{len(cases)}", "",
                 "> 默认脱敏；PDF 请用浏览器打开 HTML 后打印。", "", "## 文档", ""]
        for doc in docs:
            lines += [f"### {doc.get('title') or doc.get('id')}", "",
                      f"- 来源：{doc.get('source_name') or '—'}",
                      f"- 板卡/领域/版本：{' / '.join(str(x) for x in (doc.get('boards') or []) + (doc.get('domains') or []) + (doc.get('versions') or [])) or '—'}",
                      "", str(doc.get("text") or ""), ""]
        lines += ["## 案例", "", "| 标题 | 板卡 | 现象 | 原因 | 方案 |", "| --- | --- | --- | --- | --- |"]
        for case in cases:
            def cell(key: str) -> str:
                return str(case.get(key) or "").replace("|", "/").replace("\n", " ")
            lines.append(f"| {cell('title')} | {' / '.join(str(x) for x in case.get('boards') or [])} | "
                         f"{cell('symptom')} | {cell('cause') or cell('cause_detail')} | {cell('solution')} |")
        return "\n".join(lines) + "\n"

    def _csv(self, docs: list[dict[str, Any]], cases: list[dict[str, Any]]) -> str:
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["kind", "doc_id", "case_id", "title", "boards", "domains",
                         "versions", "symptom", "cause", "solution", "source", "uri"])
        for doc in docs:
            writer.writerow([_safe_cell("doc"), _safe_cell(doc.get("id")), "",
                             _safe_cell(doc.get("title")), _safe_cell(" / ".join(doc.get("boards") or [])),
                             _safe_cell(" / ".join(doc.get("domains") or [])),
                             _safe_cell(" / ".join(doc.get("versions") or [])), "", "", "",
                             _safe_cell(doc.get("source_name")), ""])
        for case in cases:
            writer.writerow([_safe_cell("case"), _safe_cell(case.get("doc_id")),
                             _safe_cell(case.get("id")), _safe_cell(case.get("title")),
                             _safe_cell(" / ".join(case.get("boards") or [])),
                             _safe_cell(" / ".join(case.get("domains") or [])),
                             _safe_cell(" / ".join(case.get("versions") or [])),
                             _safe_cell(case.get("symptom")), _safe_cell(case.get("cause") or case.get("cause_detail")),
                             _safe_cell(case.get("solution")), "",
                             _safe_cell(case.get("uri"))])
        return out.getvalue()

    def _docx(self, docs: list[dict[str, Any]], cases: list[dict[str, Any]],
              title: str, generated_at: str) -> bytes:
        paragraphs = [f"{title or '知识库导出报告'}", f"生成时间：{generated_at}",
                      f"文档 {len(docs)} 个 / 案例 {len(cases)} 条；默认脱敏。"]
        for doc in docs:
            paragraphs.append(f"文档：{doc.get('title') or doc.get('id')}")
            paragraphs.append(str(doc.get("text") or ""))
        for case in cases:
            paragraphs.append(f"案例：{case.get('title') or ''}")
            paragraphs.append(f"现象：{case.get('symptom') or ''}")
            paragraphs.append(f"原因：{case.get('cause') or case.get('cause_detail') or ''}")
            paragraphs.append(f"方案：{case.get('solution') or ''}")
        body = "".join(
            f"<w:p><w:r><w:t xml:space=\"preserve\">{_xml(text)}</w:t></w:r></w:p>"
            for text in paragraphs)
        document = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:body>{body}<w:sectPr><w:pgSz w:w="11906" w:h="16838"/></w:sectPr></w:body></w:document>"""
        rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""
        content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""
        styles = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>
</w:styles>"""
        doc_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", content_types)
            archive.writestr("_rels/.rels", rels)
            archive.writestr("word/document.xml", document)
            archive.writestr("word/styles.xml", styles)
            archive.writestr("word/_rels/document.xml.rels", doc_rels)
        return buf.getvalue()
