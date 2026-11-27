"""1.32.0 第一波：备份 v3 附件、原子恢复、去重、跨库上限与可见性。"""
from __future__ import annotations

import hashlib
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "vendor"))

from bbu.config import Config  # noqa: E402
from bbu.db import Database  # noqa: E402
from bbu.kb_service import KBError, KBService  # noqa: E402
from bbu.security import bootstrap_admin  # noqa: E402


def _doc(title: str, solution: str = "方案") -> dict:
    return {
        "document": {"title": title, "raw_text": f"{title} 原文", "masked_text": f"{title} 原文",
                     "boards": [], "domains": [], "versions": []},
        "cases": [{"title": title, "cause": "原因", "solution": solution}],
    }


class KBFirstWave132Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="bbu-kb-132-"))
        self.config = Config(self.tmp)
        self.db = Database(self.tmp / "data.db")
        bootstrap_admin(self.db, self.tmp, "初始管理员密码.txt")
        self.admin = {"id": 1, "role": "admin", "username": "admin"}
        self.engineer = {"id": 2, "role": "engineer", "username": "engineer"}
        self.svc = KBService(self.db, self.config, start_worker=False)
        self.lib = self.svc.create_library(self.admin, name="第一波库")["library"]
        self.kb_id = int(self.lib["id"])

    def tearDown(self) -> None:
        try:
            self.db.close()
        except Exception:
            pass

    def _backup(self, kb_id: int | None = None,
                include_files: bool = True) -> tuple[str, bytes]:
        return self.svc.backup_library(kb_id or self.kb_id, self.admin,
                                       include_files=include_files)

    def test_backup_v3_restores_attachment_rows_and_bytes(self) -> None:
        self.svc.save_document({
            "document": {"title": "附件案例", "raw_text": "原文", "masked_text": "原文",
                         "attachments": [{"file_id": "F-source", "name": "note.txt"}],
                         "boards": [], "domains": [], "versions": []},
            "cases": [{"title": "附件案例", "cause": "原因", "solution": "方案"}],
        }, self.admin, kb_id=self.kb_id)
        body = b"attachment body"
        sha = hashlib.sha256(body).hexdigest()
        stored = self.svc.files_dir / sha[:2] / sha[2:4] / f"{sha[:12]}-note.txt"
        stored.parent.mkdir(parents=True, exist_ok=True)
        stored.write_bytes(body)
        self.db.execute(
            "INSERT INTO kb_import_jobs(id,kb_id,user_id,status,mode,total_files,done_files,"
            "created_at) VALUES(?,?,?,?,?,?,?,?)",
            ("J-source", self.kb_id, 1, "completed", "direct", 1, 1, "2026-01-01"))
        self.db.execute(
            "INSERT INTO kb_import_files(id,job_id,name,rel_path,size,sha256,mime,status,error,"
            "doc_id,draft_id,doc_ids_json,draft_ids_json,confidence,stored_path) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("F-source", "J-source", "note.txt", "note.txt", len(body), sha, "text/plain",
             "done", "", "", "", "[]", "[]", 0, str(stored)))
        _, data = self._backup()
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            manifest = json.loads(zf.read("manifest.json"))
            self.assertEqual(manifest["version"], 3)
            self.assertEqual(manifest["import_jobs"], 1)
            self.assertEqual(manifest["import_files"], 1)
            self.assertEqual(manifest["files"], 1)
            self.assertEqual(zf.read(f"files/{sha}"), body)
            self.assertTrue(manifest["files_checksum"])
        result = self.svc.restore_library(self.admin, data, name="附件恢复库")
        self.assertEqual(result["import_jobs"], 1)
        self.assertEqual(result["import_files"], 1)
        self.assertEqual(result["files"], 1)
        row = self.db.one(
            "SELECT f.* FROM kb_import_files f JOIN kb_import_jobs j ON j.id=f.job_id "
            "WHERE j.kb_id=?", (result["kb_id"],))
        self.assertEqual(row["sha256"], sha)
        path, _ = self.svc.file_path(row["id"])
        self.assertEqual(path.read_bytes(), body)
        doc = self.db.one("SELECT attachments_json FROM kb_documents WHERE kb_id=?",
                          (result["kb_id"],))
        self.assertIn("F-source", doc["attachments_json"])

    def test_restore_is_atomic_when_a_later_document_fails(self) -> None:
        self.svc.save_document(_doc("原子一"), self.admin, kb_id=self.kb_id)
        self.svc.save_document(_doc("原子二"), self.admin, kb_id=self.kb_id)
        _, data = self._backup()
        before_libraries = self.db.one("SELECT COUNT(*) AS n FROM kb_libraries")["n"]
        original = KBService._insert_document_conn
        calls = {"n": 0}

        def boom(self, conn, payload, user_id, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("注入失败")
            return original(self, conn, payload, user_id, **kwargs)

        with mock.patch.object(KBService, "_insert_document_conn", boom):
            with self.assertRaisesRegex(RuntimeError, "注入失败"):
                self.svc.restore_library(self.admin, data, name="失败恢复")
        after_libraries = self.db.one("SELECT COUNT(*) AS n FROM kb_libraries")["n"]
        self.assertEqual(before_libraries, after_libraries)
        self.assertEqual(self.db.one(
            "SELECT COUNT(*) AS n FROM kb_documents WHERE kb_id<>?", (self.kb_id,))["n"], 0)

    def test_merge_skips_duplicate_source_and_content(self) -> None:
        self.svc.save_document(_doc("去重案例"), self.admin, kb_id=self.kb_id)
        _, data = self._backup()
        target_lib = self.svc.create_library(self.admin, name="去重目标")["library"]
        target = int(target_lib["id"])
        first = self.svc.restore_library(self.admin, data, target_kb_id=target)
        self.assertEqual(first["documents"], 1)
        second = self.svc.restore_library(self.admin, data, target_kb_id=target)
        self.assertEqual(second["documents"], 0)
        self.assertEqual(second["skipped_duplicates"], 1)
        self.assertEqual(second["skipped_cases"], 1)
        self.assertEqual(self.db.one(
            "SELECT COUNT(*) AS n FROM kb_documents WHERE kb_id=?", (target,))["n"], 1)

    def test_cross_library_search_rejects_more_than_five(self) -> None:
        ids = [self.kb_id]
        for index in range(5):
            ids.append(int(self.svc.create_library(
                self.admin, name=f"上限库{index}")["library"]["id"]))
        with self.assertRaisesRegex(KBError, "一次最多跨 5 个知识库检索"):
            self.svc.search("任意查询", self.admin, kb_ids=ids, log=False)

    def test_get_case_requires_visible_library_for_non_admin(self) -> None:
        private = self.svc.create_library(
            self.admin, name="仅管理员库", visibility="admin")["library"]
        self.svc.save_document(_doc("私有案例"), self.admin, kb_id=int(private["id"]))
        doc = self.svc.list_documents(kb_id=int(private["id"]))["items"][0]
        case_id = self.svc.get_document(doc["id"], reveal=False)["cases"][0]["id"]
        with self.assertRaisesRegex(KBError, "案例不存在"):
            self.svc.get_case(case_id, self.engineer)
        self.svc.get_case(case_id, self.admin)


if __name__ == "__main__":
    unittest.main()
