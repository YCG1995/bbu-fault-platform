"""1.33.0 第二波回归：评测、术语、近重复、导入映射、报告与自动发布。"""
from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor"))

from bbu.config import Config  # noqa: E402
from bbu.db import Database, SCHEMA_VERSION  # noqa: E402
from bbu.kb_duplicates import KBDuplicates  # noqa: E402
from bbu.kb_eval import KBEvaluator  # noqa: E402
from bbu.kb_ingest import (  # noqa: E402
    _document_from_text, guessed_mapping, parse_csv_bytes, preview_import_bytes,
)
from bbu.kb_report import KBReport  # noqa: E402
from bbu.kb_service import KBService, KBError  # noqa: E402
from bbu.security import bootstrap_admin, hash_password  # noqa: E402
from bbu.util import iso_now, new_id  # noqa: E402


class SecondWaveBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="bbu-kb-133-"))
        self.config = Config(self.tmp)
        self.config.values["examples_dir"] = str(ROOT / "examples")
        self.db = Database(self.tmp / "data.db")
        bootstrap_admin(self.db, self.tmp, "初始管理员密码.txt")
        now = iso_now()
        self.db.execute(
            "INSERT INTO users(username, display_name, role, password_hash, must_change, "
            "active, created_at, status, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            ("engineer", "工程师", "engineer", hash_password("Engineer-2026"), 0, 1,
             now, "active", now))
        self.admin = {"id": 1, "role": "admin", "username": "admin"}
        self.svc = KBService(self.db, self.config, start_worker=False)

    def tearDown(self) -> None:
        try:
            self.svc.shutdown()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.db.close()
        except Exception:  # noqa: BLE001
            pass

    def add_doc(self, title: str, text: str) -> str:
        return self.svc.create_paste(self.admin, title=title, text=text)["doc_id"]

    def add_draft_review(self, *, score: float = 95, confidence: float = 0.95,
                         decision: str = "pass", prompt_version: str = "review-v1",
                         warnings: list[str] | None = None) -> tuple[str, str]:
        doc = _document_from_text(
            "VBP 自动发布草稿",
            "【故障现象】单板反复复位\n【原因分析】喂狗异常\n【处理方案】升级逻辑",
            source_name="auto.csv", source_type="csv")
        payload = {"document": doc.public(), "cases": [c.public() for c in doc.cases]}
        draft_id = self.svc._create_draft(
            payload, int(self.admin["id"]), job_id="", title=doc.title,
            confidence=confidence, warnings=warnings or [])
        review_id = new_id("RV", with_time=False, entropy=8)
        now = iso_now()
        self.db.execute(
            "INSERT INTO kb_reviews(id,kb_id,target_type,target_id,stage,prompt_version,"
            "decision,score,confidence,status,created_at,finished_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (review_id, 1, "draft", draft_id, "import", prompt_version, decision,
             score, confidence, "done", now, now))
        return draft_id, review_id


