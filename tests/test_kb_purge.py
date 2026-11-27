"""离线物理清理：备份校验、归档门槛、审计/修订链保留。"""
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
from bbu.db import Database  # noqa: E402
from bbu.kb_ingest import parse_paste  # noqa: E402
from bbu.kb_service import KBService, KBError  # noqa: E402
from bbu.security import bootstrap_admin  # noqa: E402
from tools.kb_library_purge import purge_library  # noqa: E402


class KBPurgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="bbu-kb-purge-"))
        self.config = Config(self.tmp)
        self.db = Database(self.tmp / "data" / "bbu.db")
        bootstrap_admin(self.db, self.tmp, "初始管理员密码.txt")
        self.admin = {"id": 1, "role": "admin", "username": "admin"}
        self.svc = KBService(self.db, self.config, start_worker=False)
        self.lib = self.svc.create_library(self.admin, name="待清理专家库", kind="expert")["library"]
        self.kb_id = int(self.lib["id"])
        doc = parse_paste(
            "【详细描述:单板反复复位】\n【原因分析】喂狗时序异常。\n【处理方案】升级逻辑。",
            title="待清理案例", source_name="粘贴")
        self.svc.save_document(
            {"document": doc[0].public(), "cases": [c.public() for c in doc[0].cases]},
            self.admin, kb_id=self.kb_id)

    def tearDown(self) -> None:
        try:
            self.db.close()
        except Exception:
            pass

    def test_purge_requires_archive_and_backup_then_keeps_revision_chain(self) -> None:
        name, payload = self.svc.backup_library(self.kb_id, self.admin)
        backup = self.tmp / name
        backup.write_bytes(payload)
        with self.assertRaisesRegex(ValueError, "必须先归档"):
            purge_library(self.db.path, self.kb_id, "待清理专家库", backup, self.tmp)
        self.svc.archive_library(self.kb_id, self.admin)
        # 归档后仍可导出备份，便于删除前留档。
        name, payload = self.svc.backup_library(self.kb_id, self.admin)
        backup = self.tmp / name
        backup.write_bytes(payload)
        with sqlite3.connect(self.db.path) as conn:
            case_ids = [r[0] for r in conn.execute(
                "SELECT id FROM kb_cases WHERE kb_id=?", (self.kb_id,))]
            revisions = conn.execute(
                "SELECT COUNT(*) FROM kb_case_revisions WHERE case_id IN "
                "(" + ",".join("?" for _ in case_ids) + ")", tuple(case_ids)).fetchone()[0]
            self.assertGreater(revisions, 0)
        result = purge_library(self.db.path, self.kb_id, "待清理专家库", backup, self.tmp)
        self.assertTrue(result["ok"])
        self.assertEqual(result["counts"]["documents"], 1)
        self.assertEqual(result["counts"]["cases"], 1)
        with sqlite3.connect(self.db.path) as conn:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM kb_documents WHERE kb_id=?", (self.kb_id,)).fetchone()[0], 0)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM kb_cases WHERE kb_id=?", (self.kb_id,)).fetchone()[0], 0)
            self.assertGreater(conn.execute(
                "SELECT COUNT(*) FROM kb_case_revisions WHERE case_id IN "
                "(" + ",".join("?" for _ in case_ids) + ")", tuple(case_ids)).fetchone()[0], 0)
            lib = conn.execute("SELECT status FROM kb_libraries WHERE id=?", (self.kb_id,)).fetchone()
            self.assertEqual(lib[0], "deleted")

    def test_purge_rejects_default_library_and_name_mismatch(self) -> None:
        name, payload = self.svc.backup_library(1, self.admin)
        backup = self.tmp / name
        backup.write_bytes(payload)
        with self.assertRaisesRegex(ValueError, "默认知识库"):
            purge_library(self.db.path, 1, "故障知识库", backup, self.tmp)
        self.svc.archive_library(self.kb_id, self.admin)
        name, payload = self.svc.backup_library(self.kb_id, self.admin)
        backup = self.tmp / name
        backup.write_bytes(payload)
        with self.assertRaisesRegex(ValueError, "库名不匹配"):
            purge_library(self.db.path, self.kb_id, "错误名称", backup, self.tmp)


if __name__ == "__main__":
    unittest.main()
