"""备份 v2：状态、草稿/审核/修订链/词典与 checksum 的恢复。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor"))

from bbu.config import Config  # noqa: E402
from bbu.db import Database  # noqa: E402
from bbu.kb_ingest import parse_paste  # noqa: E402
from bbu.kb_revision import verify_chain  # noqa: E402
from bbu.kb_service import KBService  # noqa: E402
from bbu.security import bootstrap_admin  # noqa: E402
from bbu.trace import TraceStore  # noqa: E402
from bbu.util import iso_now  # noqa: E402


class KBBackupV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="bbu-kb-backupv2-"))
        self.config = Config(self.tmp)
        self.db = Database(self.tmp / "data.db")
        bootstrap_admin(self.db, self.tmp, "初始管理员密码.txt")
        self.admin = {"id": 1, "role": "admin", "username": "admin"}
        self.traces = TraceStore(self.db, self.config)
        self.svc = KBService(self.db, self.config, start_worker=False,
                             traces=self.traces)
        self.lib = self.svc.create_library(self.admin, name="备份专家库", kind="expert")["library"]
        self.kb_id = int(self.lib["id"])

    def tearDown(self) -> None:
        try:
            self.db.close()
        except Exception:
            pass

    def test_roundtrip_keeps_status_drafts_reviews_terms_and_revision_chain(self) -> None:
        doc = parse_paste("【详细描述:复位】\n【原因分析】原始原因。\n【处理方案】原始方案。",
                          title="备份案例")[0]
        self.svc.save_document({"document": doc.public(), "cases": [c.public() for c in doc.cases]},
                               self.admin, kb_id=self.kb_id)
        item = self.svc.list_documents(kb_id=self.kb_id)["items"][0]
        case = self.svc.get_document(item["id"], reveal=True)["cases"][0]
        self.svc.update_case(case["id"], {"solution": "修订方案", "reason": "修订",
                                          "boards": ["VBP2e"], "domains": ["时钟"]},
                             self.admin)
        self.svc.set_document_status(item["id"], "disabled", self.admin)
        draft = self.svc.create_paste({"id": 1, "role": "engineer", "username": "engineer"},
                                      title="草稿案例", text="【原因分析】草稿原因",
                                      kb_id=self.kb_id)
        review = self.svc.create_review(self.admin, target_type="draft",
                                        target_id=draft["draft_id"], stage="manual", run=False)
        self.db.execute(
            "INSERT INTO kb_terms(kb_id,kind,term,aliases_json,weight,enabled,created_at) "
            "VALUES(?,?,?,?,?,?,?)", (self.kb_id, "synonym", "复位", "[\"重启\"]", 1.2, 1, iso_now()))
        self.svc.archive_library(self.kb_id, self.admin)
        filename, data = self.svc.backup_library(self.kb_id, self.admin)
        with zipfile.ZipFile(__import__("io").BytesIO(data)) as zf:
            manifest = json.loads(zf.read("manifest.json"))
            self.assertEqual(manifest["documents"], 1)
            self.assertEqual(manifest["drafts"], 1)
            self.assertGreaterEqual(manifest["reviews"], 1)
            self.assertEqual(manifest["revisions"], 2)
            self.assertEqual(manifest["terms"], 1)
            self.assertGreaterEqual(manifest["doc_versions"], 1)
            self.assertTrue(manifest["checksum"])
        restored = self.svc.restore_library(self.admin, data, name="恢复专家库")
        target = int(restored["kb_id"])
        self.assertEqual(restored["documents"], 1)
        self.assertEqual(restored["revisions"], 2)
        docs = self.svc.list_documents(kb_id=target, status="all")["items"]
        self.assertEqual(docs[0]["status"], "disabled")
        restored_case = self.svc.get_document(docs[0]["id"], reveal=True)["cases"][0]
        rows = self.db.all("SELECT * FROM kb_case_revisions WHERE case_id=? ORDER BY revision",
                           (restored_case["id"],))
        self.assertEqual(len(rows), 2)
        self.assertTrue(verify_chain(rows)["ok"])
        self.assertEqual(
            {r["value"] for r in self.db.all(
                "SELECT value FROM kb_case_tags WHERE case_id=? AND kind='board'",
                (restored_case["id"],))}, {"VBP2e"})
        self.assertEqual(
            {r["value"] for r in self.db.all(
                "SELECT value FROM kb_case_tags WHERE case_id=? AND kind='domain'",
                (restored_case["id"],))}, {"时钟"})
        self.assertEqual(self.db.one("SELECT COUNT(*) AS n FROM kb_drafts WHERE kb_id=?",
                                     (target,))["n"], 1)
        self.assertEqual(self.db.one("SELECT COUNT(*) AS n FROM kb_reviews WHERE kb_id=?",
                                     (target,))["n"], manifest["reviews"])
        self.assertEqual(self.db.one("SELECT COUNT(*) AS n FROM kb_terms WHERE kb_id=?",
                                     (target,))["n"], 1)
        trace_actions = [json.loads(r["meta_json"]).get("action")
                         for r in self.db.all(
                             "SELECT meta_json FROM trace_runs WHERE kind='kb' AND status='success'")]
        self.assertIn("library_backup", trace_actions)
        self.assertIn("library_restore", trace_actions)
        versions = self.db.all(
            "SELECT version,payload_json FROM kb_doc_versions v JOIN kb_documents d ON d.id=v.doc_id "
            "WHERE d.kb_id=? ORDER BY version", (target,))
        self.assertEqual(len(versions), manifest["doc_versions"])
        self.assertIn(restored_case["id"], versions[-1]["payload_json"])

    def test_restore_can_merge_into_existing_library(self) -> None:
        doc = parse_paste("【详细描述:合并】\n【原因分析】原因。\n【处理方案】方案。",
                          title="合并案例")[0]
        self.svc.save_document({"document": doc.public(), "cases": [c.public() for c in doc.cases]},
                               self.admin, kb_id=self.kb_id)
        _, data = self.svc.backup_library(self.kb_id, self.admin)
        target_lib = self.svc.create_library(self.admin, name="合并目标库", kind="fault")["library"]
        target = int(target_lib["id"])
        result = self.svc.restore_library(self.admin, data, target_kb_id=target)
        self.assertTrue(result["merged"])
        self.assertEqual(result["kb_id"], target)
        self.assertEqual(self.svc.list_documents(kb_id=target)["items"][0]["title"], "合并案例")

    def test_bad_checksum_is_rejected(self) -> None:
        self.svc.save_document({"document": {"title": "x", "raw_text": "x", "masked_text": "x",
                                             "boards": [], "domains": [], "versions": []},
                                "cases": [{"title": "x", "cause": "x", "solution": "x"}]},
                               self.admin, kb_id=self.kb_id)
        _, data = self.svc.backup_library(self.kb_id, self.admin)
        with zipfile.ZipFile(__import__("io").BytesIO(data)) as zf:
            files = {name: zf.read(name) for name in zf.namelist()}
        manifest = json.loads(files["manifest.json"])
        manifest["file_sha256"]["documents.jsonl"] = "0" * 64
        files["manifest.json"] = json.dumps(manifest).encode()
        buf = __import__("io").BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for name, body in files.items():
                zf.writestr(name, body)
        with self.assertRaisesRegex(Exception, "文件校验失败"):
            self.svc.restore_library(self.admin, buf.getvalue(), name="坏包")


if __name__ == "__main__":
    unittest.main()