class SchemaAndEvalTests(SecondWaveBase):
    def test_schema_27_columns_indexes_and_unique_term(self) -> None:
        self.assertEqual(SCHEMA_VERSION, 27)
        doc_cols = {r["name"] for r in self.db.all("PRAGMA table_info(kb_documents)")}
        self.assertTrue({"merged_into", "merged_at", "merge_token"} <= doc_cols)
        eval_cols = {r["name"] for r in self.db.all("PRAGMA table_info(kb_eval_queries)")}
        self.assertTrue({"category", "split"} <= eval_cols)
        names = {r["name"] for r in self.db.all("PRAGMA index_list(kb_terms)")}
        self.assertIn("idx_kb_terms_unique", names)
        self.svc.save_term(self.admin, {"term": "唯一词", "aliases": ["唯一别名"]})
        with self.assertRaises(Exception):
            self.db.execute(
                "INSERT INTO kb_terms(kb_id,kind,term,aliases_json,weight,enabled,created_at) "
                "VALUES(1,'synonym','唯一词','[]',1,1,?)", (iso_now(),))

    def test_eval_300_layered_metrics_are_deterministic_and_comparable(self) -> None:
        self.svc.import_sample(self.admin)
        self.svc.ensure_index(1)
        cases = self.db.all("SELECT id,title FROM kb_cases WHERE status='active' ORDER BY id")
        self.assertGreaterEqual(len(cases), 10)
        rows = []
        for category in ("实体精确", "症状描述", "口语改写"):
            for i in range(75):
                case = cases[i % len(cases)]
                query = case["title"] if category == "实体精确" else (
                    case["title"] + " 怎么处理" if category == "症状描述"
                    else "设备" + case["title"] + "咋回事")
                rows.append({"query": query, "qrels": [case["id"]],
                             "category": category, "split": "test"})
        for i in range(75):
            rows.append({"query": f"红烧肉{i}怎么做", "qrels": [],
                         "category": "库外无答案", "split": "test"})
        data = ("\n".join(json.dumps(x, ensure_ascii=False) for x in rows)).encode()
        ev = KBEvaluator(self.svc)
        set_row = ev.import_set(self.admin, name="300条金标准", data=data,
                                filename="golden.jsonl")
        self.assertEqual(set_row["query_count"], 300)
        run1 = ev.run_set(set_row["id"], self.admin, kb_id=1)
        run2 = ev.run_set(set_row["id"], self.admin, kb_id=1)
        summary = run1["summary"]
        self.assertEqual(summary["queries"], 300)
        self.assertEqual(summary["graded_queries"], 225)
        self.assertEqual(summary["no_answer_queries"], 75)
        self.assertGreaterEqual(summary["recall@5"], 0.95)
        self.assertGreaterEqual(summary["no_answer_accuracy"], 0.98)
        self.assertTrue(summary["recall@5_ci95"]["lower"] <= summary["recall@5"]
                        <= summary["recall@5_ci95"]["upper"])
        for key in ("queries", "graded_queries", "no_answer_queries", "recall@1",
                    "recall@5", "recall@10", "mrr@10", "ndcg@10",
                    "no_answer_accuracy", "answered_rate", "recall@5_ci95"):
            self.assertEqual(summary[key], run2["summary"][key], key)
        # 运行后列表必须能读回最近一次 summary（覆盖 SQL 绑定参数回归）。
        listed = ev.list_sets(self.admin)
        listed_set = next(x for x in listed["items"] if x["id"] == set_row["id"])
        self.assertEqual(listed_set["last_run"]["id"], run2["id"])
        self.assertEqual(listed_set["last_summary"], run2["summary"])
        self.assertEqual(len(run1["metrics"]["by_category"]), 4)
        self.assertEqual(len(run1["metrics"]["by_split"]), 1)
        self.assertTrue(run1["metrics"]["qrels_sha256"])
        self.assertTrue(run1["metrics"]["index_checksum"])
        # 索引变化后，新 run 必须标记为不可比。
        self.add_doc("VBP 新文档", "【故障现象】新增案例\n【原因分析】测试\n【处理方案】观察")
        self.svc.ensure_index(1)
        run3 = ev.run_set(set_row["id"], self.admin, kb_id=1)
        self.assertFalse(run3["metrics"]["comparable"])
        self.assertTrue(any("不可" in x for x in run3["metrics"]["warnings"]))

    def test_eval_empty_index_and_empty_qrels_do_not_divide_by_zero(self) -> None:
        ev = KBEvaluator(self.svc)
        data = json.dumps([{"query": "红烧肉怎么做", "qrels": [],
                            "category": "库外无答案"}]).encode()
        set_row = ev.import_set(self.admin, name="空库", data=data, filename="x.json")
        run = ev.run_set(set_row["id"], self.admin, kb_id=1)
        self.assertEqual(run["summary"]["graded_queries"], 0)
        self.assertEqual(run["summary"]["recall@5"], 0.0)
        self.assertEqual(run["summary"]["no_answer_accuracy"], 1.0)
        self.assertEqual(run["summary"]["recall@5_ci95"], {"lower": 0.0, "upper": 0.0})


class TermTests(SecondWaveBase):
    def test_terms_are_bidirectional_cycle_safe_and_used_by_search(self) -> None:
        saved = self.svc.save_term(self.admin, {
            "term": "复位", "aliases": ["反复重启"], "weight": 2})
        term_id = saved["item"]["id"]
        # 别名侧提问也能扩展到词条，词条侧提问也能扩展到别名。
        for query in ("复位", "反复重启"):
            analysis = self.svc.search(query, self.admin, log=False)["analysis"]
            self.assertIn("反复重启" if query == "复位" else "复位",
                          analysis["expansions"])
        # 局部更新只改权重，不应把 term 清空。
        updated = self.svc.save_term(self.admin, {"id": term_id, "weight": 3})
        self.assertEqual(updated["item"]["term"], "复位")
        self.assertEqual(updated["item"]["weight"], 3)
        with self.assertRaises(KBError):
            self.svc.save_term(self.admin, {"term": "复位", "aliases": ["反复重启"]})
        with self.assertRaises(KBError):
            self.svc.save_term(self.admin, {"term": "空别名", "aliases": [""]})
        # A→B→C→A 必须拒绝，且不能因菱形汇聚误报。
        self.svc.save_term(self.admin, {"term": "A", "aliases": ["B"]})
        self.svc.save_term(self.admin, {"term": "B", "aliases": ["C"]})
        with self.assertRaises(KBError):
            self.svc.save_term(self.admin, {"term": "C", "aliases": ["A"]})
        self.svc.save_term(self.admin, {"term": "菱形A", "aliases": ["菱形B", "菱形C"]})
        self.svc.save_term(self.admin, {"term": "菱形B", "aliases": ["菱形D"]})
        self.svc.save_term(self.admin, {"term": "菱形C", "aliases": ["菱形D"]})
        self.svc.delete_term(term_id, self.admin)
        self.assertNotIn("复位", self.svc._library_synonyms(1))


