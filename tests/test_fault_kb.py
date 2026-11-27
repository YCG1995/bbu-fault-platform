"""故障知识库核心回归：文本、索引、召回、导入、审核、问答与 PII。"""
from __future__ import annotations

import io
import json
import struct
import sys
import tempfile
import unittest
import zipfile
from types import SimpleNamespace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor"))

from bbu.ai import AIResult, NoModelError  # noqa: E402
from bbu.config import Config  # noqa: E402
from bbu.db import Database, SCHEMA_VERSION  # noqa: E402
from bbu.kb_index import IndexCase, KBIndex  # noqa: E402
from bbu.kb_ingest import _document_from_text, parse_file, parse_paste  # noqa: E402
from bbu.kb_service import KBService  # noqa: E402
from bbu.kb_text import build_cases, extract_sections, mask_pii  # noqa: E402


class KBTextTests(unittest.TestCase):
    def test_template_inline_section_and_derivation(self) -> None:
        answer = (
            "【详细描述:单板反复复位，业务中断】\n"
            "【原因分析】EPLD 喂狗时序异常。\n【处理方案】升级 EPLD。\n"
            "【原因分析】电源瞬断导致复位。\n【处理方案】更换电源模块。\n"
            "【故障建议】升级前核对版本。"
        )
        preamble, sections = extract_sections(answer)
        self.assertEqual(preamble, "")
        self.assertEqual(sum(1 for s in sections if s.kind == "cause"), 2)
        self.assertEqual(sections[0].label, "详细描述")
        cases, warnings = build_cases("VBPE2 反复复位", answer)
        self.assertEqual(len(cases), 2)
        self.assertTrue(cases[1].is_derived)
        self.assertIn("电源", cases[1].cause)
        self.assertEqual(cases[0].solution, "升级 EPLD。")
        self.assertEqual(cases[1].solution, "更换电源模块。")
        self.assertNotIn("未识别到板卡型号", warnings)

    def test_pii_masks_cases_and_document(self) -> None:
        raw = "【原因分析】张三工号10263031 电话13800138000 邮箱a.b@example.com 导致复位\n【处理方案】升级"
        doc = _document_from_text("张三10263031 复位", raw, source_name="x.csv", source_type="csv")
        self.assertTrue(doc.pii_masked)
        self.assertNotIn("13800138000", doc.masked_text)
        self.assertNotIn("a.b@example.com", doc.masked_text)
        self.assertNotIn("10263031", doc.cases[0].cause)
        self.assertNotIn("张三10263031", doc.title)
        self.assertIn("〔人员〕", doc.title)
        allowed, _ = mask_pii("正常文本")
        self.assertFalse(_)

    def test_index_same_term_in_title_and_later_field(self) -> None:
        case = IndexCase(id="K1", doc_id="D1", title="看门狗复位", title_clean="看门狗复位",
                         symptom="单板反复重启", cause="看门狗复位由逻辑版本异常导致",
                         solution="升级逻辑", body="现场日志显示其它内容")
        index = KBIndex([case])
        for term in ("看门狗复位", "复位"):
            term_id = index.terms[term]
            self.assertEqual(list(index.postings[term_id]), [0])
            self.assertEqual(len(index.tfs[term_id]), 5)
        self.assertGreater(index.bm25f(["看门狗复位"], {})[0], 0)
        self.assertEqual(index.phrase_hits("看门狗复位"), {0: 2.0})

    def test_empty_and_bad_queries_do_not_crash(self) -> None:
        index = KBIndex([])
        self.assertEqual(index.bm25f(["任意"], {}), {})
        self.assertEqual(index.fuzzy_hits("任意"), {})




class KBFormatTests(unittest.TestCase):
    """DOCX/XLSX/文本 PDF 与图片人工队列的纯标准库适配。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="bbu-kb-format-"))

    def _zip(self, name: str, files: dict[str, str]) -> Path:
        path = self.tmp / name
        with zipfile.ZipFile(path, "w") as archive:
            for entry, content in files.items():
                archive.writestr(entry, content)
        return path

    def test_docx_paragraphs_and_table(self) -> None:
        docx = self._zip("sample.docx", {
            "word/document.xml": """<?xml version="1.0"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>VBPE2 看门狗复位</w:t></w:r></w:p>
