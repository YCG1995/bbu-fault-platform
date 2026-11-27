"""schema 25 → 26 的 KB 迁移回归：先删新列模拟旧库，再由当前代码升级。"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor"))

from bbu.config import Config  # noqa: E402
from bbu.db import Database, SCHEMA_VERSION  # noqa: E402
from bbu.kb_service import KBService  # noqa: E402


class KBMigrationTests(unittest.TestCase):
    def test_schema_25_to_26_keeps_counts_and_flags_old_jobs(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="bbu-kb-migrate-"))
        db_path = tmp / "data" / "bbu.db"
        db_path.parent.mkdir(parents=True)
        db = Database(db_path)
        now = "2026-11-26T12:00:00Z"
        with db.tx() as conn:
            conn.execute(
                "INSERT INTO kb_documents(id,title,title_clean,status,raw_text,masked_text,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                ("DOLD", "旧文档", "旧文档", "active", "原因", "原因", now, now))
            conn.execute(
                "INSERT INTO kb_cases(id,doc_id,ordinal,title,title_clean,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                ("KOLD", "DOLD", 1, "旧案例", "旧案例", "active", now, now))
            conn.execute(
                "INSERT INTO kb_import_jobs(id,user_id,status,created_at) VALUES(?,?,?,?)",
                ("JOLD", 1, "completed", now))
            conn.execute(
                "INSERT INTO kb_import_files(id,job_id,name,status,doc_id) VALUES(?,?,?,?,?)",
                ("FOLD", "JOLD", "old.csv", "done", "DOLD"))
        # 删除 schema 26 新增列/表，模拟 1.30.5 的 schema 25。
        conn = sqlite3.connect(db_path)
        try:
            for name in (
                "idx_kb_docs_content_hash", "idx_kb_cases_kb_status", "idx_kb_import_jobs_kb",
                "idx_kb_drafts_kb", "idx_kb_drafts_hash", "idx_kb_query_logs_kb",
                "idx_kb_reviews_kb", "idx_kb_terms_kb", "idx_kb_terms_unique",
                "idx_kb_docs_merged",
            ):
                conn.execute(f"DROP INDEX IF EXISTS {name}")
            for table, columns in (
                ("kb_documents", ("content_sha256",)),
                ("kb_cases", ("kb_id",)),
                ("kb_import_jobs", ("kb_id", "rollback_supported")),
                ("kb_import_files", ("doc_ids_json", "draft_ids_json")),
                ("kb_drafts", ("kb_id", "source_sha256", "content_sha256")),
                ("kb_query_logs", ("kb_id", "kb_ids_json")),
                ("kb_reviews", ("kb_id", "model_decision", "model_score")),
                ("kb_terms", ("kb_id",)),
            ):
                for column in columns:
                    conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
            conn.execute("DROP TABLE IF EXISTS kb_library_index_meta")
            conn.execute("DROP TABLE IF EXISTS kb_libraries")
            conn.execute("UPDATE settings SET value='25' WHERE key='schema_version'")
            conn.commit()
        finally:
            conn.close()
        # 关闭旧连接后再用当前代码升级。
        db.connect().close()
        upgraded = Database(db_path)
        self.assertEqual(SCHEMA_VERSION, 28)
        self.assertEqual(upgraded.one("SELECT value FROM settings WHERE key='schema_version'")["value"], "28")
        self.assertEqual(upgraded.one("SELECT COUNT(*) AS n FROM kb_documents")["n"], 1)
        self.assertEqual(upgraded.one("SELECT COUNT(*) AS n FROM kb_cases")["n"], 1)
        lib = upgraded.one("SELECT id,name,status,is_default FROM kb_libraries WHERE id=1")
        self.assertEqual(lib["name"], "故障知识库")
        old_job = upgraded.one("SELECT rollback_supported FROM kb_import_jobs WHERE id=?", ("JOLD",))
        self.assertEqual(int(old_job["rollback_supported"]), 0)
        config = Config(tmp)
        svc = KBService(upgraded, config, start_worker=False)
        status = svc.status({"id": 1, "role": "admin", "username": "admin"})
        self.assertEqual(status["documents"], 1)
        self.assertEqual(status["cases"], 1)
        self.assertEqual(len(status["libraries"]), 1)
        svc.rebuild_index()
        self.assertEqual(svc.status({"id": 1, "role": "admin", "username": "admin"})["index"]["cases"], 1)


if __name__ == "__main__":
    unittest.main()