class DuplicateTests(SecondWaveBase):
    def test_near_duplicate_pairs_are_found_negatives_excluded_and_merge_is_traceable(self) -> None:
        positive = []
        for i in range(5):
            title = f"VBP 近重复 {i} 单板复位"
            text = (f"【故障现象】VBP 近重复 {i} 单板业务中断反复复位\n"
                    f"【原因分析】第 {i} 组喂狗时序异常\n【处理方案】升级第 {i} 组逻辑版本")
            positive.append((self.add_doc(title, text), self.add_doc(title, text)))
        negative = []
        for i in range(5):
            title = f"VBP 负样本 {i} 单板异常"
            text = (f"【故障现象】VBP 负样本 {i} 单板业务中断\n"
                    f"【原因分析】第 {i} 组不同根因：电源/时钟/光模块/风扇/链路\n"
                    f"【处理方案】按第 {i} 组独立处理")
            negative.append(self.add_doc(title, text))
        dups = KBDuplicates(self.svc).candidates(self.admin, threshold=0.82, limit=200)
        found = {frozenset((x["source"]["doc_id"], x["target"]["doc_id"]))
                 for x in dups["items"]}
        for left, right in positive:
            self.assertIn(frozenset((left, right)), found)
        for doc_id in negative:
            self.assertFalse(any(doc_id in pair for pair in found), doc_id)
        left, right = positive[0]
        result = KBDuplicates(self.svc).merge(
            left, right, self.admin, merge_token="test-merge", reason="unit")
        self.assertGreaterEqual(result["moved_cases"], 1)
        source = self.db.one("SELECT status,merged_into,merge_token FROM kb_documents WHERE id=?",
                             (left,))
        self.assertEqual(source["status"], "merged")
        self.assertEqual(source["merged_into"], right)
        merged_cases = self.db.all("SELECT id FROM kb_cases WHERE doc_id=? AND status='active'",
                                   (left,))
        self.assertEqual(merged_cases, [])
        revisions = self.db.all(
            "SELECT action,revision FROM kb_case_revisions WHERE action='merge'")
        self.assertTrue(revisions)
        noop = KBDuplicates(self.svc).merge(
            left, right, self.admin, merge_token="test-merge", reason="unit")
        self.assertTrue(noop["noop"])
        self.assertEqual(noop["moved_cases"], 0)


class ImportMappingTests(SecondWaveBase):
    def test_preview_and_manual_mapping_keep_case_fields(self) -> None:
        csv = ("\ufeff问题,现象,原因,方案,空列\n"
               "VBPE2 复位,单板反复复位,喂狗时序异常,升级逻辑,\n").encode("utf-8")
        preview = preview_import_bytes(csv, filename="mapped.csv")
        self.assertEqual(preview["headers"], ["问题", "现象", "原因", "方案", "空列"])
        self.assertEqual(preview["encoding"], "utf-8-sig")
        self.assertEqual(preview["guessed_mapping"]["方案"], "solution")
        manual = parse_csv_bytes(csv, source_name="mapped.csv", mapping={
            "问题": "title", "现象": "symptom", "原因": "cause", "方案": "solution"})
        auto = parse_csv_bytes(csv, source_name="mapped.csv")
        self.assertEqual(len(manual), 1)
        self.assertEqual(len(auto), 1)
        case = manual[0].cases[0]
        self.assertIn("喂狗时序异常", case.cause)
        self.assertIn("升级逻辑", case.solution)
        with self.assertRaises(ValueError):
            parse_csv_bytes(csv, source_name="bad.csv", mapping={"不存在": "title"})
        with self.assertRaises(ValueError):
            parse_csv_bytes(csv, source_name="bad.csv",
                            mapping={"问题": "title", "现象": "title"})
        self.assertEqual(guessed_mapping(["方案"]), {"方案": "solution"})