<w:p><w:r><w:t>【详细描述】单板反复复位</w:t></w:r></w:p>
<w:p><w:r><w:t>【原因分析】喂狗时序异常</w:t></w:r></w:p>
<w:p><w:r><w:t>【处理方案】升级 EPLD</w:t></w:r></w:p>
<w:tbl><w:tr><w:tc><w:p><w:r><w:t>板卡</w:t></w:r></w:p></w:tc>
<w:tc><w:p><w:r><w:t>VBPE2</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
</w:body></w:document>""",
        })
        docs = parse_file(docx, docx.name)
        self.assertEqual(len(docs), 1)
        self.assertIn("VBPE2", docs[0].title)
        self.assertEqual(docs[0].source_type, "docx")
        self.assertIn("升级 EPLD", docs[0].cases[0].solution)
        self.assertIn("板卡 | VBPE2", docs[0].raw_text)

    def test_xlsx_shared_strings_each_row(self) -> None:
        xlsx = self._zip("sample.xlsx", {
            "xl/workbook.xml": """<?xml version="1.0"?><workbook
xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>""",
            "xl/_rels/workbook.xml.rels": """<?xml version="1.0"?><Relationships
xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"
Target="worksheets/sheet1.xml"/></Relationships>""",
            "xl/sharedStrings.xml": """<?xml version="1.0"?><sst
xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<si><t>问题</t></si><si><t>答案</t></si>
<si><t>VBPD2 风扇转速异常</t></si>
<si><t>【原因分析】风扇堵转
【处理方案】清理风扇</t></si></sst>""",
            "xl/worksheets/sheet1.xml": """<?xml version="1.0"?><worksheet
xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>
<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>
<row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2" t="s"><v>3</v></c></row>
</sheetData></worksheet>""",
        })
        docs = parse_file(xlsx, xlsx.name)
        self.assertEqual(len(docs), 1)
        self.assertIn("风扇", docs[0].title)
        self.assertIn("清理风扇", docs[0].cases[0].solution)

    def test_pdf_text_and_scan_placeholder(self) -> None:
        pdf = b"""%PDF-1.4
1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj
2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj
3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R >> endobj
4 0 obj << /Length 70 >>
stream
BT /F1 12 Tf 72 720 Td (VBPE3 SFP receive power low) Tj ET
endstream
endobj
trailer << /Root 1 0 R >>
%%EOF"""
        path = self.tmp / "text.pdf"
        path.write_bytes(pdf)
        docs = parse_file(path, path.name)
        self.assertEqual(len(docs), 1)
        self.assertIn("SFP", docs[0].title)
        self.assertFalse(docs[0].force_review)
        blank = self.tmp / "scan.pdf"
        blank.write_bytes(b"%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\n%%EOF")
        placeholder = parse_file(blank, blank.name)[0]
        self.assertTrue(placeholder.force_review)
        self.assertIn("人工转写", placeholder.raw_text)

    def test_image_enters_manual_queue(self) -> None:
        png = (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR"
               + struct.pack(">II", 1, 1) + b"\x08\x02\x00\x00\x00"
               + b"\x00" * 4 + b"IEND\xaeB`\x82")
        path = self.tmp / "site.png"
        path.write_bytes(png)
        doc = parse_file(path, path.name)[0]
        self.assertTrue(doc.force_review)
        self.assertEqual(doc.source_type, "image")
        self.assertIn("人工", "".join(doc.warnings))


class _FakeGateway:
    def __init__(self, data: dict | None = None, *, fail: bool = False) -> None:
        self.data = data
        self.fail = fail
        self.calls = 0

    def has_real_model(self, role: str) -> bool:
        return True

    def complete(self, *args, **kwargs) -> AIResult:
        self.calls += 1
        if self.fail:
            raise NoModelError("模型临时不可用")
        return AIResult(text=json.dumps(self.data or {}, ensure_ascii=False),
                        data=self.data, model_name="fake-model")


class KBServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="bbu-kb-test-"))
        self.config = Config(self.tmp)
        self.config.values["examples_dir"] = str(ROOT / "examples")
        self.db = Database(self.tmp / "data.db")
        from bbu.security import bootstrap_admin, hash_password
        from bbu.util import iso_now
        bootstrap_admin(self.db, self.tmp, "初始管理员密码.txt")
        now = iso_now()
        self.db.execute(
            "INSERT INTO users(username, display_name, role, password_hash, must_change, "
            "active, created_at, status, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            ("engineer", "工程师", "engineer", hash_password("Engineer-2026"), 0, 1,
             now, "active", now))
        self.admin = {"id": 1, "role": "admin", "username": "admin"}
        self.engineer = {"id": 2, "role": "engineer", "username": "engineer"}

    def tearDown(self) -> None:
        try:
            self.db.close()
        except Exception:
            pass

    def service(self, gateway=None, traces=None) -> KBService:
        return KBService(self.db, self.config, gateway=gateway, traces=traces,
                         start_worker=False)

    def test_image_upload_stays_in_review_even_for_admin_direct(self) -> None:
        png = (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR"
               + struct.pack(">II", 1, 1) + b"\x08\x02\x00\x00\x00"
               + b"\x00" * 4 + b"IEND\xaeB`\x82")
        path = self.tmp / "现场.png"
        path.write_bytes(png)
        upload = SimpleNamespace(filename=path.name, size=len(png), path=str(path))
        svc = self.service()
        try:
            job = svc.create_import_job(self.admin, [upload], mode="direct")
            svc._process_job(job["id"])
            self.assertEqual(svc.status(self.admin)["documents"], 0)
            self.assertGreaterEqual(svc.status(self.admin)["drafts"], 1)
            drafts = svc.list_drafts(self.admin)["items"]
            self.assertTrue(drafts)
            self.assertIn("人工", "".join(drafts[0]["warnings"]))
            svc.approve_draft(drafts[0]["id"], self.admin)
            self.assertEqual(svc.status(self.admin)["documents"], 1)
        finally:
            svc.shutdown()

    def test_schema_and_sample_import(self) -> None:
        self.assertEqual(SCHEMA_VERSION, 28)
        for table in ("kb_documents", "kb_cases", "kb_import_jobs", "kb_drafts",
                      "kb_doc_versions", "kb_query_logs", "kb_feedback", "kb_index_meta",
                      "kb_chat_sessions", "kb_chat_messages"):
            row = self.db.one("SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                              (table,))
            self.assertIsNotNone(row, table)
        svc = self.service()
        try:
            result = svc.import_sample(self.admin)
            self.assertEqual(result["documents"], 20)
            self.assertEqual(svc.status(self.admin)["documents"], 20)
            svc.rebuild_index()
            self.assertEqual(svc.status(self.admin)["cases"], 21)
            # 重复载入按 source_sha256 跳过。
            again = svc.import_sample(self.admin)
            self.assertEqual(again["skipped"], 20)
        finally:
            svc.shutdown()

    def test_search_exact_filters_and_no_answer(self) -> None:
        svc = self.service()
        try:
            svc.import_sample(self.admin)
            svc.rebuild_index()
            result = svc.search("看门狗复位", self.engineer)
            self.assertFalse(result["no_answer"])
            self.assertIn("看门狗", result["items"][0]["title"])
            scores = [item["score"] for item in result["items"]]
            self.assertEqual(scores, sorted(scores, reverse=True),
                             "通道补位不能用低分项污染首条排序")
            filtered = svc.search("复位", self.engineer, filters={"board": "VBPE2"})
            self.assertTrue(filtered["items"])
            for item in filtered["items"]:
                self.assertIn("VBPE2", item["boards"])
            empty = svc.search("如何办理护照", self.engineer)
            self.assertTrue(empty["no_answer"])
            self.assertEqual(empty["items"], [])
        finally:
            svc.shutdown()

    def test_kb_ids_have_wide_entropy(self) -> None:
        """22xx 条真实 CSV 导入曾因 6 位十六进制 ID 生日碰撞失败。"""
        from bbu.kb_service import _kb_id
        ids = {_kb_id("K") for _ in range(3000)}
        self.assertEqual(len(ids), 3000)
        self.assertGreaterEqual(len(next(iter(ids))), 17)

    def test_operations_record_queries_and_case_changes(self) -> None:
        svc = self.service()
        try:
            svc.import_sample(self.admin)
            svc.search("看门狗复位", self.engineer)
            doc = svc.list_documents(size=1)["items"][0]
            case = svc.get_document(doc["id"], reveal=True)["cases"][0]
            svc.update_case(case["id"], {"solution": "补充现场验证步骤", "reason": "任务记录测试"},
                            self.admin)
            admin_ops = svc.list_operations(self.admin, limit=100)["items"]
            self.assertTrue(any(x["kind"] == "query" for x in admin_ops))
            self.assertTrue(any(x["kind"] == "revision" for x in admin_ops))
            engineer_ops = svc.list_operations(self.engineer, limit=100)["items"]
            self.assertTrue(engineer_ops)
            self.assertTrue(all(x["kind"] == "query" for x in engineer_ops))
        finally:
            svc.shutdown()

    def test_answer_fallback_and_citation_sanitize(self) -> None:
        svc = self.service()
        try:
            svc.import_sample(self.admin)
            svc.rebuild_index()
            fallback = svc.answer("看门狗复位", self.engineer)
            self.assertTrue(fallback["degraded"])
            self.assertFalse(fallback["no_answer"])
            self.assertTrue(fallback["cases"])
            allowed = {f"K{i}" for i in range(1, len(fallback["cases"]) + 1)}
            for conclusion in fallback["conclusions"]:
                self.assertTrue(set(conclusion["citations"]) <= allowed)
            no_answer = svc.answer("如何办理护照", self.engineer)
            self.assertTrue(no_answer["no_answer"])
            self.assertEqual(no_answer["cases"], [])
        finally:
            svc.shutdown()

    def test_model_failure_degrades_and_bad_citations_fall_back(self) -> None:
        failing = _FakeGateway(fail=True)
        svc = self.service(failing)
        try:
            svc.import_sample(self.admin)
            svc.rebuild_index()
            result = svc.answer("看门狗复位", self.engineer)
            self.assertTrue(result["degraded"])
            self.assertFalse(result["no_answer"])
        finally:
            svc.shutdown()

        fake = _FakeGateway({
            "answer": "模型回答",
            "conclusions": [{"text": "可能根因", "citations": ["K99"]}],
            "steps": ["升级前备份"], "cautions": ["需现场复核"], "confidence": 0.8,
            "no_answer": False,
        })
        svc2 = self.service(fake)
        try:
            svc2.import_sample(self.admin)
            svc2.rebuild_index()
            result = svc2.answer("看门狗复位", self.engineer)
            self.assertFalse(result["degraded"])
            self.assertEqual(result["conclusions"][0]["citations"], ["K1"])
            self.assertEqual(fake.calls, 1)
        finally:
            svc2.shutdown()

    def test_document_permissions_versions_and_restore(self) -> None:
        svc = self.service()
        try:
            svc.import_sample(self.admin)
            svc.rebuild_index()
            doc = svc.list_documents(size=1)["items"][0]
            ordinary = svc.get_document(doc["id"], reveal=False)
            self.assertNotIn("raw_text", ordinary)
            self.assertTrue(ordinary["text"])
            admin = svc.get_document(doc["id"], reveal=True)
            self.assertIn("raw_text", admin)
            version = svc.list_versions(doc["id"], self.admin)["items"][0]
            updated = svc.update_document(doc["id"], {"title": "改过的标题"}, self.admin)
            self.assertEqual(updated["title"], "改过的标题")
            self.assertGreaterEqual(len(svc.list_versions(doc["id"], self.admin)["items"]), 2)
            restored = svc.restore_version(doc["id"], version["id"], self.admin)
            self.assertEqual(restored["title"], doc["title"])
            svc.set_document_status(doc["id"], "disabled", self.admin)
            self.assertTrue(svc.search("看门狗复位", self.engineer)["no_answer"] or
                            all(x["doc_id"] != doc["id"]
                                for x in svc.search("看门狗复位", self.engineer)["items"]))
        finally:
            svc.shutdown()

    def test_import_job_direct_and_review(self) -> None:
        csv_path = self.tmp / "one.csv"
        import csv
        import io
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["问题", "答案"])
        writer.writerow(["VBPE2 复位", "【原因分析】逻辑异常\n【处理方案】升级逻辑"])
        csv_path.write_text(buf.getvalue(), encoding="utf-8")

        class Upload:
            filename = "one.csv"
            size = csv_path.stat().st_size
            path = str(csv_path)

        svc = self.service()
        try:
            job = svc.create_import_job(self.admin, [Upload()], mode="direct")
            svc._process_job(job["id"])
            got = svc.get_job(job["id"])
            self.assertEqual(got["status"], "completed")
            self.assertGreaterEqual(svc.status(self.admin)["documents"], 1)
        finally:
            svc.shutdown()

        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["问题", "答案"])
        writer.writerow(["VBPE2 复位（工程师）", "【原因分析】逻辑异常\n【处理方案】升级逻辑"])
        csv_path.write_text(buf.getvalue(), encoding="utf-8")
        Upload.size = csv_path.stat().st_size

        svc2 = self.service()
        try:
            job = svc2.create_import_job(self.engineer, [Upload()], mode="direct")
            svc2._process_job(job["id"])
            got = svc2.get_job(job["id"])
            self.assertEqual(got["status"], "completed")
            self.assertEqual(got["stats"]["drafts"], 1)
            drafts = svc2.list_drafts(self.admin)
            self.assertEqual(len(drafts["items"]), 1)
            draft_id = drafts["items"][0]["id"]
            before = svc2.status(self.admin)["documents"]
            approved = svc2.approve_draft(draft_id, self.admin)
            self.assertTrue(approved["ok"])
            self.assertEqual(svc2.status(self.admin)["documents"], before + 1)
        finally:
            svc2.shutdown()

    def test_import_dedup_and_batch_rollback(self) -> None:
        svc = self.service()
        try:
            import csv
            csv_path = self.tmp / "dup.csv"
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(["问题", "答案"])
            writer.writerow(["去重测试案例", "【原因分析】去重测试根因\n【处理方案】去重测试方案"])
            csv_path.write_text(buf.getvalue(), encoding="utf-8")
            upload = SimpleNamespace(filename=csv_path.name, size=csv_path.stat().st_size,
                                     path=str(csv_path))
            job1 = svc.create_import_job(self.admin, [upload], mode="direct", kb_id=1)
            svc._process_job(job1["id"])
            self.assertEqual(svc.get_job(job1["id"])["stats"]["documents"], 1)
            doc_id = svc.list_documents(status="active")["items"][0]["id"]
            case_id = svc.get_document(doc_id, reveal=True)["cases"][0]["id"]
            job2 = svc.create_import_job(self.admin, [upload], mode="direct", kb_id=1)
            svc._process_job(job2["id"])
            got = svc.get_job(job2["id"])
            self.assertEqual(got["status"], "completed")
            self.assertEqual(got["stats"]["documents"], 0)
            self.assertGreaterEqual(got["stats"]["skipped_files"], 1)
            # 回退必须停用本批文档而不是物理删除；修订链仍可读。
            rollback = svc.rollback_jobs([job1["id"]], self.admin)
            self.assertTrue(rollback["items"][0]["ok"])
            self.assertEqual(svc.get_document(doc_id, reveal=True)["status"], "disabled")
            self.assertGreaterEqual(svc.list_case_revisions(case_id, self.admin)["total"], 1)
        finally:
            svc.shutdown()

    def test_library_isolation_cross_search_and_backup(self) -> None:
        from bbu.kb_ingest import parse_paste
        svc = self.service()
        try:
            lib = svc.create_library(self.admin, name="专家经验库", kind="expert")
            expert_id = int(lib["library"]["id"])
            doc = parse_paste("【原因分析】跨库检索专用根因\n【处理方案】跨库专用方案",
                              title="跨库专用案例")[0]
            payload = {"document": doc.public(), "cases": [c.public() for c in doc.cases]}
            svc.save_document(payload, self.admin, kb_id=expert_id)
            svc.rebuild_index()
            single = svc.search("跨库检索专用", self.admin, kb_id=1, log=False)
            self.assertFalse(any(int(x.get("kb_id") or 1) == expert_id
                                 for x in single.get("items") or []))
            cross = svc.search("跨库检索专用", self.admin, kb_ids=[1, expert_id], log=False)
            self.assertTrue(any(int(x.get("kb_id") or 0) == expert_id
                                for x in cross.get("items") or []))
            # 归档后仍可备份；管理员可以把归档库恢复为 active。
            svc.archive_library(expert_id, self.admin)
            self.assertEqual(svc.get_library(expert_id, include_archived=True)["status"], "archived")
            self.assertEqual(len(svc.list_libraries()["items"]), 1)
            self.assertEqual(len(svc.list_libraries(include_archived=True)["items"]), 2)
            filename, data = svc.backup_library(expert_id, self.admin)
            self.assertTrue(filename.endswith(".kbbackup.zip"))
            svc.update_library(expert_id, {"status": "active"}, self.admin)
            self.assertEqual(svc.get_library(expert_id)["status"], "active")
            restored = svc.restore_library(self.admin, data, name="恢复专家库")
            self.assertGreaterEqual(restored["documents"], 1)
            self.assertEqual(svc.status(self.admin, kb_id=restored["kb_id"])["documents"], 1)
            self.assertEqual(svc.status(self.admin, kb_id=expert_id)["documents"], 1)
            self.assertEqual(svc.stats(self.admin, kb_id=1)["inventory"]["documents"], 0)
            self.assertEqual(svc.stats(self.admin, kb_id=expert_id)["inventory"]["documents"], 1)
            expert_doc = svc.list_documents(kb_id=expert_id, status="active")["items"][0]
            case = svc.get_document(expert_doc["id"], reveal=True)["cases"][0]
            svc.create_review(self.admin, target_type="case", target_id=case["id"],
                              stage="manual", run=False)
            scoped = svc.list_reviews(self.admin, kb_id=expert_id)
            self.assertTrue(scoped["items"])
            self.assertTrue(all(int(x["kb_id"]) == expert_id for x in scoped["items"]))
            self.assertFalse(svc.list_reviews(self.admin, kb_id=1)["items"])
            with self.assertRaises(Exception):
                svc.archive_library(1, self.admin)
        finally:
            svc.shutdown()

    def test_revision_ledger_restore_and_append_only(self) -> None:
        svc = self.service()
        try:
            svc.import_sample(self.admin)
            svc.rebuild_index()
            doc = svc.list_documents(size=1)["items"][0]
            case = svc.get_document(doc["id"], reveal=True)["cases"][0]
            old_solution = case["solution"]
            self.assertEqual(svc.list_case_revisions(case["id"], self.admin)["total"], 1)
            svc.update_case(case["id"], {"solution": "补丁后的处理方案", "reason": "修正方案"},
                            self.admin)
            revisions = svc.list_case_revisions(case["id"], self.admin)
            self.assertEqual(revisions["total"], 2)
            self.assertEqual(revisions["items"][0]["diff"][0]["field"], "solution")
            self.assertTrue(svc.verify_case_revisions(case["id"], self.admin)["ok"])
            svc.restore_case_revision(case["id"], "1", self.admin, reason="回滚测试")
            self.assertEqual(svc.get_case(case["id"])["case"]["solution"], old_solution)
            self.assertTrue(svc.verify_case_revisions(case["id"], self.admin)["ok"])
            # 台账触发器不可更新/删除
            row = svc.list_case_revisions(case["id"], self.admin)["items"][0]
            with self.assertRaises(Exception):
                self.db.execute("UPDATE kb_case_revisions SET reason='x' WHERE id=?", (row["id"],))
        finally:
            svc.shutdown()

    def test_case_revision_permissions_and_no_empty_revision(self) -> None:
        svc = self.service()
        try:
            svc.import_sample(self.admin)
            doc = svc.list_documents(size=1)["items"][0]
            case = svc.get_document(doc["id"], reveal=True)["cases"][0]
            with self.assertRaises(Exception) as ctx:
                svc.list_case_revisions(case["id"], self.engineer)
            self.assertEqual(getattr(ctx.exception, "status", 0), 403)
            with self.assertRaises(Exception) as ctx:
                svc.update_case(case["id"], {"solution": "x"}, self.admin)
            self.assertEqual(getattr(ctx.exception, "status", 0), 400)
            before = case["revision"]
            svc.update_document(doc["id"], {"title": "只改标题"}, self.admin)
            after = svc.get_case(case["id"])["case"]
            self.assertEqual(after["revision"], before)
        finally:
            svc.shutdown()

    def test_stats_groups_usage_and_csv(self) -> None:
        svc = self.service()
        try:
            svc.import_sample(self.admin)
            svc.rebuild_index()
            svc.search("看门狗复位", self.engineer)
            data = svc.stats(self.admin, days=30)
            for key in ("inventory", "coverage", "usage", "review", "changes",
                        "imports", "index", "knowledge", "trend"):
                self.assertIn(key, data)
            self.assertEqual(data["inventory"]["active_documents"], 20)
            self.assertGreaterEqual(data["usage"]["queries"], 1)
            self.assertTrue(any(x["query"] == "看门狗复位" for x in data["usage"]["top_queries"]))
            self.assertGreaterEqual(data["changes"]["revisions"], 21)
            self.assertTrue(svc.stats_csv(self.admin, days=7).startswith("group,metric"))
        finally:
            svc.shutdown()

    def test_review_fallback_and_draft_reject(self) -> None:
        svc = self.service()
        try:
            created = svc.create_paste(self.engineer, title="待审 SSD",
                                       text="【详细描述:SSD 只读】\n【原因分析】写保护\n【处理方案】更换 SSD")
            draft_id = created["draft_id"]
            review = self.db.one("SELECT * FROM kb_reviews WHERE target_id=? ORDER BY created_at DESC",
                                 (draft_id,))
            self.assertIsNotNone(review)
            result = svc.run_review(review["id"], self.admin)
            self.assertEqual(result["status"], "manual")
            stats = svc.stats(self.admin, days=30)
            self.assertGreaterEqual(stats["review"]["pending"], 1)
            self.assertEqual(stats["review"]["ai_coverage"], 0)
            self.assertGreaterEqual(stats["review"]["fallback_manual"], 1)
            decided = svc.decide_review(review["id"], self.admin, decision="reject", note="资料不足")
            self.assertLessEqual(svc.stats(self.admin, days=30)["review"]["pending"], 0)
            self.assertEqual(decided["human_decision"], "reject")
            draft = svc.get_draft(draft_id, self.admin)
            self.assertEqual(draft["status"], "rejected")
        finally:
            svc.shutdown()

    def test_review_model_and_patch(self) -> None:
        fake = _FakeGateway({
            "decision": "pass", "score": 90,
            "dimensions": [
                {"key": "answer_match", "score": 25, "max": 25},
                {"key": "cause_complete", "score": 18, "max": 20},
                {"key": "solution_actionable", "score": 18, "max": 20},
                {"key": "entity_consistency", "score": 14, "max": 15},
                {"key": "source_fidelity", "score": 9, "max": 10},
                {"key": "safety_privacy", "score": 10, "max": 10},
            ],
            "issues": [], "suggested_patch": {"prevention": "补充预防措施"},
            "confidence": 0.9,
        })
        svc = self.service(fake)
        try:
            svc.import_sample(self.admin)
            doc = svc.list_documents(size=1)["items"][0]
            case = svc.get_document(doc["id"], reveal=True)["cases"][0]
            review = svc.create_review(self.admin, target_type="case", target_id=case["id"],
                                       stage="manual", run=True)
            self.assertEqual(review["decision"], "pass")
            self.assertIn("prevention", review["suggested_patch"])
            svc.decide_review(review["id"], self.admin, decision="adopt_patch", note="采纳")
            self.assertEqual(svc.get_case(case["id"])["case"]["prevention"], "补充预防措施")
        finally:
            svc.shutdown()

    def test_kb_traces_record_import_and_search(self) -> None:
        from bbu.trace import TraceStore
        store = TraceStore(self.db, self.config)
        svc = self.service(traces=store)
        try:
            csv_path = self.tmp / "trace.csv"
            csv_path.write_text('问题,答案\n"轨迹测试","【原因分析】轨迹专用原因\n【处理方案】轨迹专用方案"\n',
                                encoding="utf-8")
            upload = SimpleNamespace(filename=csv_path.name, size=csv_path.stat().st_size,
                                     path=str(csv_path))
            job = svc.create_import_job(self.admin, [upload], mode="direct", kb_id=1)
            svc._process_job(job["id"])
            runs = store.list(kind="kb", ref_id=job["id"])
            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0]["status"], "success")
            events = store.events(runs[0]["id"])["items"]
            self.assertTrue(any(e.get("type") == "import_result" for e in events))
            result = svc.search("轨迹专用", self.admin, kb_id=1, log=True)
            search_runs = store.list(kind="kb", ref_id=result["query_id"])
            self.assertEqual(len(search_runs), 1)
            search_events = store.events(search_runs[0]["id"])["items"]
            retrieval = next(e for e in search_events if e.get("type") == "retrieval")
            self.assertEqual(int(retrieval.get("latency_ms") or 0) >= 0, True)
            self.assertIn("hits", retrieval)
        finally:
            svc.shutdown()

    def test_review_role_fallback_and_numeric_override(self) -> None:
        review_data = {
            "decision": "pass", "score": 90,
            "dimensions": [
                {"key": "answer_match", "score": 25, "max": 25},
                {"key": "cause_complete", "score": 18, "max": 20},
                {"key": "solution_actionable", "score": 18, "max": 20},
                {"key": "entity_consistency", "score": 14, "max": 15},
                {"key": "source_fidelity", "score": 9, "max": 10},
                {"key": "safety_privacy", "score": 10, "max": 10},
            ],
            "issues": [], "suggested_patch": {}, "confidence": 0.9,
        }

        class _FallbackGateway:
            def __init__(self) -> None:
                self.roles: list[str] = []

            def has_real_model(self, role: str) -> bool:
                return role == "default"

            def choose(self, role: str, override_id: int | None = None):
                return [{"provider": "openai"}]

            def complete(self, role: str, *args, **kwargs) -> AIResult:
                self.roles.append(role)
                return AIResult(text=json.dumps(review_data), data=review_data,
                                model_name="fallback-model")

        fake = _FallbackGateway()
        self.config.values["kb_review_role"] = "kb_reviewer"
        svc = self.service(fake)
        try:
            self.assertEqual(svc.reviews.roles(), ["kb_reviewer", "assistant", "default"])
            created = svc.create_paste(self.engineer, title="回退审核案例",
                                       text="【原因分析】回退原因\n【处理方案】回退方案")
            review = self.db.one(
                "SELECT * FROM kb_reviews WHERE target_id=? ORDER BY created_at DESC",
                (created["draft_id"],))
            result = svc.run_review(review["id"], self.admin)
            self.assertEqual(result["decision"], "pass")
            self.assertEqual(fake.roles, ["default"])
            self.config.values["kb_review_model"] = "9"
            self.assertEqual(svc.reviews.model_override(), 9)
            self.config.values.pop("kb_review_model", None)
        finally:
            svc.shutdown()



if __name__ == "__main__":
    unittest.main()