class ReportTests(SecondWaveBase):
    def test_report_whitelist_escaping_formula_guard_and_docx(self) -> None:
        self.add_doc("<script>alert(1)</script>",
                     "【故障现象】XSS 标题\n【原因分析】=SUM(1,1)\n【处理方案】'公式")
        docs = self.svc.list_documents(kb_id=1, size=5)["items"]
        report = KBReport(self.svc)
        payload, filename, ctype = report.generate(
            self.admin, kb_id=1, doc_ids=[d["id"] for d in docs],
            formats=["html", "md", "csv", "docx"], title="<报告>")
        self.assertEqual(ctype, "application/zip")
        self.assertTrue(filename.endswith(".zip"))
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            names = set(archive.namelist())
            self.assertEqual(names, {"report.html", "report.md", "data.csv",
                                     "report.docx", "manifest.json"})
            html_text = archive.read("report.html").decode("utf-8")
            self.assertNotIn("<script>alert(1)</script>", html_text)
            self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html_text)
            csv_text = archive.read("data.csv").decode("utf-8-sig")
            self.assertIn("'=SUM(1,1)", csv_text)
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            self.assertEqual(set(manifest["files"]), names - {"manifest.json"})
            for name, item in manifest["files"].items():
                self.assertEqual(len(archive.read(name)), item["bytes"])
            docx = zipfile.ZipFile(io.BytesIO(archive.read("report.docx")))
            self.assertIn("[Content_Types].xml", docx.namelist())
            self.assertIn("word/document.xml", docx.namelist())


class AutoPublishTests(SecondWaveBase):
    def test_dry_run_actual_publish_audit_and_rejections(self) -> None:
        draft_id, review_id = self.add_draft_review()
        self.config.values["kb_review_auto_publish"] = True
        self.config.values["kb_review_auto_publish_dry_run"] = True
        dry = self.svc.auto_publish_now(review_id, self.admin)
        self.assertTrue(dry["dry_run"])
        self.assertFalse(dry["executed"])
        self.assertEqual(self.db.one("SELECT status FROM kb_drafts WHERE id=?",
                                     (draft_id,))["status"], "reviewing")
        self.config.values["kb_review_auto_publish_dry_run"] = False
        result = self.svc.auto_publish_now(review_id, self.admin)
        self.assertTrue(result["ok"])
        self.assertEqual(self.db.one("SELECT status FROM kb_drafts WHERE id=?",
                                     (draft_id,))["status"], "approved")
        review = self.db.one("SELECT status,human_decision FROM kb_reviews WHERE id=?",
                             (review_id,))
        self.assertEqual(review["status"], "decided")
        self.assertEqual(review["human_decision"], "auto_publish")
        audits = self.db.all("SELECT action,target FROM audit_logs WHERE action='kb.auto_publish'")
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0]["target"], review_id)
        # 重复触发不能生成第二个文档。
        docs_before = self.db.one("SELECT COUNT(*) AS n FROM kb_documents")["n"]
        with self.assertRaises(KBError):
            self.svc.auto_publish_now(review_id, self.admin)
        self.assertEqual(self.db.one("SELECT COUNT(*) AS n FROM kb_documents")["n"], docs_before)

        # 低分、blocker、扫描占位稿与旧 prompt 都必须被拒绝。
        _, low_review = self.add_draft_review(score=80, confidence=0.95)
        self.assertFalse(self.svc._auto_publish_eval(
            self.db.one("SELECT * FROM kb_reviews WHERE id=?", (low_review,)))["eligible"])
        _, blocker_review = self.add_draft_review()
        self.db.execute(
            "UPDATE kb_reviews SET issues_json=? WHERE id=?",
            (json.dumps([{"severity": "blocker", "message": "字段冲突"}]), blocker_review))
        self.assertFalse(self.svc._auto_publish_eval(
            self.db.one("SELECT * FROM kb_reviews WHERE id=?", (blocker_review,)))["eligible"])
        _, scan_review = self.add_draft_review(warnings=["扫描 PDF，需人工转写"])
        self.assertFalse(self.svc._auto_publish_eval(
            self.db.one("SELECT * FROM kb_reviews WHERE id=?", (scan_review,)))["eligible"])
        _, old_prompt = self.add_draft_review(prompt_version="review-v0")
        self.assertFalse(self.svc._auto_publish_eval(
            self.db.one("SELECT * FROM kb_reviews WHERE id=?", (old_prompt,)))["eligible"])


if __name__ == "__main__":
    unittest.main()
